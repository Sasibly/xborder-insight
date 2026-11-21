"""0010：FBA 费率加「版本」与「产品类别」两维。

要解决的两个静默错误：

1. 【时段枚举装不下跨年的费率期】
   m0009 用 `season IN ('off_peak','peak')` 表达时段。但官方费率期是
   2026 非旺季（1/15–10/14）、2026 旺季（10/15–次年 1/14）、
   2027 非旺季（2027-01-15–2027-10-14），而 2027 旺季官方尚未发布。
   单靠 season='peak' 无法区分「2026 旺季」与「尚未发布的 2027 旺季」——
   2027-12-01 发货的报价会命中 2026 旺季那一行，价格看起来正常，
   实际用了一张已经过期的表。这正是本项目定义里最贵的一类错（②类）。
   `effective_date` 列当时就存在，但 `pick_row()` 完全不看它，
   等于给了一个「看起来能追溯」的字段。

2. 【官方费率分三套，表里只有一套】
   非服装 / 服装 / 危险品是三张独立费率表，同一档位差 10–25%。
   无线充电器与扫地机若按非服装口径报价，每件少算约 $0.8。
   表里没有 product_class 这一列，就只能填一套、然后假装其余不存在。

结构上的两个决定：

* 【版本独立成表，而不是在行上重复起止日期】
  同一个费率期里有 126 行，若每行都存 effective_from/to，改一次区间要改
  126 行，且漏改一行就得到「同一版本内两个生效期」这种无法解释的状态。
  独立成 `fba_rate_version` 后，区间只有一处，行只引用 version_key（带外键）。

* 【行上的 season 与 effective_date 一并删除，不保留作「冗余便利」】
  版本已经承载时段与区间，行上再留一份就是两个真相源：行说 peak、
  版本区间落在 6 月，任何一方单独看都正常，凑在一起才算得出错。
  宁可多一次 join，也不要一个会漂移的字段。行上只保留
  「这个数字出自哪一页」的来源（source_url / verified_at / status）。

为什么重建表而不是 ADD COLUMN：
  要加的是两个 NOT NULL 列 + 外键 + 改写 UNIQUE 约束。SQLite 的
  ADD COLUMN 加不了 NOT NULL（无默认值）列、改不了 UNIQUE、也加不了外键。
  重建是唯一路径；down() 同样是重建，以还原 m0009 的逐字结构。

重建的手法沿用 m0007：先把旧表改名挪走，再按常量「逐字」建新表。
直接 CREATE new + RENAME 会让 sqlite_master 里的 DDL 被重写（表名加双引号），
schema_fingerprint 就与「从未升级过」的库不一致——这条坑在 m0007 里踩过。
"""

from __future__ import annotations

import sqlite3

CREATED_AT = "2026-11-21T00:00:00+08:00"  # 迁移内不读时钟，保证可重复执行
MARKET = "US"

# 只登记三个费率期，不预置 2027 旺季（10/15/2027 起）——
# 官方费率页未发布该期，凭空写一个区间会让「缺版本」这件事消失。
# 状态一律 draft：区间是官方写的，但「这一期数值已核对」是 0011 的事。
SEED_VERSIONS: tuple[tuple[str, str, str, str], ...] = (
    ("us_2026_off_peak", "off_peak", "2026-01-15", "2026-10-14"),
    ("us_2026_peak", "peak", "2026-10-15", "2027-01-14"),
    ("us_2027_off_peak", "off_peak", "2027-01-15", "2027-10-14"),
)

VERSION_NOTE = (
    "区间取官方费率页各表的表头口径：非旺季 1/15–10/14，旺季 10/15–次年 1/14。"
    "2027 旺季（10/15/2027 起）官方未发布，故意不登记——"
    "该日期之后的报价应报「缺版本」，而不是借用 2026 旺季费率。"
)

FBA_RATE_VERSION = """
CREATE TABLE fba_rate_version (
    version_key     TEXT PRIMARY KEY,
    marketplace     TEXT NOT NULL,
    season          TEXT NOT NULL CHECK (season IN ('off_peak', 'peak')),
    effective_from  TEXT NOT NULL CHECK (effective_from LIKE '____-__-__'),
    effective_to    TEXT NOT NULL CHECK (effective_to LIKE '____-__-__'),
    source_url      TEXT,
    verified_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'verified')),
    note            TEXT,
    created_at      TEXT NOT NULL,
    -- 区间写反会让这一期永远匹配不到任何发货日期。看起来是「缺数据」，
    -- 实际是「数据写错」，两种原因的修法完全不同，所以在库层面就拦掉。
    CHECK (effective_to >= effective_from),
    CHECK (
        status <> 'verified' OR (
            source_url IS NOT NULL AND verified_at IS NOT NULL
        )
    )
)
"""

FBA_FEE_SCHEDULE_V10 = """
CREATE TABLE fba_fee_schedule (
    id                  INTEGER PRIMARY KEY,
    version_key         TEXT NOT NULL
                        REFERENCES fba_rate_version (version_key)
                        ON UPDATE CASCADE ON DELETE RESTRICT,
    marketplace         TEXT NOT NULL,
    size_tier           TEXT NOT NULL
                        CHECK (size_tier IN (
                            'small_standard', 'large_standard',
                            'large_bulky', 'extra_large'
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
    -- 同一版本、同一尺寸段、同一产品类别、同一价格带下，同一个重量起点只能有一行。
    -- 这仍拦不住区间重叠（[0,4] 与 [2,6] 起点不同），重叠只能靠 audit() 检出：
    -- SQLite 的 CHECK 不能跨行查询。这是本表与 m0009 一样的已知边界。
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

# down() 用的还原 schema：m0009 原文，一字不改（含 season 与行级 effective_date）。
FBA_FEE_SCHEDULE_V9 = """
CREATE TABLE fba_fee_schedule (
    id                  INTEGER PRIMARY KEY,
    marketplace         TEXT NOT NULL,
    size_tier           TEXT NOT NULL
                        CHECK (size_tier IN (
                            'small_standard', 'large_standard',
                            'large_bulky', 'extra_large'
                        )),
    weight_min_oz       REAL NOT NULL CHECK (weight_min_oz >= 0),
    weight_max_oz       REAL CHECK (weight_max_oz IS NULL OR weight_max_oz >= 0),
    price_band          TEXT NOT NULL
                        CHECK (price_band IN ('under_10', '10_to_50', 'over_50')),
    season              TEXT NOT NULL
                        CHECK (season IN ('off_peak', 'peak')),
    base_fee_usd        REAL NOT NULL CHECK (base_fee_usd >= 0),
    per_increment_usd   REAL NOT NULL DEFAULT 0 CHECK (per_increment_usd >= 0),
    increment_oz        REAL CHECK (increment_oz IS NULL OR increment_oz > 0),
    included_oz         REAL CHECK (included_oz IS NULL OR included_oz >= 0),
    source_url          TEXT,
    effective_date      TEXT,
    verified_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'verified')),
    note                TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (marketplace, size_tier, price_band, season, weight_min_oz),
    CHECK (weight_max_oz IS NULL OR weight_max_oz > weight_min_oz),
    CHECK (per_increment_usd = 0 OR increment_oz IS NOT NULL),
    CHECK (per_increment_usd > 0 OR increment_oz IS NULL),
    CHECK (
        status <> 'verified' OR (
            source_url IS NOT NULL
            AND effective_date IS NOT NULL
            AND verified_at IS NOT NULL
        )
    )
)
"""

# 索引名沿用 m0009，保证 up → down → up 后索引名不变（DDL 内容会随列集变）
INDEXES_V10: dict[str, str] = {
    "idx_fba_lookup": (
        "CREATE INDEX idx_fba_lookup ON fba_fee_schedule "
        "(marketplace, version_key, size_tier, product_class, price_band, weight_min_oz)"
    ),
}

INDEXES_V9: dict[str, str] = {
    "idx_fba_lookup": (
        "CREATE INDEX idx_fba_lookup ON fba_fee_schedule "
        "(marketplace, size_tier, price_band, season, weight_min_oz)"
    ),
}

V9_COLUMNS = (
    "id, marketplace, size_tier, weight_min_oz, weight_max_oz, price_band, season, "
    "base_fee_usd, per_increment_usd, increment_oz, included_oz, source_url, "
    "effective_date, verified_at, status, note, created_at"
)


def up(conn: sqlite3.Connection) -> None:
    # 版本表必须先建：下面的新表带外键指向它，且回填时要引用 us_2026_off_peak。
    conn.execute(FBA_RATE_VERSION)
    conn.executemany(
        "INSERT INTO fba_rate_version "
        "(version_key, marketplace, season, effective_from, effective_to, "
        " status, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)",
        [(key, MARKET, season, start, end, VERSION_NOTE, CREATED_AT)
         for key, season, start, end in SEED_VERSIONS],
    )

    conn.execute("ALTER TABLE fba_fee_schedule RENAME TO fba_fee_schedule_pre_v10")
    conn.execute(FBA_FEE_SCHEDULE_V10)
    # 存量 12 行全部是 large_standard / 非旺季 / 非服饰，因此回填口径是确定的：
    # 挂到 us_2026_off_peak + non_apparel，状态保持 draft（数值核对在 0011）。
    conn.execute(
        "INSERT INTO fba_fee_schedule "
        "(version_key, marketplace, size_tier, product_class, weight_min_oz, weight_max_oz, "
        " price_band, base_fee_usd, per_increment_usd, increment_oz, included_oz, "
        " source_url, verified_at, status, note, created_at) "
        "SELECT 'us_2026_off_peak', marketplace, size_tier, 'non_apparel', weight_min_oz, "
        "       weight_max_oz, price_band, base_fee_usd, per_increment_usd, increment_oz, "
        "       included_oz, source_url, verified_at, status, note, created_at "
        "FROM fba_fee_schedule_pre_v10"
    )
    conn.execute("DROP TABLE fba_fee_schedule_pre_v10")
    for statement in INDEXES_V10.values():
        conn.execute(statement)


def down(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE fba_fee_schedule RENAME TO fba_fee_schedule_pre_v9")
    conn.execute(FBA_FEE_SCHEDULE_V9)
    # season 回填为 off_peak：存量行全部来自非旺季那张表，这是 m0009 的事实，
    # 不是「猜一个默认值」。
    conn.execute(
        f"INSERT INTO fba_fee_schedule ({V9_COLUMNS}) "
        "SELECT id, marketplace, size_tier, weight_min_oz, weight_max_oz, price_band, "
        "       'off_peak', base_fee_usd, per_increment_usd, increment_oz, included_oz, "
        "       source_url, NULL, verified_at, status, note, created_at "
        "FROM fba_fee_schedule_pre_v9"
    )
    conn.execute("DROP TABLE fba_fee_schedule_pre_v9")
    for statement in INDEXES_V9.values():
        conn.execute(statement)
    conn.execute("DROP TABLE fba_rate_version")
