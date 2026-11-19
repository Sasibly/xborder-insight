"""0005：API Key 与租户限流。

两张表回答两个不同的问题：

    api_keys    —— 你是谁（tenant 从密钥派生，客户端不能自己声明）
    rate_limits —— 你这一分钟敲了几次门

三个刻意的选择：

1. **只存哈希，不存明文**
   `key_hash` 是主键，明文只在创建时返回一次，之后无法再取出。
   「能不能查回明文」是判断一套密钥管理是否合格的唯一标准。

2. **不加盐、不用慢哈希**
   慢哈希（bcrypt/argon2）是为**低熵**口令设计的，用来抵消字典攻击。
   API Key 是 256 位随机串，暴力枚举不可行，而按哈希做等值查询又必须是 O(1)——
   逐行加盐会让每次鉴权退化成全表扫描。选错哈希的代价是性能，不是安全。

3. **限流计数落库，不放进程内存**
   放内存的实现有两个说不出口的问题：进程一重启配额度就重置；
   多实例部署时每个实例各算一份，实际限流是标称值的 N 倍。
   落库只多一次 UPSERT，换来「重启与扩容都不改变语义」。
"""
from __future__ import annotations

import sqlite3

API_KEYS = """
CREATE TABLE api_keys (
    -- sha256 十六进制，64 字符。主键即查询路径：鉴权不做全表扫描
    key_hash          TEXT PRIMARY KEY CHECK (length(key_hash) = 64),
    -- 前若干位明文，只用于「这是哪把钥匙」的人工辨认，不足以还原密钥
    key_prefix        TEXT NOT NULL CHECK (length(key_prefix) BETWEEN 4 AND 16),

    tenant_id         TEXT NOT NULL CHECK (length(tenant_id) > 0),
    label             TEXT NOT NULL DEFAULT '',

    -- NULL = 不限额度。用 NULL 而不是 0：0 表示「一条都不许」，
    -- 两者语义差得远，用同一个值表达会让「没设额度」变成「禁止使用」。
    daily_token_quota INTEGER CHECK (daily_token_quota IS NULL OR daily_token_quota > 0),
    rpm_limit         INTEGER NOT NULL DEFAULT 60 CHECK (rpm_limit BETWEEN 1 AND 100000),

    created_at        TEXT NOT NULL,
    revoked_at        TEXT,
    last_used_at      TEXT,

    -- 吊销时间不可能早于创建时间。这条约束挡住的是「批量迁移数据时字段错位」
    -- 这类事故——它不会报错，只会让吊销静默失效。
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
)
"""

API_KEYS_INDEXES: dict[str, str] = {
    "idx_api_keys_tenant": "CREATE INDEX idx_api_keys_tenant ON api_keys (tenant_id, created_at DESC)",
}

# 固定窗口限流。表很小：只保留当前窗口与少量历史，读取时顺手清理。
RATE_LIMITS = """
CREATE TABLE rate_limits (
    tenant_id    TEXT NOT NULL,
    window_start TEXT NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 0 CHECK (hits >= 0),
    PRIMARY KEY (tenant_id, window_start)
)
"""


def up(conn: sqlite3.Connection) -> None:
    conn.execute(API_KEYS)
    for statement in API_KEYS_INDEXES.values():
        conn.execute(statement)
    conn.execute(RATE_LIMITS)


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS rate_limits")
    for name in reversed(list(API_KEYS_INDEXES)):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS api_keys")
