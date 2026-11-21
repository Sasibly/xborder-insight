"""0007：给成本参数加适用区间（applies_min / applies_max）。

为什么需要这两列：
    docs/04 第 5 节把这条列为「当前最该补的不足」——
    费率是函数不是常数，而适用区间原先只写在 note 的自然语言里，机器读不到。
    于是售价 $150 的商品照样按「$100 以下 15%」算佣金，不报错、不警告，
    结论看起来完全正常。这是 ②类错误的标准形状：
    系统在工作，答案是错的，页面上看不出来。

为什么不拆成「一个分段一行」：
    表上有 UNIQUE (param_key, marketplace)，一个 key 只能有一行。
    分段费率（$100 以下 15%、以上 8%）靠加行表达不了。
    所以这里的语义是**断言式**的：这一行存的值只在 [applies_min, applies_max]
    内成立；越界就报错，逼调用方补该区间的参数，而不是让它静默用错。
    真正的分段计算仍留在模型里（如 `profit.py` 的退款管理费封顶）。

为什么重建表而不是 ADD COLUMN：
    ADD COLUMN 带得动单列 CHECK，但带不动跨列 CHECK（applies_min <= applies_max）。
    而「区间写反」一旦落库，任何取值都会被判越界——虽然也是立刻暴露，
    但没有理由留这个口子。重建一次能把两个单列 CHECK 和一条表级 CHECK
    全放进去，且约束对存量行同样生效。

为什么 down() 也是重建：
    SQLite 的 DROP COLUMN 要求该列不被任何约束引用，两列都带 CHECK，
    直接删会失败。重建是唯一能做到「up → down → up 结果完全一致」的路径。

列全部可空：不是所有参数都有适用区间（如 `returns.rate` 就是单一比率）。
NULL 表示「不声明区间」，此时 require() 不做区间校验，行为与 0006 之前一致。
"""

from __future__ import annotations

import sqlite3

# 与 m0002 逐字一致，只在 created_at 之后多两列。
# 刻意不 import m0002 的常量再拼接：已发布的迁移是冻结的，重建表的 schema
# 必须自包含，否则将来 m0002 有任何改动，m0007 的回滚行为会跟着漂移。
COST_PARAMS_WITH_RANGE = """
CREATE TABLE {table} (
    id              INTEGER PRIMARY KEY,
    param_key       TEXT NOT NULL,
    marketplace     TEXT NOT NULL,
    category        TEXT,
    unit            TEXT NOT NULL
                    CHECK (unit IN ('ratio', 'usd', 'usd_per_unit', 'usd_per_cubic_foot')),
    value           REAL,
    source_url      TEXT,
    effective_date  TEXT,
    verified_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'verified')),
    note            TEXT,
    created_at      TEXT NOT NULL,
    applies_min     REAL CHECK (applies_min IS NULL OR applies_min >= 0),
    applies_max     REAL CHECK (applies_max IS NULL OR applies_max >= 0),
    UNIQUE (param_key, marketplace),
    CHECK (
        status <> 'verified' OR (
            value IS NOT NULL AND source_url IS NOT NULL
            AND effective_date IS NOT NULL AND verified_at IS NOT NULL
        )
    ),
    CHECK (
        applies_min IS NULL OR applies_max IS NULL OR applies_min <= applies_max
    )
)
"""

# down() 用的还原 schema：m0002 原文，一字不改
COST_PARAMS_WITHOUT_RANGE = """
CREATE TABLE {table} (
    id              INTEGER PRIMARY KEY,
    param_key       TEXT NOT NULL,
    marketplace     TEXT NOT NULL,
    category        TEXT,
    unit            TEXT NOT NULL
                    CHECK (unit IN ('ratio', 'usd', 'usd_per_unit', 'usd_per_cubic_foot')),
    value           REAL,
    source_url      TEXT,
    effective_date  TEXT,
    verified_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'verified')),
    note            TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (param_key, marketplace),
    CHECK (
        status <> 'verified' OR (
            value IS NOT NULL AND source_url IS NOT NULL
            AND effective_date IS NOT NULL AND verified_at IS NOT NULL
        )
    )
)
"""

BASE_COLUMNS = (
    "id, param_key, marketplace, category, unit, value, source_url, "
    "effective_date, verified_at, status, note, created_at"
)

# 索引名沿用 m0002，保证 up → down → up 后索引名不变
INDEXES: dict[str, str] = {
    "idx_cost_status": "CREATE INDEX idx_cost_status ON cost_params (status, marketplace)",
}


def up(conn: sqlite3.Connection) -> None:
    # 顺序不能换成「建新表 → 复制 → drop 旧表 → rename」：
    # ALTER TABLE ... RENAME 会重写 sqlite_master 里的 DDL 文本（表名被加上双引号），
    # 于是 schema_fingerprint 与「从未升级到本版本」的库不一致，
    # test_downgrade_one_step_equals_never_having_upgraded 会正确地报错。
    # 先把旧表挪走、再按原文建新表，最终 DDL 就与常量逐字一致。
    conn.execute("ALTER TABLE cost_params RENAME TO cost_params_pre_v7")
    conn.execute(COST_PARAMS_WITH_RANGE.format(table="cost_params"))
    conn.execute(
        f"INSERT INTO cost_params ({BASE_COLUMNS}, applies_min, applies_max) "
        f"SELECT {BASE_COLUMNS}, NULL, NULL FROM cost_params_pre_v7"
    )
    conn.execute("DROP TABLE cost_params_pre_v7")
    for statement in INDEXES.values():
        conn.execute(statement)


def down(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE cost_params RENAME TO cost_params_pre_v6")
    conn.execute(COST_PARAMS_WITHOUT_RANGE.format(table="cost_params"))
    conn.execute(
        f"INSERT INTO cost_params ({BASE_COLUMNS}) SELECT {BASE_COLUMNS} FROM cost_params_pre_v6"
    )
    conn.execute("DROP TABLE cost_params_pre_v6")
    for statement in INDEXES.values():
        conn.execute(statement)
