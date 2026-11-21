"""0009：FBA 配送费维表 `fba_fee_schedule`。

为什么不能继续留在 cost_params 里：
    cost_params 的语义是「一个 key 一个值」，且 UNIQUE (param_key, marketplace)
    决定了它连分段费率都装不下（m0007 因此只能把区间做成断言式的越界报错）。
    FBA 费是**四维查表**：尺寸段 × 重量段 × 价格带 × 时段。
    往单行参数里塞任何一个数，都必须先替调用方假设「哪个尺寸、多重、
    卖多少钱、哪天发货」——而换个 SKU 这四个维度全变。填的那个数看起来
    正常，用起来全错，正是 ②类错误。

四个维度为什么必须分列：
    分开列才能让「缺哪一维」变成可查询的事实。合成一个 `tier_key`
    字符串（如 `large_standard|12-16oz|10_to_50|peak`）写起来省事，
    但 gap 检测、重叠检测、覆盖率统计全得靠字符串解析，而且拼错不报错。
    分列之后每个维度都带 CHECK 枚举，写错直接进不了库。

重量语义：闭区间 + 共享端点归更精确的档
    官方档位写作「4 oz or less」「8 to 12 oz」「12 to 16 oz」——
    12 这个值同时属于后两档的表述。表里存闭区间 [8, 12] 与 [12, 16]，
    匹配时取 `weight_min_oz` 更大的一行（更精确的档优先）。
    这样不必引入浮点 epsilon 去切「4.0001 算不算 4 以内」，
    也不会因为改档位而让存量数据在边界上语义漂移。
    代价是审计时「相邻档共享端点」不算重叠，判定条件是 `prev.max > cur.min`
    而不是 `>=`——这条口径写在 domain/fba.py 的 audit 里，测试钉住。

为什么不预置全部档位：
    官方表的档位远不止这次记录的 4 档（4 至 8 oz、16 oz 至 3 lb 都缺），
    超尺寸段整体未录。凭「大概是这个数」补齐会让缺口消失，
    而缺口正是这张表最有价值的信息——它告诉你哪些 SKU 现在还不敢报价。
    所以种子只放 docs/04 第 2 节已记录的锚点，status 一律 draft。

为什么仍保留 status 三闸：
    与 cost_params 同构：status = 'verified' ⇒ 来源 + 生效日期 + 核实日期齐全。
    一张维表如果有 80% 的行能用来算钱、20% 的行是猜的，而查询层不区分，
    那么任何一次查表都可能在用猜的那一行——且返回值看起来一样。
"""

from __future__ import annotations

import sqlite3

CREATED_AT = "2026-11-21T00:00:00+08:00"  # 迁移内不读时钟，保证可重复执行
MARKET = "US"

FBA_FEE_SCHEDULE = """
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
    -- 同一维度组合下，同一个重量起点只能有一行。这拦不住区间重叠
    -- （[0,4) 与 [2,6) 的起点不同、依然重叠），重叠检测只能在写入路径
    -- 与 audit() 里做——SQLite 的 CHECK 不能跨行查询。这是本表已知边界。
    UNIQUE (marketplace, size_tier, price_band, season, weight_min_oz),
    CHECK (weight_max_oz IS NULL OR weight_max_oz > weight_min_oz),
    -- 有增量单价就必须说明「每多少 oz 加一次」，否则计价函数无法收敛；
    -- 没有增量单价则 increment_oz 必须为空，避免留下语义含糊的半行。
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

INDEXES: dict[str, str] = {
    "idx_fba_lookup": (
        "CREATE INDEX idx_fba_lookup ON fba_fee_schedule "
        "(marketplace, size_tier, price_band, season, weight_min_oz)"
    ),
}

# 燃油与物流附加费是**乘数**，不是加数，且全美统一（不随尺寸段变），
# 所以它仍然是一行参数，留在 cost_params 里更合适——塞进维表会让
# 每一行都重复一遍同一个比率，改一次要改上百行。
FUEL_SURCHARGE_KEY = "amazon.fba_fuel_surcharge"
FUEL_SURCHARGE_NOTE = (
    "2026-04-17 起全美 FBA 加收 3.5% fuel & logistics surcharge，"
    "按「配送费」计提、不按售价。费率页与附件需逐条核对后置 verified；"
    "注意它作用于 base_fee 与超重增量之和，不是只作用于 base_fee"
)

# (size_tier, weight_min_oz, weight_max_oz, price_band, season, base, per_inc, inc_oz)
#
# 只录 docs/04 第 2 节已记录的锚点：large_standard、非旺季、非服饰、未含附加费。
# 4 至 8 oz、16 oz 至 3 lb、以及 320 oz（20 lb）以上均**故意留空**——
# 那几档的官方数值没有逐条记录，凭印象补齐会让缺口消失。
_DOCS_ANCHORS: tuple[tuple[str, float, float | None, str, float, float, float | None], ...] = (
    # 4 oz 或以下
    ("large_standard", 0.0, 4.0, "under_10", 2.91, 0.0, None),
    ("large_standard", 0.0, 4.0, "10_to_50", 3.73, 0.0, None),
    ("large_standard", 0.0, 4.0, "over_50", 3.99, 0.0, None),
    # 8 至 12 oz
    ("large_standard", 8.0, 12.0, "under_10", 3.38, 0.0, None),
    ("large_standard", 8.0, 12.0, "10_to_50", 4.20, 0.0, None),
    ("large_standard", 8.0, 12.0, "over_50", 4.46, 0.0, None),
    # 12 至 16 oz
    ("large_standard", 12.0, 16.0, "under_10", 3.78, 0.0, None),
    ("large_standard", 12.0, 16.0, "10_to_50", 4.60, 0.0, None),
    ("large_standard", 12.0, 16.0, "over_50", 4.86, 0.0, None),
    # 3 至 20 lb：首 3 lb 收 base，其后每 4 oz 加 $0.08
    ("large_standard", 48.0, 320.0, "under_10", 6.15, 0.08, 4.0),
    ("large_standard", 48.0, 320.0, "10_to_50", 6.97, 0.08, 4.0),
    ("large_standard", 48.0, 320.0, "over_50", 7.23, 0.08, 4.0),
)

ANCHOR_NOTE = (
    "口径：large_standard、非旺季（1/15–10/14）、非服饰、未含 3.5% 燃油附加费。"
    "数值来自 docs/04 第 2 节记录的官方费率页锚点；"
    "本次只把锚点搬进结构，未逐档复核原文，故保持 draft。"
    "缺档：4–8 oz、16–48 oz、320 oz 以上，以及 small_standard / bulky / extra_large 全段"
)


def up(conn: sqlite3.Connection) -> None:
    conn.execute(FBA_FEE_SCHEDULE)
    for statement in INDEXES.values():
        conn.execute(statement)

    conn.executemany(
        "INSERT INTO fba_fee_schedule "
        "(marketplace, size_tier, weight_min_oz, weight_max_oz, price_band, season, "
        " base_fee_usd, per_increment_usd, increment_oz, status, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'off_peak', ?, ?, ?, 'draft', ?, ?)",
        [
            (MARKET, tier, lo, hi, band, base, per_inc, inc, ANCHOR_NOTE, CREATED_AT)
            for tier, lo, hi, band, base, per_inc, inc in _DOCS_ANCHORS
        ],
    )
    conn.execute(
        "INSERT INTO cost_params "
        "(param_key, marketplace, category, unit, value, source_url, effective_date, "
        " verified_at, status, note, created_at) "
        "VALUES (?, ?, NULL, 'ratio', NULL, NULL, NULL, NULL, 'draft', ?, ?)",
        (FUEL_SURCHARGE_KEY, MARKET, FUEL_SURCHARGE_NOTE, CREATED_AT),
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM cost_params WHERE param_key = ?", (FUEL_SURCHARGE_KEY,))
    for name in reversed(list(INDEXES)):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS fba_fee_schedule")
