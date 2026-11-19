#!/bin/sh
# 容器入口：先迁移，再启动。顺序不可交换。
#
# 为什么不让应用启动时自动迁移：
#   自动迁移意味着「任何一个跑起来的进程都可能改库结构」。单实例时这只是风格问题，
#   多实例时就是两个进程同时往同一个库上跑 DDL——SQLite 会靠锁串行化，但你要为
#   「为什么启动慢」和「为什么偶发 schema 冲突」各写一份解释。
#   放进入口脚本，迁移的时机就只有「容器启动」这一个，能一眼看全。
#
# 迁移失败时脚本退出、容器退出、编排层重启它。这是对的：
#   库结构没到位就接流量，故障会延迟到第一个写请求才出现，那时的错误信息里
#   不会有「你忘了迁移」这句话。
set -eu

DB_PATH="${DB_PATH:-data/app.sqlite3}"
APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8000}"

echo "[entrypoint] run_mode=${RUN_MODE:-mock} auth_required=${AUTH_REQUIRED:-true} db=${DB_PATH}"
echo "[entrypoint] 迁移数据库…"
python scripts/db.py up

# exec：把 PID 1 让给 uvicorn，SIGTERM 才能直达进程组。
# 少了 exec，docker stop 会等满 10 秒超时再 SIGKILL，正在跑的任务不会被优雅收尾。
#
# --no-access-log：应用自己有 request.done 结构化日志（带 request_id 与耗时），
# uvicorn 的访问日志会为每个请求再打一行，除了把日志体积翻倍没有别的用处。
echo "[entrypoint] 启动 uvicorn :${APP_PORT}"
exec uvicorn xborder_insight.api:app \
  --app-dir src \
  --host "${APP_HOST}" \
  --port "${APP_PORT}" \
  --workers 1 \
  --no-access-log
