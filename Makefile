# 质量门禁入口。
#
# 为什么用 .RECIPEPREFIX 而不是传统的 TAB：
# TAB 不可见，编辑器/复制粘贴经常把它换成空格，报错 "missing separator"
# 只在别人机器上出现。这里把配方前缀改成可见的 '>'，让这类问题在 diff 里就能看出来。
.RECIPEPREFIX := >

ifeq ($(OS),Windows_NT)
  PY ?= ./.venv/Scripts/python.exe
else
  PY ?= ./.venv/bin/python
endif

PORT ?= 8018
TENANT ?= local
QUOTA ?= 200000
RPM ?= 60
TAG ?= 0.5.0
TO ?= 0
BASE_IMAGE ?=
DAYS ?= 30
YES ?=

.DEFAULT_GOAL := check
.PHONY: check test db db-up db-status db-params eval baseline show serve demo jobs jobs-drain jobs-recover jobs-archive key-create keys key-revoke smoke smoke-docker docker-build rollback backup calibrate

## check: 全量门禁 = 测试 + 迁移可逆性 + 评测 + 基线退化比对
check:
>$(PY) scripts/check.py

## test: 只跑测试
test:
>$(PY) -m pytest

## db: 迁移可逆性校验（临时库，不碰真库）
db:
>$(PY) scripts/check.py --db-only

## db-up: 升级真实数据库到最新
db-up:
>$(PY) scripts/db.py up

## db-status: 查看迁移版本
db-status:
>$(PY) scripts/db.py status

## db-params: 参数核实进度（当前应全部为待核实）
db-params:
>$(PY) scripts/db.py params

## eval: 只跑评测并比对基线（F1 掉超 2 个点即非零退出）
eval:
>$(PY) scripts/check.py --eval-only

## baseline: 显式刷新基线快照（等于宣布接受当前指标）
baseline:
>$(PY) scripts/check.py --update-baseline

## show: 打印当前基线，不跑任何东西
show:
>$(PY) scripts/check.py --show

## serve: 起服务
serve:
>$(PY) -m uvicorn xborder_insight.api:app --app-dir src --port $(PORT)

## demo: 跑一次离线演示
demo:
>$(PY) scripts/demo.py

## jobs: 队列快照 + 成本归因
jobs:
>$(PY) scripts/jobs.py status

## jobs-drain: 在本进程把待跑任务处理完
jobs-drain:
>$(PY) scripts/jobs.py drain

## jobs-recover: 回收租约过期的任务（进程被强杀后用）
jobs-recover:
>$(PY) scripts/jobs.py recover

## jobs-archive: 归档 $(DAYS) 天前完成的任务结果；默认只预演，加 YES=1 才执行
jobs-archive:
>$(PY) scripts/jobs.py archive --days $(DAYS) $(if $(YES),--yes,)

## key-create: 新建 API Key（明文只打印一次，当场抄走）
key-create:
>$(PY) scripts/keys.py create --tenant $(TENANT) --quota $(QUOTA) --rpm $(RPM)

## keys: 列出密钥与状态
keys:
>$(PY) scripts/keys.py list

## key-revoke: 吊销密钥，用法 make key-revoke PREFIX=xb_abcd1234
key-revoke:
>$(PY) scripts/keys.py revoke --prefix $(PREFIX)

## smoke: 交付冒烟 = 临时库 + 临时端口 + 建密钥 + 提交任务 + 断言（15 项）
smoke:
>$(PY) scripts/smoke.py

## smoke-docker: 构建镜像并起容器冒烟（含非 root 与 HEALTHCHECK 断言）
## BASE_IMAGE=... 可换基础镜像（Docker Hub 不可达时用同版本 Python 镜像顶上）
smoke-docker:
>$(PY) scripts/smoke.py --target docker $(if $(BASE_IMAGE),--base-image $(BASE_IMAGE),)

## docker-build: 只构建镜像，不跑冒烟
docker-build:
>docker build -t xborder-insight:$(TAG) .

## backup: 在线备份数据库（走 sqlite backup API，可在服务运行时执行）
backup:
>$(PY) -c "import sqlite3,datetime,os; p='data/app.sqlite3'; d='data/backup-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3'; s=sqlite3.connect(p); t=sqlite3.connect(d); s.backup(t); s.close(); t.close(); print('已备份 →', d, os.path.getsize(d), 'bytes')"

## rollback: 回滚 schema 到指定版本，用法 make rollback TO=3（破坏性）
rollback:
>$(PY) scripts/db.py down --to $(TO) --yes

## calibrate: 聚类阈值标定（hash 离线；dashscope 需 DASHSCOPE_API_KEY，向量走缓存）
calibrate:
>$(PY) scripts/calibrate_cluster.py

## calibrate-hash: 只跑离线向量，强制重算（改了 HashEmbedder 之后用）
calibrate-hash:
>$(PY) scripts/calibrate_cluster.py --provider hash --no-cache
