"""0001：评论与运行台账。

两张表回答两个不同问题：
  reviews —— 输入是什么（去重口径与 ingest.py 保持一致，避免两套逻辑打架）
  runs    —— 这次分析发生了什么（成本、解析率、参数快照），是成本归因的载体

两条刻意的设计：
1. `reviews` 的唯一索引建在 (dataset, content_hash) 上，与 `parse_reviews()`
   的「正文指纹去重」口径完全一致。两处口径不一致是最难查的一类 bug。
2. `runs.params_snapshot` 存 JSON。参数会变，但历史 run 必须能回答
   「当时用的是哪一版费率」——否则审计时数字对不上账。
"""

from __future__ import annotations

import sqlite3

REVIEWS = """
CREATE TABLE reviews (
    review_id     TEXT PRIMARY KEY,
    dataset       TEXT NOT NULL,
    asin          TEXT NOT NULL,
    rating        INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    title         TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL CHECK (length(body) > 0),
    lang          TEXT,
    content_hash  TEXT NOT NULL,
    ingested_at   TEXT NOT NULL
)
"""

# 索引写成「名字 → DDL」而不是裸字符串列表：
# up 时按值建，down 时按名字删。名字显式写在左边，就不可能和 DDL 里的名字不一致，
# 也不需要从 DDL 里反解名字（`CREATE UNIQUE INDEX` 会让按空格切分的方式错位）。
REVIEWS_INDEXES: dict[str, str] = {
    "idx_reviews_dataset": "CREATE INDEX idx_reviews_dataset ON reviews (dataset, ingested_at DESC)",
    "uq_reviews_content": "CREATE UNIQUE INDEX uq_reviews_content ON reviews (dataset, content_hash)",
}

RUNS = """
CREATE TABLE runs (
    run_id              TEXT PRIMARY KEY,
    dataset             TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    review_count        INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0),
    parsed_count        INTEGER NOT NULL DEFAULT 0 CHECK (parsed_count >= 0),
    parse_success_rate  REAL CHECK (parse_success_rate BETWEEN 0 AND 1),
    llm_calls           INTEGER NOT NULL DEFAULT 0 CHECK (llm_calls >= 0),
    prompt_tokens       INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens   INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    latency_p95_ms      REAL,
    request_fingerprint TEXT,
    params_snapshot     TEXT,
    error_code          TEXT,
    -- 终态必须有结束时间，运行中必须没有：状态与字段的一致性由库保证，而非应用层自觉
    CHECK (status = 'running' OR finished_at IS NOT NULL)
)
"""

RUNS_INDEXES: dict[str, str] = {
    "idx_runs_dataset": "CREATE INDEX idx_runs_dataset ON runs (dataset, started_at DESC)",
    # 非唯一索引：P3 做幂等时再升级为 UNIQUE，这里先留出查询入口
    "idx_runs_fingerprint": "CREATE INDEX idx_runs_fingerprint ON runs (request_fingerprint)",
}


def up(conn: sqlite3.Connection) -> None:
    conn.execute(REVIEWS)
    for statement in REVIEWS_INDEXES.values():
        conn.execute(statement)
    conn.execute(RUNS)
    for statement in RUNS_INDEXES.values():
        conn.execute(statement)


def down(conn: sqlite3.Connection) -> None:
    # 先删索引再删表。SQLite 会自动清理索引，但显式写出来才能证明
    # down() 是 up() 的严格逆操作，而不是「差不多清空了」。
    for name in reversed(list(RUNS_INDEXES)):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS runs")
    for name in reversed(list(REVIEWS_INDEXES)):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS reviews")
