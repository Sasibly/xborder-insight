# 运维手册（runbook）

> 用途：出问题时按「症状 → 一条命令 → 原因 → 处置」走，不靠回忆。
> 对应验收项：`docs/02` P4-4。所有命令都实跑过；写进来但没跑过的命令要标明。
> 最后核对：2026-11-24 11:26（schema v15，门禁 815 条；容器冒烟 19/19 于 v15 实测，11-23 20:52）。

## 开始之前：怎么跑一条命令（Windows，第一次用终端就看这节）

**「项目」在哪**：`D:\WorkBuddy_Spaces\spaces\2026_11_17_10-57-57`。里面有 `src`（代码）、
`tests`（815 条测试）、`docs`（这套文档）、`data/app.sqlite3`（本地库，已迁到 v15）、`.venv`（依赖）。
**不需要新建任何东西，也不需要先准备数据**——库由迁移从零建起，测试自带隔离库。

跑命令有两条路，任选：

**路 A（最省事）**：直接跟 WorkBuddy 说「跑一下 `scripts/check.py`，把最后 10 行贴给我」。
代理在本机执行，结果直接回给你。适合「我只想知道对不对、不想学命令」。

**路 B（自己动手）**：

1. 按 `Win` 键，输入 `Git Bash`，回车（装过 Git 就有；没有就用系统自带的 PowerShell）
2. 进项目目录，**盘符要写成 `/d/`**，反斜杠在 Bash 里会被当转义符吃掉：

   ```bash
   cd /d/WorkBuddy_Spaces/spaces/2026_11_17_10-57-57
   ```

3. 确认进对了（该列出 `src tests docs data`，缺一个就说明还在别的目录）：

   ```bash
   ls
   ```

4. 粘贴命令。**不用先激活虚拟环境**——本手册所有命令都写成 `.venv/Scripts/python.exe`，
   直接指向项目自己的解释器；敲 `python` 有可能跑到系统里那个（缺依赖 → `ModuleNotFoundError`）。

   ```bash
   .venv/Scripts/python.exe scripts/db.py status
   ```

**在 Windows 自带的 PowerShell 里**，把路径分隔符换成反斜杠即可：
`.venv\Scripts\python.exe scripts\db.py status`。

> 判断「这条命令是不是跑在项目里」的最快办法：报错里出现 `data/app.sqlite3` 或
> `src/xborder_insight` 说明进了；出现 `No such file or directory: 'scripts/db.py'`
> 说明没进到项目根目录——回到第 2 步。

## 0. 三个运维端点，别混用

故障时第一件事不是看日志，是分清「哪个环节坏了」。三个端点回答三个不同的问题：

| 端点 | 回答什么 | 查库吗 | 挂了意味着 |
| --- | --- | --- | --- |
| `GET /health` | 进程还活着吗 | 不查 | 进程死了 / 端口不通 / 容器退了 |
| `GET /ready` | 现在能接活吗 | 查（版本） | 进程活着但**不该接流量**：库没迁移、配置缺项 |
| `GET /metrics` | 系统跑了多少、多快 | 查（聚合台账） | 指标端点坏了不影响业务，但等于瞎了 |

**`/health` 返回 200 不代表能接活。** 这是本项目刻意分开两处的理由：数据库结构落后时，
`/health` 一直是 200，第一个写任务的请求才 500——那种「健康检查说健康」的故障，排查成本
远高于启动时直接拒绝。所以判断「能不能接活」看 `/ready`，不看 `/health`。

```bash
curl -s localhost:8000/health     # {"status":"ok","run_mode":"mock"}
curl -s localhost:8000/ready      # schema_version / auth / embedding / cluster_threshold
curl -s -H "Authorization: Bearer $KEY" localhost:8000/metrics
```

`/ready` 的响应里有两个字段是「部署事实回显」，不是装饰：

- `auth`：`required` / `disabled`。`disabled` 时任何人都能提交任务、花你的 token 预算。
- `cluster_threshold` + `cluster_threshold_source`：阈值可能来自 provider 标定值，也可能来自
  某次临时设的环境变量（`source=env`）。两者都能正常启动，但产出的主题粒度完全不同。
  怀疑「结果跟上个月不一样」时先看这一行，别先去怀疑模型。

## 1. 五分钟分诊表

| 症状 | 第一眼看什么 | 大概率是 | 去哪节 |
| --- | --- | --- | --- |
| 容器起不来，日志一行 `config_invalid` | 日志里的报错原文 | 环境变量显式失败（拼错 / 越界） | §2 |
| `/health` 200，`/ready` 503 `db_not_ready` | `/ready` 的 `detail` | 库没建，或结构落后于代码 | §3 |
| `/ready` 422 `input_invalid` | `detail` | 数据目录不可用 / live 缺 `LLM_API_KEY` | §3 |
| 所有 `/v1/*` 都 401 | `WWW-Authenticate: Bearer` | 键错、键被吊销、少 `Bearer ` 前缀 | §4 |
| 429 且带 `Retry-After` | 响应头秒数 | 请求太快（限流） | §4 |
| 429 但没 `Retry-After`，code 是 `quota_exceeded` | `/metrics` 的 `xbi_quota_tokens_remaining` | 当日 token 用完了（配额） | §4 |
| 任务一直 `queued` | `make jobs` | worker 没起 / 并发占满 | §5 |
| 任务一直 `running` 不动 | `make jobs`（会自己告警） | 租约过期没人回收（进程被强杀过） | §5 |
| 任务 `failed` | `python scripts/jobs.py show <job_id>` 的 `error_code` | 分五类，见 §5 表 | §5 |
| 提交返回 409 `idempotency_conflict` | 请求体 | 同一个键换了请求体 | §6 |
| 读任务返回 404 `job_not_found` | 租户 | 跨租户，或真不存在（刻意不区分） | §6 |
| 决策接口 503 `param_unverified` | `make db-params` | 参数没核实到官方来源（设计如此） | §7 |
| 决策接口 422 `param_out_of_range` | `detail` | 取值落在参数声明的区间之外 | §7 |
| `/metrics` 里没有延迟那一族 | `xbi_run_latency_samples` | 没有成功的 run，不是「0 毫秒」 | §8 |
| `make check` 的往返层失败 | 失败输出的表名 | `down()` 不完整，或 DDL 指纹不一致 | §9 |

## 2. 起不来：配置层直接拒绝

**症状**：进程启动即退出，日志是一条 JSON，`msg` 或 `code` 含 `config_invalid`。

```bash
# 直接跑一次，看报错原文（不要只看容器 restart 次数）
.venv/Scripts/python.exe -c "from xborder_insight.config import load_settings; load_settings()"
```

**为什么不让它带着错配置起来**：这类错误全部是「值拼错但不崩」。`DEFAULT_INCOTERM=FOBb`
会让利润模型静默走错分支，`JOB_MAX_ATTEMPTS=0` 会让任务永远写不进库——两者都能正常启动，
故障点离原因很远。所以配置层一律在启动时抛，`message` 里直接给允许值：

| 变量 | 允许值 / 范围 | 默认 |
| --- | --- | --- |
| `RUN_MODE` | `mock` / `live` | `mock` |
| `EMBEDDING_PROVIDER` | `hash` / `dashscope` | `hash` |
| `CLUSTER_SIMILARITY_THRESHOLD` | `(0, 1]` | 按 provider 取标定值 |
| `DEFAULT_INCOTERM` | `FOB` / `EXW` / `CIF` / `DDP` | `FOB` |
| `JOB_MAX_ATTEMPTS` | 1–10 | 3 |
| `JOB_LEASE_SECONDS` | ≥ 5 | 120 |
| `JOB_CONCURRENCY` | ≥ 1 | 2 |
| `JOB_POLL_SECONDS` | > 0 | 0.5 |

`RUN_MODE=live` 时缺 `LLM_API_KEY`，或 `EMBEDDING_PROVIDER=dashscope` 时缺同一个键，也会直接拒绝。

## 3. `/ready` 不 ok

### 3.1 `db_not_ready`（503）

两种情况，`detail` 会把话说全：

| detail 里写着 | 原因 | 处置 |
| --- | --- | --- |
| `缺少 .../app.sqlite3，先执行: python scripts/db.py up` | 库文件不存在 | `make db-up`（= `db.py up`） |
| `当前 vN，需要 vM；执行: python scripts/db.py up` | 结构落后于代码 | `make db-up`，然后重查 `/ready` |

**新镜像 + 旧库 = 第一个写请求就 500。** 所以入口脚本的顺序是「先迁移再起服务」
（`scripts/docker-entrypoint.sh`），不是「起服务后等它自己升」。如果你手工起服务而跳过迁移，
`/ready` 会替你拦住——这就是它查版本号的原因。

### 3.2 `input_invalid`（422）

| message | 原因 | 处置 |
| --- | --- | --- |
| `数据目录不可用` | `DATA_DIR` 不存在或不可读（挂载点没起来） | 查容器卷挂载；`detail` 里是期望路径 |
| `live 模式缺少 LLM_API_KEY` | `RUN_MODE=live` 但没给键 | 补 `.env` 或容器 `-e` |

### 3.3 `/ready` 是 200，但 `auth=disabled`

不是故障，是**必须当场确认的风险点**。`AUTH_REQUIRED` 在 `mock` 下默认 `false`、`live` 下默认
`true`；要拿掉必须显式写 `false`。启动日志里也会有一条 `auth.disabled`。

处置：对外可达的部署一律 `AUTH_REQUIRED=true`。回滚时最容易忘这一条——旧的启动命令里没有它，
容器一起来就是全开放。

## 4. 鉴权、限流、配额

三者都返回 4xx，但**调用方该做的事完全不同**，所以码必须分开：

| code | 状态 | 额外头 | 含义 | 处置 |
| --- | --- | --- | --- | --- |
| `unauthorized` | 401 | `WWW-Authenticate: Bearer` | 键缺失 / 无效 / 已吊销 | 换键；`make keys` 看状态 |
| `rate_limited` | 429 | `Retry-After: <秒>` | 一分钟内请求太多 | 按头里的秒数退避，别立即重试 |
| `quota_exceeded` | 429 | 无 | 当日 token 额度用尽 | 加额度，或等日界；重试无意义 |

```bash
make keys                                  # 列出密钥、租户、额度、最后使用时间
make key-create TENANT=acme QUOTA=200000 RPM=60   # 明文只打印一次，当场抄走
make key-revoke PREFIX=xb_abcd1234         # 按前缀吊销（创建时展示的前缀）
```

三个容易误判的点：

1. **「键不存在」与「键已吊销」返回同一句话。** 刻意如此：区分两者等于送出一个「这个键是否
   曾经有效」的探测接口。
2. **配额不另建计数器**，直接聚合 `jobs` 台账。所以 `make jobs` 的 token 归因就是配额依据，
   两边永远不会对不上；反过来，**手工改库不会让配额变宽松**——改的是真相源本身。
3. **「当日」按进程本地时区分界**（`date.today()`）。容器默认是 UTC，所以中国时间 08:00 才重置
   配额。觉得「明明没超却 429」时，先确认容器时区。

`/metrics` 的抓取**认人但不记限流窗口**。这不是漏记：Prometheus 默认 15 秒抓一次，若复用
普通请求的限流，抓取会自己吃掉 rpm，两小时内把自己的监控打死。

## 5. 任务侧：队列与租约

```bash
make jobs                                  # 队列快照 + 成本归因（有租约过期会直接告警）
python scripts/jobs.py list --status queued
python scripts/jobs.py show <job_id>        # 含每次尝试的执行台账
make jobs-recover                          # 回收租约过期的任务
make jobs-drain                            # 不起服务，在本进程把队列跑到空
```

### 5.1 一直 `queued`

| 检查 | 命令 | 结论 |
| --- | --- | --- |
| worker 起没起 | 启动日志里有没有 `job.worker_started` 一类记录；或看 `JOB_WORKER_ENABLED` | 关了就不会有人领任务 |
| 并发是否占满 | `python scripts/jobs.py list --status running` | 两三个长任务会把 `JOB_CONCURRENCY=2` 占满 |
| 服务是不是只起了 API | worker 是**进程内**的（同一进程的 asyncio task），不是独立进程 | 用 `--workers 4` 起多个 API 进程会得到多个 worker，见下 |

> 多进程部署的边界：`--workers N` 会起 N 个进程内 worker，SQLite 写会互相抢锁。单人部署规模下
> 用单进程 + `JOB_CONCURRENCY` 调并发；要真多实例，得换掉 SQLite，不在本项目的降级范围内。

### 5.2 一直 `running` 不动

**判据**：`make jobs` 会打印「⚠ N 个 running 任务租约已过期」。租约默认 120 秒，worker 每
1/3 租约（40 秒）续一次。

**原因**：进程被 `kill -9`、容器被 `docker rm -f`、机器重启——最后一次续租之后没有任何人接手。
**为什么不用「重启时把所有 running 改成 queued」**：多实例时会误杀另一个实例正在跑的任务。

```bash
make jobs-recover     # 租约过期且还有尝试次数的 → 重新入队；次数用尽的 → failed(lease_expired)
```

服务启动时（`lifespan`）也会自动做一次同样的回收。手工执行只用于「进程还活着但那次回收跳过了」
的情形——例如库当时还没迁移，日志里会有 `app.recovery_skipped`。

### 5.3 `failed`：先看 `error_code`

```bash
python scripts/jobs.py show <job_id>
```

| `error_code` | 含义 | 处置 |
| --- | --- | --- |
| `lease_expired` | 尝试次数已用尽，被判失败 | 修好根因（通常是进程被杀）后 `POST /v1/jobs/<id>/retry` |
| `upstream_error` | LLM / Embedding 侧超时、5xx、限流 | 看 `LLM_TIMEOUT_SECONDS`、供应商状态；重试即可 |
| `output_unparseable` | 模型输出不满足契约，重试后仍失败 | 看 prompt 版本与 `docs/04` 的契约；不是基础设施问题 |
| 其它字符串 | 处理器内部异常 | 按 `request_id` 抓日志（§10） |

`POST /v1/jobs/<id>/retry` 只对 `failed` 有效：成功或仍在排队的任务返回 409
（`invalid_state_transition`）。重试等于替这个租户再花一次钱，所以归属校验与详情接口一致。

## 6. 409 与 404

| 场景 | 码 | 为什么这么设计 | 处置 |
| --- | --- | --- | --- |
| 同一个 `Idempotency-Key` 配了**不同的**请求体 | `idempotency_conflict` | 不能返回旧任务当「重放」：调用方会以为新参数已生效，实际跑的是老参数。**结论错了但全程没报错** | 换一个键重新提交；确认旧任务是否要作废 |
| 对已完成 / 排队中的任务调 `retry` | `invalid_state_transition` | 状态机不允许 | 不需要重试 |
| 读 / 重试**别的租户**的任务 | `job_not_found`（404） | 返回 403 等于承认「这个 id 存在」，是侧信道 | 用本租户的键；确认 id 是否抄错 |

幂等重放成功时，响应头是 `Idempotency-Replayed: true`，`job_id` 与首次一致。看到 `false` 说明
这是新任务——如果你以为在重放，那说明键没带或带错了。

## 7. 决策接口 503 / 422：这是设计，不是故障

| code | 状态 | 含义 | 该做什么 |
| --- | --- | --- | --- |
| `param_unverified` | 503 | 结论依赖的参数还没核实到官方来源 | `make db-params` 看缺哪项；补齐来源与生效日期后升 `verified` |
| `param_out_of_range` | 422 | 参数已核实，但本次取值落在它声明的适用区间外 | 补该区间的参数，或换口径；重试同样请求不会变好 |
| `return_rate_missing` | 503 | 这一台没有登记两档退货率（`product_returns` 里没有这一行） | `make db-returns` 看已登记的 key；用 `returns-set` 补两档 |
| `input_invalid` | 422 | 请求里同时给了 `sku_key` 与 `cost.return_rate`（退货率只能有一个来源） | 去掉其中一个。想用库里的两档就删 `cost.return_rate`；想自己指定基线就删 `sku_key`（那样没有压力档） |

**不合并这几个码的理由**：「没查到」「查到了但不管用」「这一台根本没登记」需要的动作完全不同。
合成一个，第二种会被当成「再多找找就能解决」，第三种会被当成「参数库不完整」，都白花时间。
`input_invalid` 单独一类：它不是数据没就绪，是请求写错了——**重试同样的请求永远不会变好**。

### 7.1 契约在哪看，以及为什么它不会过期

| 想看什么 | 去哪 | 谁保证它不掉队 |
| --- | --- | --- |
| 字段含义、取值范围、互斥关系 | `GET /docs`（Swagger UI）或 `GET /openapi.json` | `tests/test_openapi_contract.py`：任何字段缺 `description` 都红 |
| 请求示例与结论口径 | `README.md`「决策接口」节 | 同上的错误体一致性用例 + `docs/04` §30/§31 |
| 错误码与处置 | 本节 §7 的表 | `tests/test_runbook.py`：表里的码必须与 `errors.py` 对得上 |

**为什么给文档写测试**：删掉一个 `description=` 不会让任何业务测试变红，只会让 `/docs`
悄悄变成一排字段名。而 `sku_key` 恰好是「看名字猜不到行为」的字段——它一给，
退货率来源整体切换、还会跑压力档。文档变空的代价是调用方按默认理解去填，
然后收到一个莫名其妙的 422。所以「字段必须有说明」被写成断言（9 条），
不靠自觉。

**文档本身不要鉴权**：`/docs` 与 `/openapi.json` 必须能匿名打开，否则调用方拿到
base_url 后第一步就卡住。这条也有用例钉着。

**决策接口的退货率只有一个来源。** 带 `sku_key` 就按 `product_returns` 里的两档算，
响应多一个 `returns` 块（基线/压力档毛利 + 值不值得谈 + 证据强弱，见 `docs/04` §30）；
不带就用 `cost.return_rate`，没有压力档。两个都给会被 422 挡下——
基线来自调用方、压力档来自库，两档不同源，比出来的差值是假的。

**`return_rate_missing` 只拦「没有这一行」，不拦 draft 行。** 退货率天然难核实
（后台只给类目阈值，真实 ASIN 均值要导报表），拒答 draft 会让压测整体无法进行。
证据强弱随结论一起输出，`make db-returns` 里逐台标着「类目阈值 / 假设」。

**503 而非 500 的理由**：语义是「依赖未就绪」，补齐参数即可恢复，不是服务内部坏了。

**不要为了让它返回 200 而把 draft 参数用起来。** `require()` 拒绝一切 `draft` 是刻意的：
内置参数参与计算会让结论看起来精确、实际没有出处。当前 12 项里 11 项**已填值但仍 draft**
（`m0013`，缺官方「生效日期」），所以内置参数依然不能参与计算——填值只解决「有人知道数从哪来」。

## 8. `/metrics` 怎么读

```bash
curl -s -H "Authorization: Bearer $KEY" localhost:8000/metrics
```

| 指标 | 口径 |
| --- | --- |
| `xbi_jobs_total{status="..."}` | 按状态计数，含 `queued` / `running` / `succeeded` / `failed` |
| `xbi_jobs_success_ratio` | **分母只算终态**（succeeded + failed），不含排队中 |
| `xbi_run_latency_ms{quantile="0.5"/"0.95"}` | 跨 run 现算的端到端耗时分位 |
| `xbi_run_latency_samples` | 参与分位的样本数 |
| `xbi_llm_calls_total` | 累计调用次数 |
| `xbi_tokens_total{kind="prompt"/"completion"}` | 累计 token |
| `xbi_quota_tokens_used{tenant}` / `_remaining` / `_limit` | 与配额检查同源（都聚合 `jobs` 台账） |

三条「数值看着对、其实是错的」的坑，已经在实现里避掉，读指标时要按同样口径理解：

1. **成功率的分母不含排队中**。把 `queued` 算进去，排队一长成功率就自己变差——那不是服务变差，
   是流量变多。
2. **没有样本时不出延迟族**。输出 `0ms` 等于宣称「我们很快」。看到 `xbi_run_latency_samples 0`
   （或整族缺失）说明还没有成功的 run。
3. **不限量时不出 `_limit`**。`limit=0` 在告警规则里等于「额度耗尽」，与「没有限制」正好相反。

命名债（已知，未动）：`runs.latency_p95_ms` 这一列存的是**单次 run 的端到端耗时**，不是 p95。
所以上面的分位是跨 run 现算的，不复用那个字段名。改名要按 `m0010` 的手法重建表，不在本轮范围。

## 9. 数据与迁移

```bash
make db-status       # 当前版本 / 最新版本 / 每个迁移的应用时间
make db-params       # 12 项成本参数：已填值 / 未填值
make db-fba          # FBA 费率维表覆盖率与缺口
make check           # 全量门禁：测试 + 往返 + 评测 + 基线 diff
```

### 9.1 `roundtrip` 失败

`make check` 的第二层做 `up → down → up`，比对 schema 指纹。失败通常是两种：

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| 第二次 `up` 撞 `table already exists` | `down()` 漏删某张表 | 补 `down()`；不要改 `up()` 绕过 |
| 指纹不一致（表名 / DDL 文本不同） | 建表语句被写成了 `CREATE TABLE "x"` 之类，引号进了 `sqlite_master` | 按 `m0010` 的手法：先 `ALTER TABLE ... RENAME`，再逐字写回原 DDL |

**为什么指纹要比字符串**：`up` 之后 `sqlite_master` 里的 DDL 是规范化后的文本。用 `CREATE TABLE
IF NOT EXISTS` + rename 重建出来的表，DDL 里会带双引号，语义相同但指纹不同——那意味着「升级过的库」
和「新建的库」结构不等价，早晚在别的地方炸。

### 9.2 改了 CHECK 约束

SQLite 不支持改 CHECK，只能重建表。顺序固定：建新表 → 搬数据 → 删旧表 → 改名。
**不要**直接 `CREATE TABLE ... AS SELECT`，类型与约束会一起丢。

### 9.3 备份与回滚

不在本手册重复，见 `README.md` 「交付」一节。三条硬规矩摘出来：

- 备份**必须用 `sqlite3.Connection.backup()`**，不能 `cp`：WAL 模式下最新提交可能还在 `-wal` 里，
  拷主文件会得到一个「少了一段最新提交、但看起来完全正常」的库。
- 回滚顺序是**先回镜像，再（按需）回库**，且优先不回库。旧的 `down()` 是 `DROP TABLE`，
  数据一起没——它只保证结构可逆，不保证数据无损。
- 旧镜像 + 新库能跑（迁移只加表加列）；新镜像 + 旧库不能跑（第一个写请求就 500）。

## 10. 用 request_id 定位一次请求

每个响应都带 `X-Request-Id`，同一个 id 出现在该请求产生的每一条日志里。日志是单行 JSON：

```json
{"ts":"2026-11-23T15:04:11","level":"INFO","logger":"xborder_insight.api","request_id":"a1b2c3d4e5f6","msg":"app.error","code":"param_unverified"}
```

```bash
docker logs xborder 2>&1 | grep a1b2c3d4e5f6
```

`X-Request-Id` 也会回显在错误响应体的 `request_id` 字段里（响应体结构固定为
`{code, message, request_id}`，不含堆栈）。

**日志里刻意不记的三样**：API Key、prompt 原文、评论正文。所以拿不到「模型到底看到了什么」——
需要复现时用同一份数据集重跑，不要期望从日志里捞出原文。

## 11. 每次变更后必跑（交接清单）

| 场景 | 必跑 | 通过标准 |
| --- | --- | --- |
| 任何代码改动 | `make check` | 退出 0：测试全绿、往返 vN、F1 与基线一致 |
| 改了迁移 | `make check` + `make db-status` | 往返通过；版本号 +1 |
| 改了接口 / 鉴权 / 指标 | `make smoke` | 17/17 |
| 改镜像 | `make smoke-docker` | 19/19（含非 root、HEALTHCHECK）；11-23 16:21 在 v13 上实测通过，Docker Hub 不可达时加 `BASE_IMAGE=public.ecr.aws/docker/library/python:3.13-slim` |
| 上线前 | `curl /health` → `curl /ready` → `curl /metrics` | ready 里 `auth=required`、`schema_version` 与代码一致 |
| 回滚后 | 同上三个端点 | **尤其确认 `auth`**：旧启动命令常常漏了 `AUTH_REQUIRED` |

## 12. 不要做的动作

| 不要 | 原因 |
| --- | --- |
| `cp data/app.sqlite3 backup.sqlite3` | WAL 下会丢最新提交，且备份看起来正常 |
| 对跑过一阵的库执行 `make rollback` | `down()` 是 `DROP TABLE`，数据一起没；回镜像才是正解 |
| `python scripts/db.py reset --yes` | 回滚到 0 再升级 = 清空所有任务与密钥（不加 `--yes` 会被拒，退出码 2） |
| 手工 `UPDATE jobs SET status='queued'` 捞卡住的任务 | 会绕过 attempts 计数，且不区分租约；用 `jobs-recover` |
| 手工改库让配额变松 | 配额直接聚合 `jobs`，改库改的是真相源本身 |
| 为了让决策接口返回 200 而给 draft 参数补「生效日期」 | 编出来的日期比没有日期更危险：它会让参数看起来被核实过 |
| 用 `curl` 打 `/metrics` 时复用业务 key 的高频调用 | 抓取不记限流，业务调用记；混用会看不清到底谁在吃 rpm |

## 13. 冷启动演练：换机器能不能跑起来

P4 的判据是「换一台干净机器，三条命令跑起来」。**在自己的 `.venv` 里跑绿不算数**——
本机跑过 `db.py up`、装过依赖、有缓存，能掩盖掉一半的交付缺陷。

### 13.1 三条命令（README 原文）

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # Linux/macOS 去掉 Scripts
make check                                                          # Windows 无 make 时用 check.cmd
```

然后（第三条命令全绿之后）：

```bash
python scripts/db.py up     # 建库并升到最新版本；smoke 会自建临时库，但 serve 需要真库
python scripts/smoke.py     # 17/17
```

### 13.2 2026-11-23 实测记录（`git clone` 到空目录 + 全新 venv；当时 v13）

| 环节 | 结果 |
| --- | --- |
| `pip install -r requirements-dev.txt` | 通过，约 5 分钟（无缓存） |
| 测试 | 727 passed + 1 xfailed |
| 迁移往返 | v13，10 张业务表 |
| 评测 | F1 0.6070 / P 0.7364 / R 0.5163，与基线逐位一致 |
| `db.py up` | 13 条迁移全部应用，`data/app.sqlite3` 生成 |
| `smoke.py` | 17/17，`/ready` 回 `schema=v13 auth=required` |
| `smoke.py --target docker` | **19/19**（另加非 root `uid=10001` 与 HEALTHCHECK）；Docker Hub 认证服务返回 Bad Gateway 时用 `--base-image public.ecr.aws/docker/library/python:3.13-slim` |

### 13.3 这次演练真抓到的缺陷（已修）

干净克隆首跑 **4 failed**，全是 503：

```
test_api.py::test_health_and_ready            KeyError: 'status'
test_api.py::test_analyze_by_dataset_returns_report   503 != 200
test_api.py::test_inline_reviews_path_works           503 != 200
test_metrics.py::test_endpoint_serves_prometheus_text 503 != 200
```

根因不是代码 bug，是**测试依赖了一个不进版本库的文件**：`api.DB_FILE` 默认指向
`<repo>/data/app.sqlite3`，而它在 `.gitignore` 里。本机因为跑过 `db.py up` 所以永远是绿的，
换台机器 `/ready` 直接 503。

修法（两处，不改生产逻辑）：

- `tests/conftest.py` 加 session 级 autouse fixture `app_db_file`：在 pytest 临时目录建库并
  跑完迁移，再把 `api.DB_FILE` 指过去，会话结束恢复。实测每条测试都拿隔离库，
  不碰工作区文件（需要「库不存在」场景的两条用例自己再 `monkeypatch` 一次，行为不变）。
- `tests/test_api.py::test_http_layer_never_reads_workspace_database`：断言 `api.DB_FILE`
  不等于工作区的 `data/app.sqlite3`。假设一旦被改回去，这条先红。

**这类缺陷的教训**：`make check` 全绿只证明「在这台机器上没退化」，不证明「别人能跑」。
冷启动演练必须真的换目录、换 venv，且**先确认目标目录里没有 `data/app.sqlite3`**，
否则演练等于没做。

### 13.4 受限环境提示

若在沙箱/CI 里 `make check` 因临时目录清理策略被拦（pytest 会周期性删除旧的
`pytest-of-*` 临时根），用 pytest 原生环境变量把临时根固定到项目外的一个空目录即可，
不需要改仓库：

```bash
PYTEST_ADDOPTS="--basetemp=/tmp/pytest-xbi" make check
```

## 附录 A · 错误码总表

响应体结构固定：`{"code": ..., "message": ..., "request_id": ...}`。

<!-- error-codes:begin -->
| code | 状态 | 含义 | 处置 |
| --- | --- | --- | --- |
| `config_invalid` | 500 | 环境变量非法，启动期拒绝 | 按 message 里的允许值改 `.env`（§2） |
| `input_invalid` | 422 | 入参或部署前置不合法 | 看 `detail`；数据目录、live 缺键都走这里 |
| `upstream_error` | 502 | LLM / Embedding 超时、5xx、限流 | 退避重试；查供应商状态 |
| `output_unparseable` | 502 | 模型输出不满足契约，重试后仍失败 | 查 prompt 版本与契约（§5.3） |
| `invalid_state_transition` | 409 | 状态机不允许这一步 | 例如对成功任务调 retry |
| `job_not_found` | 404 | 任务不存在，或不属于本租户 | 刻意不区分（侧信道） |
| `idempotency_conflict` | 409 | 同键不同请求体 | 换键重新提交（§6） |
| `param_unverified` | 503 | 参数未核实到官方来源 | `make db-params`（§7） |
| `param_out_of_range` | 422 | 取值落在参数适用区间外 | 补该区间的参数（§7） |
| `return_rate_missing` | 503 | 这台还没登记两档退货率 | `make db-returns` 看已登记的 key；`returns-set` 补（§7） |
| `db_not_ready` | 503 | 库未初始化或结构落后 | `make db-up`（§3.1） |
| `unauthorized` | 401 | 键缺失 / 无效 / 已吊销 | `make keys`（§4） |
| `rate_limited` | 429 | 单位时间请求过多 | 按 `Retry-After` 退避（§4） |
| `quota_exceeded` | 429 | 当日 token 额度用尽 | 加额度或等日界（§4） |
| `feature_disabled` | 403 | 接口存在但本部署关闭 | 如 `SYNC_ANALYZE_ENABLED=false` 时的同步分析 |
<!-- error-codes:end -->

这张表由 `tests/test_runbook.py` 与 `src/xborder_insight/errors.py` 逐项比对：新增或改名一个错误码
而没更新 runbook，门禁会红。文档漂移不能靠自觉。
