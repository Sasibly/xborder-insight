"""0004：任务化（jobs）+ 幂等键 + 单次执行台账。

为什么要在 `runs` 之外再建一张 `jobs`：
    两者回答的问题不同。
    runs —— 这次执行**花了多少钱**（一次 job 重试两次 → 两条 run，因为真的花了两次钱）
    jobs —— 这次请求**排到哪了**（幂等、排队、卡死恢复、能否重试）

把两者合并成一张表是常见的偷懒做法，后果是「重试」会污染成本口径：
要么把重试当同一次执行（少算钱），要么把重试当新任务（多算一次幂等）。

三条硬约束，都写在库里而不是代码里：
1. `UNIQUE(tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL`
   幂等靠唯一索引，不靠「先查再插」——后者在并发下必然漏。
   用部分索引而非普通唯一索引：没带幂等键的请求不该互相碰撞，而 SQLite 的
   唯一索引里 NULL 彼此不相等，所以必须显式排除，否则语义靠 NULL 的巧合。
2. `CHECK (status <> 'running' OR lease_expires_at IS NOT NULL)`
   running 必须有租约。没有租约的 running 就是「永久卡死」——重启后没人知道
   它是活着还是死了，只能人工修。
3. `CHECK (attempts <= max_attempts)`
   重试次数上限由库保证。代码里数错一次，代价是无限重试烧钱。

`runs.job_id + attempt` 上的部分唯一索引同样重要：它保证「同一次尝试只能记一次账」。
"""
from __future__ import annotations

import sqlite3

JOBS = """
CREATE TABLE jobs (
    job_id              TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL DEFAULT 'local' CHECK (length(tenant_id) > 0),
    kind                TEXT NOT NULL CHECK (kind IN ('analyze', 'decision')),
    status              TEXT NOT NULL
                        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),

    -- 幂等键由调用方给（Idempotency-Key 头）。键相同但请求体不同 → 拒绝，不能复用。
    idempotency_key     TEXT CHECK (idempotency_key IS NULL OR length(idempotency_key) BETWEEN 1 AND 128),
    -- 请求体的规范化指纹：用来区分「同一个请求重发」和「同一个键换了内容」
    request_fingerprint TEXT NOT NULL,
    payload             TEXT NOT NULL,

    result              TEXT,
    error_code          TEXT,

    attempts            INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts        INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),

    -- 租约：worker 领任务时写入，心跳续期。过期即可被别人重新领取
    worker_id           TEXT,
    lease_expires_at    TEXT,

    -- 版本三元组：同一三元组重跑必须得到同一结果，否则「可复现」是句空话
    prompt_version      TEXT NOT NULL,
    model               TEXT NOT NULL,
    taxonomy_version    TEXT NOT NULL,

    llm_calls           INTEGER NOT NULL DEFAULT 0 CHECK (llm_calls >= 0),
    prompt_tokens       INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens   INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),

    created_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,

    -- 状态与字段的一致性：这些约束的作用是让「脏数据」写不进来，
    -- 而不是指望每个写入方都记得清理这些字段。
    CHECK (status <> 'running' OR (started_at IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK (status <> 'queued' OR (started_at IS NULL AND finished_at IS NULL)),
    CHECK (status NOT IN ('succeeded', 'failed') OR finished_at IS NOT NULL),
    CHECK (status = 'succeeded' OR result IS NULL),
    CHECK (status <> 'failed' OR error_code IS NOT NULL),
    CHECK (attempts <= max_attempts)
)
"""

JOBS_INDEXES: dict[str, str] = {
    # 幂等的唯一性来源。tenant_id 放在前面，是为了 P3b 接入 API Key 后
    # 不同租户可以用相同的键而不互相影响。
    "uq_jobs_idem": (
        "CREATE UNIQUE INDEX uq_jobs_idem ON jobs (tenant_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    ),
    "idx_jobs_queue": "CREATE INDEX idx_jobs_queue ON jobs (status, created_at)",
    "idx_jobs_tenant": "CREATE INDEX idx_jobs_tenant ON jobs (tenant_id, created_at DESC)",
}

# runs 增补：把「哪次任务的第几次尝试」记到台账上。
# 写成「列名 → DDL」而不是裸列表：down() 要按列名删，从 DDL 里反解列名
# 会拿到类型声明（TEXT），那是上一版索引名解析踩过的同一个坑。
RUNS_COLUMNS: dict[str, str] = {
    "job_id": "ALTER TABLE runs ADD COLUMN job_id TEXT",
    # 老数据与直接调用（不经任务）的 run 记为第 1 次尝试
    "attempt": "ALTER TABLE runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1",
}

RUNS_INDEXES: dict[str, str] = {
    # 同一次尝试只能有一条台账：重试不会把上一次的 token 再记一遍。
    # 注意这**不等于**「重试不计费」——重试真的又调了一次模型，就该再记一笔，
    # 但必须是新的一条，而不是把同一条覆盖或复制。
    "uq_runs_attempt": (
        "CREATE UNIQUE INDEX uq_runs_attempt ON runs (job_id, attempt) "
        "WHERE job_id IS NOT NULL"
    ),
    "idx_runs_job": (
        "CREATE INDEX idx_runs_job ON runs (job_id) WHERE job_id IS NOT NULL"
    ),
}


def up(conn: sqlite3.Connection) -> None:
    conn.execute(JOBS)
    for statement in JOBS_INDEXES.values():
        conn.execute(statement)
    for statement in RUNS_COLUMNS.values():
        conn.execute(statement)
    for statement in RUNS_INDEXES.values():
        conn.execute(statement)


def down(conn: sqlite3.Connection) -> None:
    for name in reversed(list(RUNS_INDEXES)):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    # 先删索引再删列：SQLite 不允许删掉被索引引用的列。
    # 逆序执行，才是 up() 的严格逆操作。
    for column in reversed(list(RUNS_COLUMNS)):
        conn.execute(f"ALTER TABLE runs DROP COLUMN {column}")
    for name in reversed(list(JOBS_INDEXES)):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS jobs")
