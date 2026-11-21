"""0012：把「大件」两段真正变成可报价的（枚举 + 24 行官方费率）。

只做两件事，且必须一起做：

1. **`size_tier` 的 CHECK 放开 `small_bulky`**。SQLite 不支持改 CHECK，
   只能重建表。重建手法沿用 m0007 / m0010：先把旧表**改名挪走**，再用常量
   「逐字」建新表，搬完数据再删旧表。
   *不能* 写成「建新表 → 改名成正式名」——那样 `sqlite_master` 里的 DDL
   会被改写成带双引号的表名，`schema_fingerprint` 就与「从未升级过」的库对不上。
   这条坑在 m0007 里踩过一次，这里按同样的顺序写。
2. **写入 2026 两期的大件费率**（small_bulky + large_bulky，非服装 + 危险品）。

**故意不录**：
- 2027 非旺季：官方已把 bulky 重构成 4 档 bulky + 6 档 oversize，
  与这里的枚举不是一套，硬塞会造出「名字对得上、含义对不上」的格子。
- 超大件（extra_large）：仍整段未录，超出大件即 503，不拿 bulky 顶替。

大件与标准尺寸的计费结构不同：base 含首 1 lb，其后每满 1 lb 加 $0.38，
所以 `included_oz = 16`、`increment_oz = 16`；标准尺寸那边则是「一档一个价」。
"""

from __future__ import annotations

import sqlite3

from .fba_us_bulky_rates import (
    BULKY_MAX_OZ,
    BULKY_ROW_NOTE,
    MARKET,
    SOURCE_URL,
    VERIFIED_AT,
    fee_rows,
)
from .m0009_fba_schedule import CREATED_AT
from .m0010_fba_rate_version import FBA_FEE_SCHEDULE_V10, INDEXES_V10

#: 放开 small_bulky 之后的表定义。其余列、约束、UNIQUE 与 m0010 逐字一致——
#: 重建表时「顺手把别的也改了」是这类迁移最常见的事故。
SCHEDULE_WITH_SMALL_BULKY = """
CREATE TABLE fba_fee_schedule (
    id                  INTEGER PRIMARY KEY,
    version_key         TEXT NOT NULL
                        REFERENCES fba_rate_version (version_key)
                        ON UPDATE CASCADE ON DELETE RESTRICT,
    marketplace         TEXT NOT NULL,
    size_tier           TEXT NOT NULL
                        CHECK (size_tier IN (
                            'small_standard', 'large_standard',
                            'small_bulky', 'large_bulky', 'extra_large'
                        )),
    product_class       TEXT NOT NULL
                        CHECK (product_class IN (
                            'non_apparel', 'apparel', 'dangerous_goods'
                        )),
    weight_min_oz       REAL NOT NULL CHECK (weight_min_oz >= 0),
    weight_max_oz       REAL CHECK (weight_max_oz IS NULL OR weight_max_oz >= 0),
    price_band          TEXT NOT NULL
                        CHECK (price_band IN ('under_10', '10_to_50', 'over_50')),
    base_fee_usd        REAL NOT NULL CHECK (base_fee_usd >= 0),
    per_increment_usd   REAL NOT NULL DEFAULT 0 CHECK (per_increment_usd >= 0),
    increment_oz        REAL CHECK (increment_oz IS NULL OR increment_oz > 0),
    included_oz         REAL CHECK (included_oz IS NULL OR included_oz >= 0),
    source_url          TEXT,
    verified_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'verified')),
    note                TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (version_key, size_tier, product_class, price_band, weight_min_oz),
    CHECK (weight_max_oz IS NULL OR weight_max_oz > weight_min_oz),
    CHECK (per_increment_usd = 0 OR increment_oz IS NOT NULL),
    CHECK (per_increment_usd > 0 OR increment_oz IS NULL),
    CHECK (
        status <> 'verified' OR (
            source_url IS NOT NULL AND verified_at IS NOT NULL
        )
    )
)
"""

_COLUMNS = (
    "id, version_key, marketplace, size_tier, product_class, weight_min_oz, "
    "weight_max_oz, price_band, base_fee_usd, per_increment_usd, increment_oz, "
    "included_oz, source_url, verified_at, status, note, created_at"
)

INSERT = (
    "INSERT INTO fba_fee_schedule "
    "(version_key, marketplace, size_tier, product_class, weight_min_oz, weight_max_oz, "
    " price_band, base_fee_usd, per_increment_usd, increment_oz, included_oz, status, "
    " source_url, verified_at, note, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?, ?)"
)


def up(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE fba_fee_schedule RENAME TO fba_fee_schedule_pre_v12")
    conn.execute(SCHEDULE_WITH_SMALL_BULKY)
    conn.execute(
        f"INSERT INTO fba_fee_schedule ({_COLUMNS}) "
        f"SELECT {_COLUMNS} FROM fba_fee_schedule_pre_v12"
    )
    conn.execute("DROP TABLE fba_fee_schedule_pre_v12")
    for statement in INDEXES_V10.values():
        conn.execute(statement)

    conn.executemany(
        INSERT,
        [
            (version, MARKET, tier, product_class, lo, hi, band, fee, per_inc, inc,
             included, SOURCE_URL, VERIFIED_AT, BULKY_ROW_NOTE, CREATED_AT)
            for version, product_class, tier, lo, hi, band, fee, per_inc, inc, included
            in fee_rows()
        ],
    )


def down(conn: sqlite3.Connection) -> None:
    # 先删掉本迁移新增的行，否则还原 schema 时 CHECK 会拒绝 small_bulky 的行。
    # large_bulky 的行也是本迁移加的，一并删掉。
    conn.execute(
        "DELETE FROM fba_fee_schedule WHERE size_tier IN ('small_bulky', 'large_bulky')"
    )
    conn.execute("ALTER TABLE fba_fee_schedule RENAME TO fba_fee_schedule_pre_v11")
    # 直接引用 m0010 的常量，而不是在本文件再抄一遍：m0010 已冻结，
    # 引用它才能保证「回滚后与从未升级到 v12 的库逐字一致」。
    conn.execute(FBA_FEE_SCHEDULE_V10)
    conn.execute(
        f"INSERT INTO fba_fee_schedule ({_COLUMNS}) "
        f"SELECT {_COLUMNS} FROM fba_fee_schedule_pre_v11"
    )
    conn.execute("DROP TABLE fba_fee_schedule_pre_v11")
    for statement in INDEXES_V10.values():
        conn.execute(statement)


__all__ = ["BULKY_MAX_OZ", "SCHEDULE_WITH_SMALL_BULKY", "down", "up"]
