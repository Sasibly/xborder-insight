# 跨境选品决策引擎

把竞品评论区里的非结构化抱怨，转成**可排序、可溯源、可评测**的选品机会清单。

面向岗位：大模型 / AI 应用开发。重点不在「调用模型」，而在**让模型输出成为可校验的数据**，以及**用人工标注集证明效果**。

---

## 它解决什么问题

跨境卖家选品靠人工翻评论，痛点发现慢且主观。本项目把这件事变成一条可复现的管道：

```
竞品评论(JSONL) → 清洗去重/语言识别 → 单条结构化抽取(LLM)
   → 证据短语语义聚类 → 主题聚合 → 机会分排序 → 看板 / JSON
```

输出是机会主题清单，每个主题带提及数、负向占比、机会分与**原文证据**——结论可回溯，不是模型随口一说。

## 实测结果

以下数字全部由本仓库脚本产出，可现场复现。

| 指标 | 值 | 来源 |
| --- | --- | --- |
| 评测样本量 | 42 条（2 品类，人工逐条标注） | `data/eval/gold_labels.jsonl` |
| 抽取 Precision | **0.9375** | `scripts/run_eval.py` |
| 抽取 Recall | **0.7143** | 同上 |
| 抽取 F1 | **0.8108** | 同上 |
| 契约解析成功率 | 100% | 同上（见下方口径说明） |
| Token 用量 | 74 token / 条评论 | `scripts/run_eval.py` |
| 单次调用延迟 | P50 14 ms · P95 24 ms（mock） | 同上 |
| 端到端耗时 | 57 ms（20 条评论，离线模式） | `scripts/demo.py` |
| 模型调用次数 | 1 次 / 条评论 | 同上 |

**口径必须讲清楚，否则数字没有意义：**

1. 上述是 **`RUN_MODE=mock` 的离线规则基线**，用于证明管道正确性与评测流程可用，**不代表线上模型表现**。
2. 解析成功率 100% 是因为 mock 客户端自身输出即严格 JSON，**真实模型的解析率必须实测**——这正是本项目设置三级降级的原因。
3. 填入 `LLM_API_KEY` 并把 `RUN_MODE` 改为 `live`，执行同一条命令即可得到真实模型的对照数字，届时以新结果替换上表。
4. 成本只报 token 与调用次数，**不折算人民币**：佣金率、单价等参数需先核实官方口径（P1 成本参数表），未核实前不写钱数。

### 基线漏检在哪

12 条未完全匹配全部属于语义难点，是「规则 vs 模型」差异的直接证据：

- 间接表达：`"My phone actually drained battery while sitting on this thing"`（实为充电无效）
- 中文口语：`"就是比我预期的大一点"`（未命中「太大」关键词）
- 隐性故障：`"The tank joint does not seal"`（未出现 leak）

## 快速开始

```bash
# 1. 安装（Windows 使用 .venv/Scripts/，macOS/Linux 使用 .venv/bin/）
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt

# 2. 一条命令跑完所有门禁（测试 + 迁移可逆性 + 评测 + 基线退化比对）
make check            # 没有 make 的 Windows 直接双击或运行 check.cmd

# 3. 建库（迁移到最新版本）
make db-up

# 4. 跑通全链路（无需任何 API Key）
./.venv/Scripts/python.exe scripts/demo.py

# 5. 跑评测，产出指标
./.venv/Scripts/python.exe scripts/run_eval.py

# 6. 启动服务与看板
./.venv/Scripts/python.exe -m uvicorn xborder_insight.api:app --app-dir src --port 8000
# 打开 http://127.0.0.1:8000/

# 7. 异步任务：提交后立即返回，由进程内 worker 执行
curl -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Content-Type: application/json" -H "Idempotency-Key: demo-001" \
  -d '{"kind":"analyze","dataset":"wireless_charger","top_k":5}'

# 8. 任务运维（队列快照 / 成本归因 / 回收卡死任务）
make jobs             # 队列状态 + token 归因
make jobs-recover     # 回收租约过期的任务（进程被强杀后用）
make jobs-drain       # 不起服务，直接在本进程把待跑任务处理完
make jobs-archive     # 归档 30 天前完成的任务结果（默认预演，YES=1 才执行）

# 9. 接入控制（mock 下默认免鉴权，live 下强制开启）
make key-create TENANT=acme QUOTA=200000 RPM=120   # 明文只打印一次
curl -H "Authorization: Bearer <key>" http://127.0.0.1:8000/v1/jobs

# 10. 交付冒烟：临时库 + 临时端口 + 建密钥 + 提交任务 + 15 项断言
./smoke.cmd            # 或 make smoke
./smoke.cmd --target docker   # 构建镜像并起容器，额外断言非 root 与 HEALTHCHECK
```

`make check` 证明「改了没变差」，`make smoke` 证明「现在能用」。前者全绿而服务起不来是这类项目最常见的交付事故，两者都要跑。

接入真实模型：

```bash
cp .env.example .env
# 编辑 .env：RUN_MODE=live，填入 LLM_API_KEY
# live 模式会自动要求 AUTH_REQUIRED=true；要拿掉必须在 .env 里显式写 false，
# 且启动日志与 /ready 都会标出 auth=disabled——不给自己留「忘了开」的余地。
```

## 架构

```
src/xborder_insight/
├── config.py      环境变量集中校验，启动即失败（不留到第一个请求）
├── errors.py      类型化错误体系，对外只暴露 {code, message, request_id}
├── logging.py     结构化 JSON 日志，全链路携带 request_id
├── schemas.py     Pydantic 数据契约（本项目的核心）
├── taxonomy.py    10 类痛点标签闭集
├── llm.py         LLM 客户端：离线规则实现 / 百炼在线实现
├── dashboard.py   单页看板渲染
├── api.py         控制器层：解析请求、编排调用、格式化响应
├── service.py     应用服务层：一次业务动作的唯一实现（同步接口与异步任务共用）
├── versions.py    版本三元组 prompt / model / taxonomy（内容哈希，不是手写号）
├── db/
│   ├── connection.py  连接与显式事务；schema 指纹
│   ├── migrate.py     迁移执行器 + 往返校验
│   ├── migrations/    每个迁移含 up()/down()，版本号只增不改
│   ├── runs.py        执行台账 + 状态机（唯一能改 status 的入口）
│   └── params.py      成本参数访问；未核实即抛错，无默认值
├── jobs/
│   ├── models.py      任务状态机 + 请求指纹（规范化 JSON 的 sha256）
│   ├── store.py       幂等提交、租约领取、状态流转
│   ├── handlers.py    任务类型 → 领域调用（analyze / decision）
│   └── runner.py      执行器：心跳续租、失败标记、重启恢复
├── domain/
│   ├── incoterm.py    贸易术语 → 「哪些成本已含在报价里」的映射表
│   ├── profit.py      利润模型：条件生效 + 退货五段 + 盈亏平衡 ACOS
│   ├── compliance.py  合规三态：blocked / unknown / clear（缺失不等于通过）
│   └── decision.py    决策合成：GO / NO-GO / 需补充信息 + 逐条依据
├── security/
│   ├── keys.py        API Key：生成、sha256 存储、鉴权、吊销
│   └── quota.py       限流（按分钟窗口）与日 token 配额（聚合台账）
└── pipeline/
    ├── ingest.py   清洗、去重、语言识别
    ├── extract.py  结构化抽取 + 三级降级 + 并发
    ├── cluster.py  向量化与贪心聚类
    ├── score.py    机会分计算
    └── analyze.py  管道编排
```

分层原则：控制器不含业务逻辑，业务逻辑不依赖 HTTP 类型，模型输出必须先过契约校验才允许进入下游。`domain/` 是纯计算层——不读库、不读时钟、不读环境变量，因此可以在没有数据库和 API Key 的情况下被单独测试。

## 三个关键设计

### 1. 用闭集约束模型，而不是让它自由发挥

标签是固定 10 类。模型输出越界标签即触发校验失败并进入修复轮。这牺牲了一点表达自由度，换来的是：评测可以对齐、指标可跨批次比较、下游不必处理无穷多标签。

### 2. 三级降级，失败也要被计数

```
第 1 级  正常调用 → 解析 → Pydantic 校验
第 2 级  把校验错误回灌给模型，要求只修格式不改结论，重试一次
第 3 级  仍失败则标记 unparsed 并计入 parse_success_rate
```

不静默丢弃失败样本——**解析成功率本身就是指标**。静默吞掉失败，是 LLM 应用最常见的工程债。

### 3. 机会分用固定公式，不用模型打分

```
opportunity = 0.55 × 需求广度 + 0.35 × 负向占比 + 0.10 × 证据强度
需求广度 = min(1, ln(1+提及数) / ln(1+8))   # 对数压缩，防头部通吃
```

模型直接打 0-100 分不可复现、无法解释、排序结果无法答辩。固定公式让任何人用同样输入算出同样结果。

## 评测方法

抽取与聚类用**两套独立的标注集**，因为它们是两件事：

- **抽取**（`data/eval/gold_labels.jsonl`，42 条评论逐条人工标注痛点标签，标签取自同一 taxonomy 闭集）
  - 口径：只评估痛点抽取，positive/neutral 不计入分母；以 `(review_id, label)` 为单位做 micro 平均
  - 产出：`reports/eval_report.json`，含逐条漏检/误检明细、token 用量与 P50/P95 延迟
- **聚类**（`data/eval/cluster_pairs.jsonl`，51 条 evidence 两两标注 120 对，同 59 / 异 61）
  - 口径：pairwise，同簇即判同一主题；ARI 为主指标（pairwise F1 对「全合并」有系统性偏袒）
  - 判定标准只有一条：**同一条改进行动能同时解决这两条反映的问题吗**

> 抽取指标（F1 0.8108）**不包含聚类**。换 embedding、调阈值、改分组逻辑都不会让它动——
> 用这个数字验收聚类是错的。聚类有自己的标注集与守卫测试（`tests/test_cluster_eval.py`）。

## 聚类阈值标定：0.28 是猜的，0.65 是测的

原注释写着「hash 实测取 0.28，换 dashscope 应提到 0.55~0.65」。两句话都被推翻了：

| 向量 | 同主题对 P50 | 不同主题对 P50 | 结论 |
|---|---|---|---|
| hash（字符 1-3 gram） | 0.147 | **0.160** | 不同主题反而更相似——没有可用的排序结构 |
| dashscope（text-embedding-v3） | 0.712 | 0.675 | 有区分度，但分布重叠，单阈值不可能全对 |

照原注释把阈值定 0.55 会立刻出错：无关句对的实测相似度就在 0.43~0.57 之间。

**ARI 对照（阈值 0.12 / 0.65，按 label 组独立）**：

| 向量 | 分组键 | ARI | 说明 |
|---|---|---|---|
| hash | label | 0.113 | 接近随机 |
| hash | label+polarity | 0.535 | 只改分组就有数量级收益 |
| dashscope | label | 0.195 | 只换向量收益有限 |
| dashscope | label+polarity | **0.767** | 两者乘性叠加 |

两个改进不能互相替代，因为**极性混杂本身就在污染分区**：标注集里 43 对跨极性样本，
判为同主题的是 **0 对**。不拆极性，聚类必然产出「既要维持又要改进」的机会点。

最终取值 **0.65**（不是 ARI 最优的 0.56）：0.56 时 `size_fit` 四条 evidence 合成一簇，
里面同时有「比预期的大」和「Too small for two cats」——这种机会点不可执行；
0.65 的代价是 9 个漏合并（7 个集中在 `build_quality` 一条链上，表现为同一主题分两行），
属于展示冗余而非结论错误。两种错的代价不对称。

配置不再写死阈值：默认值按 `EMBEDDING_PROVIDER` 取标定结果（hash 0.12 / dashscope 0.65），
环境变量显式设置才覆盖，`/ready` 会回显 `cluster_threshold_source` 是 `calibrated` 还是 `env`。

完整过程（标注标准、阈值扫描曲线、13 对残余误合并的成因分类）见
[`docs/05-聚类阈值标定.md`](docs/05-聚类阈值标定.md)；复现 `python scripts/calibrate_cluster.py`。

## 质量门禁：怎么证明「改了没变差」

LLM 应用的改动（换模型、调 prompt、改切片）没有编译错误，也没有类型报错。**唯一能证伪它的，是同一份标注集上的指标是否退化。**

`make check` 串起三层，任一不过即非零退出：

| 层 | 内容 | 失败意味着 |
| --- | --- | --- |
| 测试 | 280 passed + 1 xfailed：契约解析、机会分公式、聚类阈值边界、极性判定、HTTP 契约、迁移可逆性、状态机、参数闸门、利润模型、贸易术语分支、合规三态、决策合成、决策接口、任务幂等与租约、密钥与限流配额、跨租户隔离、结果归档与幂等键存活、聚类评测与标定阈值守卫、**Dockerfile 静态契约（非 root / 探针 / 入口顺序 / 换行 / 锁文件）** | 数学或对外契约被破坏 |
| 迁移 | `up → down → up` 后比对 schema 指纹（临时库，不碰真库） | 回滚会失败，或重复执行不可幂等 |
| 评测 | 复跑 P/R/F1、token/条、调用次数、P95 延迟 | 链路跑不通或成本失控 |
| 基线 diff | 与 `tests/baseline.json` 比对，阈值见 `tests/gates.json` | 指标真实退化 |

**判定规则（刻意设计，不是拍脑袋）**：

- F1 掉超 **2 个点**必拦；Precision / Recall 单独掉超 5 点也拦——否则「召回换精确」的拆东墙能骗过 F1
- **失败样本集合变化**单独看：F1 没掉但错的样本换了，说明修一个坏一个，属于假性稳定。F1 有提升时只告警，否则直接拦
- 延迟只做**相对漂移**护栏（×3 以内），mock 模式下 P95 在 14–25 ms 间自然抖动，绝对阈值会天天误报
- 口径不一致（mode / model / 样本数变了）直接拒绝比对，而不是给出一个没意义的对比表

**这个门禁已经验证过能拦住真退化**：把极性判定里的星级兜底删掉，`make check` 立刻报 F1 从 0.8108 掉到 0.6250 并退出码 1——那正是当年那个 bug 的数值。也就是说，这个 bug 今后不可能再被悄悄引回来。

## 数据地基：让「凭印象填数」在工程上不可能

四张表，各有明确职责：

| 表 | 回答什么问题 | 关键约束 |
| --- | --- | --- |
| `reviews` | 输入是什么 | `(dataset, content_hash)` 唯一索引，去重口径与 `ingest.py` 完全一致 |
| `runs` | 这次分析发生了什么 | `status = 'running' OR finished_at IS NOT NULL`——终态必须有结束时间 |
| `cost_params` | 每个数字是多少、谁说的 | `verified` 状态强制要求 value + source_url + effective_date + verified_at 四项齐全 |
| `compliance_rules` | 这个品类在这个市场要过什么关 | 同上，且区分 `blocker` / `warning` |

**迁移纪律**（`src/xborder_insight/db/migrations/`）：

- 每个迁移必须有 `down()`，且是 `up()` 的严格逆操作
- 迁移内**不得使用 `now()` 或随机数**——否则「重复执行结果一致」不成立
- 已发布的迁移禁止再改，改结构只能新增
- 门禁第 2 层跑 `up → down → up` 并比对 schema 指纹（表名 → 规范化 DDL），diff 或残留即失败

这条往返校验不是形式主义。开发过程中它当场抓到一个真问题：`down()` 漏删 `cost_params`，导致第二次 `up()` 撞上 "table already exists"。更关键的是——**如果残留检测放在重建之后，这个错误会被伪装成一个语法错误**，排查方向直接跑偏。所以 `roundtrip()` 发现残留就立即返回，不再重建。

**「不许编数」的三道闸**：

1. 应用层：`params.require()` 遇到 draft 直接抛 `ParamUnverified`，**没有默认值回落**。`get(key, default=0.15)` 这类写法会让「参数缺失」变成静默降级，利润模型照常输出一个看起来正常的毛利——那正是 ②类错误
2. 数据层：`CHECK` 约束让「verified 但没来源」的写入被数据库拒绝，绕开应用层直接 `UPDATE` 也没用
3. 交付层：种子参数**全部是 draft、value 全为 NULL**，只有 11 条「需要核实什么」的声明

诚实交代当前状态：**成本参数表 11 项，已核实 0 项**。所以利润模型现在算不出结论——这是有意接受的代价，因为算不出来会报错，算错了没人知道。

## 利润模型：同一句「采购价 8 美元」，口径不同差 30%

报价所对应的贸易术语决定「哪些成本已在报价里」，因此不能对所有报价套同一公式：

| 术语 | 报价已含头程 | 报价已含关税 | 头程 | 关税 |
| --- | --- | --- | --- | --- |
| EXW / FOB | 否 | 否 | 计入 | 计入 |
| CFR / CIF / DAP | 是 | 否 | **置零** | 计入 |
| DDP | 是 | 是 | **置零** | **置零** |

两个方向相反的错误在输出上完全一样——都是一个带百分号的毛利率：

- FOB 报价当成含税价 → 漏算关税与头程 → 毛利虚高
- CIF 报价又加一遍头程 → 运费重复计 → 毛利虚低

所以模型在含税术语下**置零并记录口径声明**（`assumptions`），而不是「忽略」。两者在审计上不同：前者留下「我为什么没算」的记录，后者只留下一个 0。同时若调用方在 DDP 下仍传了非零税率，模型发 warning 而不是静默吞掉——静默会让「以为算了」和「确实没算」无法区分。

建站初期默认 **FOB**，且默认值必须回显（`incoterm_assumed: true`），不做静默填充。

**退货影响拆成一段收入冲减 + 四段费用**，各段基数不同：

| 段 | 公式 | 基数 |
| --- | --- | --- |
| 退款冲减 | 退货率 × 售价 | 售价（交易被撤销，不是费用） |
| 处理费 | 退货率 × 每件处理费 | 退货件 |
| 退款管理费 | 退货率 × 费率 × 原佣金 | **原佣金**（按售价取比例会放大一个数量级） |
| 商品损失 | 退货率 × 不可再售比例 × 报价 | 报价 |
| 头程损失 | 退货率 × 沉没比例 × 实际头程 | 实际计入的头程 |

第一段是写这个模型时**测出来的一个真缺陷**：最初只扣四段费用，收入按 100% 计，结果退货率 100% 竟算出正毛利 6.89、50% 退货率算出盈利 9.54。根因是退款从来没被扣过，偏差正好等于 `退货率 × 售价`。补上退款冲减后，同一情景分别为 −23.10 与 −5.45。现在 `tests/test_profit.py` 里有一条测试专门锁这个：它同时断言「修复后为亏损」和「把修复项摘掉就会重现 +9.54 这个旧结论」。

**负毛利时不返回负数 ACOS**，返回 `null` 加理由。`ACOS = −0.20` 会被读成「还有 20% 空间」，这是模型能犯的最危险的错误：数值合法、含义相反。

## 决策接口：三档结论，每档都能说出依据

`POST /v1/decision` 把机会分、单件毛利、合规规则合成一个结论：

| 结论 | 含义 |
| --- | --- |
| `GO` | 毛利、广告空间、需求均达标，且合规无阻断项 |
| `NO_GO` | 按已知信息判断不该做（合规阻断 / 结构性亏损 / 毛利过薄 / 投不出量 / 需求不足） |
| `NEED_MORE_INFO` | 利润与需求已过，但合规尚未核实——**判断不了，不是判断为不行** |

三档里最容易被砍掉的是第三档。把它并进 `NO_GO` 会误杀可能完全可行的品；并进 `GO` 就是在没有依据的情况下放行。所以它必须独立存在。

判定按否决力排序：**合规 > 毛利 > 广告空间 > 需求**。顺序不是随意的——合规不过就根本不能上架，此时讨论毛利毫无意义。但**所有因素都会逐条记进 `reasons`**，不只记录触发否决的那一条：只报否决项会让人以为「修好这一项就能做」。

**成本数据由调用方提供**（`cost` 字段），不强制从内置参数表读。理由很实际：内置的 11 项参数目前全是 draft，若只能从参数表读，接口在参数补齐前永远 503，无法验证。让调用方带上自己的报价，接口立刻可用，且责任边界清晰——响应里会原样回显 `data_provenance`，不把他人的估算冒充成系统的核实结论。

实测（毛利 31.2%、机会分 0.66，全部达标）：

```json
{
  "verdict": "NO_GO",
  "headline": "存在阻断级合规要求，当前不能上架（其中含未核实项，核实后可能解除）",
  "profit": {"gross_margin": 0.312382, "breakeven_acos": 0.312382},
  "compliance": {"status": "blocked", "draft_rules": 4}
}
```

利润很好但规则没核实，所以不给绿灯。这就是「不许编数」在接口层的样子。

## 任务化：把「一次请求」和「一次执行」分开

同步接口在真实模型下撑不住：单条评论 1–3 秒，500 条必然超时。这不是优化问题，是**物理限制**。

```
POST /v1/jobs      → 202 {job_id, status: queued}      立即返回
GET  /v1/jobs/{id} → 状态 + result + 每次执行的台账
GET  /v1/jobs      → 队列列表
POST /v1/jobs/{id}/retry → 失败任务重新入队
```

**四件事都做在数据库里，不在内存里**——内存队列写起来快，但重启即丢，且丢得毫无痕迹。

| 机制 | 实现 | 不加会怎样 |
| --- | --- | --- |
| 幂等 | `UNIQUE(tenant_id, idempotency_key) WHERE key IS NOT NULL`，直接 INSERT 撞键后回查 | 「先查再插」在并发下必然漏，重复任务 = 重复付费 |
| 键复用检测 | 比对请求体规范化 JSON 的 sha256，不同则 409 | 返回旧任务会让调用方以为新参数已生效，实际跑的是老参数 |
| 防永久卡死 | 租约 `lease_expires_at` + 执行期心跳续租（租约 1/3 周期） | 进程被 kill 后任务永远 `running`，看起来「正在跑」 |
| 计费不重复 | `runs(job_id, attempt)` 部分唯一索引 | 同一次尝试被记两笔，或重试覆盖上一次的账 |

**`jobs` 与 `runs` 是两张表，不是一张**：`jobs` 记录「这次请求排到哪了」（幂等、排队、卡死恢复），`runs` 记录「这次执行花了多少钱」。重试会产生**新的** run——因为它真的又调了一次模型；但同一次尝试只能记一笔。把两者合成一张表的后果是：要么少算钱，要么多算一次幂等。

**版本三元组** `prompt_version / model / taxonomy_version` 落库在任务行上，且版本号是**内容哈希**而非手写 `v1.2`。手写号一定会在某次改动后忘记升，而忘了升的版本号比没有版本号更糟——它看起来可信。

实测（mock 口径，离线规则客户端）：

```
提交 3 条评论的分析任务 → 202 立即返回
轮询 1 次后 succeeded
usage: llm_calls=3, total_tokens=153
executions: 1 条（attempt 1, succeeded, run-a1a90dd84d96）
同一 Idempotency-Key 重发 3 次 → 3 次都返回同一 job_id、idempotency-replayed: true，队列仍是 1 条
同键换请求体 → 409 idempotency_conflict（带 request_id）
kill 掉 worker 留下的 running 任务 → 租约过期后 recover 重新入队 → 另一进程接手完成，attempts=2
```

### 结果归档：清字段，不删行

`jobs.result` 是表里最大的字段（一份完整报告），却只在任务完成后很短一段时间内被读。不管它，`jobs` 会单调膨胀，第一个被拖慢的查询恰恰是「列出我的任务」。

```bash
make jobs-archive              # 预演：多少条、能省多少
make jobs-archive YES=1        # 真执行
python scripts/jobs.py archive --days 30 --yes
```

**归档只清 `result`，不删行**——这条不能改。幂等靠 `UNIQUE(tenant_id, idempotency_key)`，删行会让归档过的幂等键一起消失，同一个键重放会变成新任务：**重新花一次钱，而调用方看到的响应和正常重放一模一样**。所以：

- 行、执行台账、token 归因全部保留，只把 `result` 置空并打上 `archived_at`
- 接口返回 `result_archived: true`，把「结果已归档」和「本来就没有结果」分开——两者在 body 里都表现为「没有 result 字段」，不加标记就是「成功却没有结果」，任何人看到都会当成 bug
- 建的是**部分索引**（`WHERE result IS NOT NULL AND archived_at IS NULL`）：行一归档就退出索引，索引不跟着表膨胀
- 默认 dry-run，`--yes` 才动手；`--days 0` 直接拒绝（它的真实含义是「把刚跑完的结果立刻丢掉」）

`tests/test_archive.py` 里最关键的一条断言不是「清了多少行」，而是**归档之后同一幂等键重放仍返回原 job_id**。

**负向验证**：把「指纹比对」那行短路掉（模拟有人图省事删掉它），两条测试立刻变红——库层测试和 HTTP 层测试各一条。所以这条防线不是靠自觉。

## 接入控制：租户从密钥派生，配额查台账

`/v1/*` 的租户**只能来自密钥**。请求体里塞 `tenant_id` 会被契约直接拒（`extra="forbid"` → 422）——「允许客户端声明自己是谁」等价于没有隔离，而它会一路看起来很正常。

```bash
# 创建：明文只打印这一次
make key-create TENANT=acme QUOTA=200000 RPM=120
make keys                                   # 前缀、租户、额度、最近使用
make key-revoke PREFIX=xb_abcd1234
python scripts/keys.py stale --days 90      # 该轮换的（只提醒，不自动吊销）
```

客户端带 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`。

| 机制 | 实现 | 换成省事写法会怎样 |
| --- | --- | --- |
| 明文不落库 | 只存 sha256，`key_hash` 即主键 | 「帮我查一下这把 key」变成可能，而找回明文的能力本身就是漏洞 |
| 不加盐、不用慢哈希 | 高熵随机串 + 等值查询 | bcrypt 逐行加盐会让每次鉴权退化成全表扫描；慢哈希防的是口令字典，不是这里 |
| 失败信息统一 | 格式错 / 不存在 / 已吊销都说「API Key 无效」 | 区分它们等于送出一个「这个 key 曾经有效吗」的探测接口 |
| 别人的任务一律 404 | `get_scoped` 把「不属于你」当成「不存在」 | 403 会确认这个 id 存在，job_id 就成了可枚举的探测接口 |
| 限流计数落库 | `rate_limits(tenant_id, window_start)` + UPSERT ... RETURNING | 放进程内存：重启即重置，多实例各算一份，实际是标称值的 N 倍 |
| 被拒的请求也计数 | 先提交计数，再抛 429 | 回滚掉的计数让「一直重试」永远是第一个请求，限流等于没有 |
| 配额不另建计数器 | 直接聚合 `jobs` 台账 | 计数器一定会漂移（补记、重试、改口径、手工修数），而配额错了要么少收钱要么误伤客户 |

**两个 429 必须能区分**，因为调用方要做的事完全不同：

| code | 含义 | 该做什么 |
| --- | --- | --- |
| `rate_limited` | 太急 | 按响应头 `Retry-After` 退避 |
| `quota_exceeded` | 今天用完了 | 等次日重置，或提额；重试没有意义 |

实测（真实服务，`AUTH_REQUIRED=true`，非单测）：

```
无 key                      → 401 + WWW-Authenticate: Bearer
带有效 key 提交 analyze      → 202 → 轮询 1 次 succeeded
另一租户读同一 job_id        → 404 job_not_found（与真的不存在完全同响应）
rpm=5，连发第 6 次           → 429 rate_limited，Retry-After: 42
quota=1，先跑完一个任务后    → 429 quota_exceeded
同一时刻提交 decision 任务    → 202（不调模型，按额度拦它属于误伤）
额度用尽后重放原请求         → 202 idempotent_replay=true（已付过钱的结果不能扣住不给）
```

**负向验证**：短路归属校验 → 2 条测试变红；让被拒请求不计数 → 1 条变红，且报错直接写出「限流可以被一直重试绕过」。恢复即绿。

鉴权的默认值跟随运行模式：mock 关闭、**live 强制开启**。这不是洁癖——「部署时忘了开鉴权」不会有任何报错，只会安静地对所有人开放，所以 `AUTH_REQUIRED` 的默认值必须站在安全那一边，关闭时还要在启动日志和 `/ready` 里各留一处痕迹。

## 交付：镜像、冒烟、回滚

### 镜像

```bash
docker build -t xborder-insight:0.5.0 .
docker run -d --name xborder -p 8000:8000 \
  -e RUN_MODE=mock -e AUTH_REQUIRED=true \
  xborder-insight:0.5.0
```

三个不是随手写的选择：

| 选择 | 为什么 | 代价 |
| --- | --- | --- |
| 两阶段构建，依赖装进 `/opt/venv` 再整目录拷贝 | 编译器与 pip 缓存不进最终镜像；改代码不使依赖层缓存失效 | 镜像多一层目录 |
| 非 root（uid/gid 10001，固定值） | 进程被打穿时，拿到的不是「可写镜像文件系统」的权限；固定 uid 是为了卷挂载时宿主机能对上同一个人 | 挂载卷要先 `chown 10001:10001` |
| 单 worker | 进程内跑着任务执行器，SQLite 写并发上限摆在那里；多 worker 不会更快，只会抬高「同一任务被两个进程领走」的概率（租约兜得住，但那是兜底） | 单机吞吐上限即单进程上限 |
| 首行**不写** `# syntax=docker/dockerfile:1` | 本文件没有用任何 BuildKit 专有语法（无 heredoc、无 `--mount=type=cache`）；写了就会让构建第一步去 Docker Hub 拉 frontend 镜像，等于给一次离线构建加一条外网硬依赖 | 以后真要用 BuildKit 新语法时得先加回来 |

**基础镜像可换。** `Dockerfile` 里是 `ARG PYTHON_IMAGE=python:3.13-slim`，两阶段都从它起：

```bash
# Docker Hub 可达（常规）
docker build -t xborder-insight:0.5.0 .
# Docker Hub 不可达：优先取官方镜像的公共副本（同一个镜像，内容一致）
docker build -t xborder-insight:0.5.0 \
  --build-arg PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-slim .
# 或换一个同版本 Python 3.13 的发行方
docker build -t xborder-insight:0.5.0 \
  --build-arg PYTHON_IMAGE=mcr.microsoft.com/devcontainers/python:3.13 .
# 冒烟里直接覆盖
./smoke.cmd --target docker --base-image public.ecr.aws/docker/library/python:3.13-slim
```

**Docker Hub 被干扰时的取镜像顺序**（本机实测）：
1. `public.ecr.aws/docker/library/<name>:<tag>` —— AWS 上的 Docker 官方镜像副本，**内容与 Hub 一致**，首选
2. `mcr.microsoft.com/...` —— 微软发行方，可用但换的是发行方
3. 第三方加速器 —— 只当最后手段，不进交付物

实测判据：`registry-1.docker.io` 与 `auth.docker.io` 的 DNS 正常、TCP 能连（6.3s、有重传），
但 TLS 握手被打断；同期 `mcr.microsoft.com` 0.27s 返回 200。这属于网络层干扰，不是 Docker 配置错误——
所以处置方向是「换取值渠道」，不是改 `daemon.json` 加代理。

换的只是发行方，不是 Python 版本，所以构建步骤不用跟着改。之所以留这个口子：`registry-1.docker.io` 在部分网络下是 TLS 层直接被重置，此时「Dockerfile 写得对不对」和「镜像能不能构建」是两件必须分开回答的事——写死了就只能回答「不知道」。

`HEALTHCHECK` 打的是 `/ready` 而不是 `/health`：`/health` 只证明进程活着，`/ready` 还要求库已迁移到最新版本。用 `/health` 做探针的容器会「健康地」接流量然后全量 500。

容器入口是 `scripts/docker-entrypoint.sh`，做两件事：先 `db.py up`，再 `exec uvicorn`。

- **迁移放在入口而不是应用启动时**：应用内自动迁移意味着「任何一个跑起来的进程都可能改库结构」，多实例时就是两个进程同时往一个库上跑 DDL。放进入口，迁移时机只有「容器启动」一个。
- **`exec` 而不是裸调用**：PID 1 让给 uvicorn，`docker stop` 的 SIGTERM 才能直达，否则每次停容器都要等满 10 秒再 SIGKILL。
- 迁移失败 → 脚本退出 → 容器退出 → 编排层重启。这比「起得来但写请求才 500」好：后者的报错里不会有「你忘了迁移」。

`requirements.lock` 与 `requirements.txt` 并存：前者给镜像（精确版本），后者给本地开发（补丁区间）。只留区间，会出现「今天构建通过、下周构建失败」，或者更糟——悄悄换了版本、指标没变但行为变了。

### 冒烟：一条命令回答「现在能不能用」

`make check` 和冒烟不是替代关系，是**互补**关系：

| | `make check` | `scripts/smoke.py` |
| --- | --- | --- |
| 回答 | 改了会不会变差 | 现在能不能用 |
| 载体 | 进程内 `TestClient` + 临时库 | 真起服务、真走端口、真建库、真建 key |
| 覆盖 | 逻辑正确性、迁移可逆、指标退化 | 启动顺序、迁移时机、鉴权、任务闭环、非 root |
| 服务起不来时 | **也可能全绿** | 必然红 |

`make check` 全绿而服务起不来，是这类项目最典型的交付事故：测试里 `TestClient` 自己建库、自己塞 key，整段「谁在什么时候建库」被跳过了。

```bash
make smoke                                      # 本机：临时库 + 临时端口
make smoke-docker                               # 构建镜像 → 起容器 → 同一套断言
python scripts/smoke.py --target remote \
  --base-url http://host:8000 --api-key xb_xxx  # 打一个已跑起来的环境
```

local 目标 15 项断言，docker 目标 17 项（多「非 root」与「HEALTHCHECK」两条容器专属断言）。local 实测输出（mock）：

```
01 /health 存活                          {"status":"ok","run_mode":"mock"}
02 /ready 就绪且 schema 与源码一致        schema=v6 auth=required
03 /ready 回报 auth=required
04 无密钥访问 /v1/jobs → 401 + WWW-Authenticate
05 创建租户密钥                           tenant=smoke-a / smoke-b
06 /v1/datasets 可读                     pet_fountain, wireless_charger
07 提交分析任务 → 202                     status=queued
08 分析任务执行完成                       llm_calls=3
09 结果含主题与证据                       3 个主题 · 3 条评论
10 同键重放 → 同一 job_id                 Idempotency-Replayed=true
11 同键换请求体 → 409                     idempotency_conflict
12 另一租户读同一任务 → 404
13 执行台账每次尝试一条                    attempt=1
14 提交决策任务 → 202
15 决策任务出结论                         verdict=NO_GO
```

几处刻意的写法：

- **第 02 项拿「源码的 `migrate.LATEST`」去和容器自报的版本对账**，而不是相信容器。镜像里漏打一个迁移文件时，容器会认为自己已是最新，只有和源码对账才抓得到。
- **mock 下强制 `AUTH_REQUIRED=true`**。鉴权在本地通常是关的，于是「401 → 建 key → 带 key 提交 → 越权 404」这条生产必经链路从来没被真的跑过。
- **冒烟里用 3 条内联评论，不跑整个数据集**：live 模式下 42 条就是 42 次真实调用。「数据集能读」由第 06 项单独验证，不占模型预算。
- **跳过不等于通过**：remote 模式没提供第二把 key 时，越权那项显示 `–` 而不是 `✓`。
- **失败继续跑完**：401 没拦住指向鉴权，任务不完成指向 worker，两个症状对应完全不同的修复动作；遇错即停只会报出第一个症状。

退出码与 `check.py` 一致：`0` 全过、`1` 有断言失败、`2` 环境问题（docker 不可用 / 服务起不来 / 端口不通）。

**负向验证（实测，不是声称）**：

```
AUTH_REQUIRED=false 起服务 → 第 03、04 项变红，退出码 1
库未迁移就起服务           → /ready 503，退出码 2（环境错误，不是「通过」）
```

### 容器冒烟：已实测 17/17

```text
17 项全过（docker 目标）。默认基础镜像 python:3.13-slim 与 MCR devcontainers 各跑一遍，均为 17/17、EXIT=0：

01 /health 存活                          {"status":"ok","run_mode":"mock"}
02 /ready 就绪且 schema 与源码一致        schema=v6 auth=required
03 /ready 回报 auth=required
04 无密钥访问 /v1/jobs → 401 + WWW-Authenticate
05 创建租户密钥（容器内执行 CLI）          tenant=smoke-a / smoke-b
06 /v1/datasets 可读（镜像含样本数据）     pet_fountain, wireless_charger
07 提交分析任务 → 202                     status=queued
08 分析任务执行完成（容器内 worker）       llm_calls=3
09 结果含主题与证据                       3 个主题 · 3 条评论
10 同键重放 → 同一 job_id                 Idempotency-Replayed=true
11 同键换请求体 → 409                     idempotency_conflict
12 另一租户读同一任务 → 404
13 执行台账每次尝试一条                    attempt=1
14 提交决策任务 → 202
15 决策任务出结论                         verdict=NO_GO
16 容器以非 root 运行                     uid=10001     ← 容器专属
17 容器 HEALTHCHECK 判定健康              healthy       ← 容器专属

冒烟通过：17/17
```

这条路径**第一次是构建失败的**，而且失败点不在 Dockerfile 的逻辑，在交付环境本身：

| 失败 | 现象 |
| --- | --- |
| 首行 `# syntax=docker/dockerfile:1` | 构建第一步就去 Docker Hub 拉 frontend 镜像，而该网络下 `registry-1.docker.io` 是 TLS 层直接重置 |
| `Dockerfile` 84 处 / 入口脚本 34 处 CRLF | 过了第一关后，`RUN pip install -r .../requirements.lock\r` 会报出一个「看起来完全正常」的路径 |

两者都已修，并加了 14 条静态守卫（`tests/test_dockerfile.py`）盯着别再回来。Docker Hub 不可达时用
`--base-image` 换发行方（见上文「镜像」小节），Python 版本不变，冒烟断言一条都不用改。

**探针的两个分支都验过**——只验 healthy 等于没验，恒返回 200 的探针也能通过：

```text
起容器 → 20s 后 healthy（--start-period=20s 正好覆盖迁移耗时）
容器内把 schema 回退到 v3 → /ready 返回 503
再等 45s（3 次失败 × 15s）→ HEALTHCHECK 变 unhealthy
```

**容器专属断言的负向验证**：删掉 `USER app` 重建镜像 → 容器 `id -u` 变成 `0`，第 16 项会红。
它不是在拿一个恒为 10001 的值自证。

### 回滚

两件事要分开回滚：**镜像**和**数据库结构**。

```bash
# 1. 先备份（必须用 sqlite 的 backup API，不能直接 cp）
./.venv/Scripts/python.exe - <<'PY'
import sqlite3
src = sqlite3.connect("data/app.sqlite3")
dst = sqlite3.connect("data/backup-20261118.sqlite3")
src.backup(dst)
PY

# 2. 回滚到指定 schema 版本（破坏性：down 是 DROP TABLE，数据一起没）
make rollback TO=3          # = db.py down --to 3 --yes

# 3. 回滚镜像
docker rm -f xborder && docker run -d --name xborder -p 8000:8000 \
  -e RUN_MODE=mock -e AUTH_REQUIRED=true xborder-insight:0.4.0
```

**顺序不能反**：先回镜像，再按需回库，且优先不回库。

- 旧镜像 + 新库：迁移都是加表加列，旧代码的查询不认新东西但也不受影响，能跑。
- 新镜像 + 旧库：缺表，第一个写请求就 500——这不是回滚，是制造新故障。

为什么备份不能用 `cp`：SQLite 开着 WAL，最新数据可能还在 `-wal` 文件里，直接拷主文件会得到一个「少了一段最新提交」的库，而且它看起来完全正常。`Connection.backup()` 走的是官方在线备份协议。

`make check` 的第 2 层（`up → down → up` 比对 schema 指纹）保证 `down()` 是完整的，但**它不保证 `down()` 无损**——加表容易，删表就是把数据一起删掉。所以回滚只在「刚迁移完、还没产生新数据」时是安全的；跑了一段时间的库，正确做法是回镜像而不是回库。

## 已知限制

| 限制 | 说明 | 影响 |
| --- | --- | --- |
| 离线向量无排序结构 | hash 下同主题对 P50 0.147 / 不同主题对 P50 0.160——不同主题反而更相似，任何阈值都近似随机切分 | 仅作离线占位；生产用 `EMBEDDING_PROVIDER=dashscope` + 标定阈值 0.65（`docs/05`） |
| 数据量为合成样本 | 42 条，人工构造，非平台抓取 | 指标是流程验证，不是规模化结论 |
| 单语言评测 | 中英混合但未分语言算指标 | 多语言场景需分语种评估 |
| 无缓存 | 重复分析同一数据集会重复调用 | 生产需加结果缓存与批量接口 |
| 基线口径是 mock | `tests/baseline.json` 固化的 0.8108 来自离线规则基线 | 填 Key 后必须重拍基线，否则 live 指标一律判「退化」 |
| 已知规则缺口 | `died` 在关键词表却不在负面词表，五星评论里的隐性故障会误判为正面 | 已在 `tests/test_polarity.py` 标为 xfail，补词表会触发 XPASS 提醒重拍基线 |
| 门禁不是测试岗 | 门禁只能证明「没变差」，不能证明「覆盖了没见过的输入」 | 新场景靠 bad case 回流队列（P4）补进 gold set |
| 成本参数全未核实 | 11 项成本参数均为 draft，value 为空 | 内置参数不可用于计算；决策接口改由调用方提供成本档案，参数表只作为「待核实清单」 |
| FBA 费装不进单行参数 | 官方费率是四维函数（尺寸段 × 重量段 × 价格带 × 旺季），`cost_params.value` 只能存标量 | 暂由调用方按实际情景查表后填入 `fba_fee`；后续需建 `fba_fee_schedule` 维表。详见 `docs/04` |
| 合规规则全未核实 | 4 条规则全为 draft，其中 2 条 blocker | 无线充电器 × 美国当前只会返回 `NO_GO`，这是有意的 |
| 佣金未按退货比例退回 | 模型按全额计佣金，退货运费与管理费也按全额摊 | 对退货的估计**偏保守**，退货率越高真实毛利略高于模型值；已在 `assumptions` 声明 |
| 限流是固定窗口 | 每分钟一个桶，窗口边界最多可突发 2 倍额度 | 精确需要滑动窗口（按请求落行）或令牌桶；当前量级下不值得多写一张表，已在注释与本表说明 |
| 配额是软限 | 只按已记账的 token 判，正在跑的任务可能把余额用穿 | 有意取舍：中途杀掉正在跑的任务，损失大于超一点额 |
| 同步分析不记账 | `/v1/analyze` 直接烧 token 却不落 `jobs` 台账 | 它因此绕过配额，所以 live 模式下默认关闭（`SYNC_ANALYZE_ENABLED`）；生产走 `/v1/jobs` |
| 密钥无自动轮换 | 只有 `stale` 提醒，没有强制过期 | 轮换要靠流程；强制过期会在没人值守时把线上打挂，比不轮换更糟 |
| worker 是进程内的 | 单进程并发由 `JOB_CONCURRENCY` 控制，多实例靠租约避免重复执行 | 没有跨实例的公平调度；突发排队时会先到先得，不会按租户限额 |
| 结果归档要手动触发 | `jobs.py archive` 已实现（清 result、保留行与幂等键），但没有定时器 | 需要外部 cron 定期跑；不跑则 `jobs` 仍会随结果累积变大 |
| 镜像只验了两个发行方 | 本机网络对 `registry-1.docker.io` 的 TLS 握手被干扰，Docker Hub 直连不可用；改从 AWS ECR Public 的官方副本取 `public.ecr.aws/docker/library/python:3.13-slim`（即默认基础镜像），另在 `mcr.microsoft.com/devcontainers/python:3.13` 上各跑过一遍 | 两个镜像都是 Python 3.13 + Debian，`slim` 上冒烟 17/17、EXIT=0（11-19 补跑）。但只覆盖 amd64 与这两个发行方，不能表述为「各发行方均验证」 |
| 冒烟不测容量 | 只有 3 条评论、单个并发提交 | 压测与并发领取的验证在 `scripts/jobs.py` 与租约测试里，不在冒烟里 |

选择合成样本是主动决策：爬取平台前台数据违反其条款，且会让面试官质疑工程判断。数据来源合规性写在这里，而不是藏在代码里。

## 简历怎么写

> **跨境选品决策引擎**（个人项目 · 独立完成 · [仓库地址]）
>
> - **背景**：跨境选品依赖人工翻阅竞品评论，痛点发现效率低、结论主观且不可复现。
> - **方案**：设计「抽取 → 聚类 → 排序」三段式管道，用 Pydantic 契约 + 标签闭集约束 LLM 输出，实现「解析 → 错误回灌重试 → 标记降级」三级容错，将不可控的模型输出转化为可校验数据。
> - **工程**：FastAPI 分层架构（控制器/管道/契约分离），异步信号量并发调度，结构化日志贯穿 request_id；机会分采用三项加权固定公式，保证排序可解释、跨批次可比。数据层自写迁移框架（每个迁移含 `down()`，门禁跑 `up→down→up` 比对 schema 指纹），运行台账用状态机约束状态流转，成本参数以 CHECK 约束强制「来源 + 生效日期 + 核实时间」齐全，缺一不可写入。
> - **评测**：人工标注 42 条评论构建 gold set，痛点抽取 F1 达 0.81（P 0.94 / R 0.71），并定位 12 条漏检均为间接表达与中文口语，据此确定改用真实语义向量的优化路径。
> - **门禁**：因无 code review，将评审前置为自动化门禁——280 个通过用例（另有 1 个已知缺口标记为 xfail）+ 指标基线快照 + 退化阈值（F1 跌 2 点、成本超预算、失败样本集漂移即非零退出），并实测该门禁可拦截一次真实退化（F1 0.81 → 0.63）。
> - **平台化**：将同步接口改造为任务化（`POST /v1/jobs` 返回 202），幂等由 `UNIQUE(tenant_id, idempotency_key)` + 请求体指纹保证（同键换内容返回 409 而非静默复用），worker 以「租约 + 心跳」防任务永久卡死、进程被 kill 后可被回收重跑；`jobs` 管排队与幂等、`runs` 管每次执行的成本归因，重试记为新的 attempt 而不覆盖旧账。
> - **接入控制**：租户由 API Key 派生（只存 sha256、可吊销、明文仅创建时返回一次），越权访问一律 404 而非 403 以免泄漏 job_id 是否存在；限流与配额落库而非放进程内存（重启与扩容不改变语义），被拒请求同样计数以防「一直重试」绕过限流，配额直接聚合 `jobs` 台账而不另建计数器（计数器必然漂移）；限流与额度用不同的 429 code 区分，让调用方知道该退避还是该停。
> - **业务建模**：利润模型按贸易术语条件分支（DDP 不叠加关税、CIF/CFR/DAP 不叠加头程，误传参数即告警），退货影响拆为一段收入冲减 + 四段不同基数的费用，负毛利时拒绝返回 ACOS 以免「−0.20 被读成还有 20% 空间」；开发中据此测出并修掉一个「100% 退货仍算出正毛利」的建模缺陷。

**数字规则**：简历里的每个数字都必须能在面试现场用一条命令跑出来。上面这段的数字来自 `scripts/run_eval.py`，填 Key 换 live 模式后请用新结果替换。

## 下一步

1. **补齐成本参数**：佣金率需先确认类目归属（Electronics Accessories 15% vs Consumer Electronics 8%，差 7 个点）；FBA 费需建维表；关税需确认全部 Chapter 99 附加税号。查证记录见 `docs/04-成本参数口径.md`
2. **核实合规规则**：4 条规则全为 draft，其中 2 条 blocker。核实前该品类只会返回 `NO_GO`
3. ~~在启动 Docker 的机器上跑一次 `make smoke-docker`~~ **已完成（11-19）**：官方 `python:3.13-slim`
   经 AWS ECR Public 取得，17/17、EXIT=0
4. ~~接入真实 embedding 并重标定阈值~~ **已完成**：`DashScopeEmbedder` 已接入，51 条 / 120 对标定出阈值 0.65，
   同时发现「极性必须进分组键」（ARI 0.195 → 0.767）。详见 [`docs/05-聚类阈值标定.md`](docs/05-聚类阈值标定.md)
5. 扩标注集：抽取 42 条 → 200 条以上、聚类 51 条 → 200 条以上，把阈值从「区间」定到小数点后两位；
   扩到 4 个品类后指标才具备统计意义
6. `jobs.result` 归档策略与 `stale` 轮换的执行化（当前只有提醒）
