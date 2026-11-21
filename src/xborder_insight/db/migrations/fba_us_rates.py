"""美国站 FBA 配送费的官方数值（2026 非旺季 / 2026 旺季 / 2027 非旺季）。

数据来源只有一个：官方费率页
<https://sellercentral.amazon.com/help/hub/reference/external/GABBX6GZPA8MSZGW>
（2026-11-21 抓取）。第三方「2026 FBA fee guide」一律不采信。

三件在这里刻意做的事：

1. 【只录业务相关的两个产品类别】
   非服装（non_apparel）与危险品（dangerous_goods）。
   服装（apparel）不在当前 6 个品类的经营范围里，录了也不会被查到，
   却会让「人工逐格复核」的工作量翻倍——所以故意留空，缺口写在 docs/04。

2. 【只录标准尺寸，大件/超大件整段留空】
   small_standard / large_standard 覆盖 3C 配件与小家电的全部候选。
   大件 6 档在 2027 年还被官方重构（small/medium bulky + 6 档 oversize），
   现在录进来的枚举很快就得推翻，见 docs/07 H11。

3. 【数值原样照抄，不做任何「看起来更合理」的修正】
   官方表里 2027 非服装 large_standard 的 ≤4 oz 与 2026 同价（$2.91），
   看起来像笔误，但那正是官方页的原值——「帮它修正」才是造错。
   数值之间的单调性由 tests/test_fba_schedule.py 的不变式用例守住，
   一旦抄错一位，通常会在某条不变式上破掉。

单位：USD/件，**未含 3.5% 燃油与物流附加费**（附加费在 cost_params 里单独计提）。
"""

from __future__ import annotations

SOURCE_URL = "https://sellercentral.amazon.com/help/hub/reference/external/GABBX6GZPA8MSZGW"
VERIFIED_AT = "2026-11-21"

# 重量档（oz）。官方写作「4 oz or less」「8 to 12 oz」「3 to 20 lb」，
# 这里一律折成闭区间并共享端点，匹配时取 weight_min_oz 更大的一行。
SMALL_BANDS: tuple[tuple[float, float], ...] = (
    (0.0, 2.0),     # ≤2 oz
    (2.0, 4.0),     # 2+–4 oz
    (4.0, 6.0),
    (6.0, 8.0),
    (8.0, 10.0),
    (10.0, 12.0),
    (12.0, 14.0),
    (14.0, 16.0),   # 14+–16 oz，小号标准件上限
)

LARGE_BANDS: tuple[tuple[float, float], ...] = (
    (0.0, 4.0),     # ≤4 oz
    (4.0, 8.0),
    (8.0, 12.0),
    (12.0, 16.0),
    (16.0, 20.0),   # 1+–1.25 lb
    (20.0, 24.0),
    (24.0, 28.0),
    (28.0, 32.0),
    (32.0, 36.0),
    (36.0, 40.0),
    (40.0, 44.0),
    (44.0, 48.0),   # 2.75+–3 lb
    (48.0, 320.0),  # 3+–20 lb，首 3 lb 收 base，其后每 4 oz 加价
)

TIER_BANDS: dict[str, tuple[tuple[float, float], ...]] = {
    "small_standard": SMALL_BANDS,
    "large_standard": LARGE_BANDS,
}

# 3–20 lb 档的增量：base 已覆盖前 48 oz，之后每 4 oz 加 $0.08。
INCREMENT: dict[str, dict[str, float]] = {
    "large_standard": {"per_increment_usd": 0.08, "increment_oz": 4.0},
}

# key = (version_key, product_class, size_tier)
# value = 每个重量档一个三元组 (under_10, 10_to_50, over_50)，顺序与 TIER_BANDS 对齐
PRICES: dict[tuple[str, str, str], tuple[tuple[float, float, float], ...]] = {
    # ---------------------------------------------------------------- 2026 非旺季
    ("us_2026_off_peak", "non_apparel", "small_standard"): (
        (2.43, 3.32, 3.58),
        (2.49, 3.42, 3.68),
        (2.56, 3.45, 3.71),
        (2.66, 3.54, 3.80),
        (2.77, 3.68, 3.94),
        (2.82, 3.78, 4.04),
        (2.92, 3.91, 4.17),
        (2.95, 3.96, 4.22),
    ),
    ("us_2026_off_peak", "non_apparel", "large_standard"): (
        (2.91, 3.73, 3.99),
        (3.13, 3.95, 4.21),
        (3.38, 4.20, 4.46),
        (3.78, 4.60, 4.86),
        (4.22, 5.04, 5.30),
        (4.60, 5.42, 5.68),
        (4.75, 5.57, 5.83),
        (5.00, 5.82, 6.08),
        (5.10, 5.92, 6.18),
        (5.28, 6.10, 6.36),
        (5.44, 6.26, 6.52),
        (5.85, 6.67, 6.93),
        (6.15, 6.97, 7.23),
    ),
    ("us_2026_off_peak", "dangerous_goods", "small_standard"): (
        (3.40, 4.29, 4.55),
        (3.43, 4.36, 4.62),
        (3.48, 4.37, 4.63),
        (3.55, 4.43, 4.69),
        (3.64, 4.55, 4.81),
        (3.65, 4.61, 4.87),
        (3.73, 4.72, 4.98),
        (3.77, 4.78, 5.04),
    ),
    ("us_2026_off_peak", "dangerous_goods", "large_standard"): (
        (3.73, 4.55, 4.81),
        (3.94, 4.76, 5.02),
        (4.17, 4.99, 5.25),
        (4.37, 5.19, 5.45),
        (4.82, 5.64, 5.90),
        (5.20, 6.02, 6.28),
        (5.35, 6.17, 6.43),
        (5.49, 6.31, 6.57),
        (5.56, 6.38, 6.64),
        (5.74, 6.56, 6.82),
        (5.90, 6.72, 6.98),
        (6.31, 7.13, 7.39),
        (6.61, 7.43, 7.69),
    ),
    # ---------------------------------------------------------------- 2026 旺季
    ("us_2026_peak", "non_apparel", "small_standard"): (
        (2.62, 3.51, 3.77),
        (2.68, 3.61, 3.87),
        (2.76, 3.65, 3.91),
        (2.86, 3.74, 4.00),
        (2.98, 3.89, 4.15),
        (3.03, 3.99, 4.25),
        (3.14, 4.13, 4.39),
        (3.17, 4.18, 4.44),
    ),
    ("us_2026_peak", "non_apparel", "large_standard"): (
        (3.15, 3.97, 4.23),
        (3.39, 4.21, 4.47),
        (3.66, 4.48, 4.74),
        (4.07, 4.89, 5.15),
        (4.52, 5.34, 5.60),
        (4.91, 5.73, 5.99),
        (5.07, 5.89, 6.15),
        (5.33, 6.15, 6.41),
        (5.47, 6.29, 6.55),
        (5.67, 6.49, 6.75),
        (5.84, 6.66, 6.92),
        (6.26, 7.08, 7.34),
        (6.69, 7.51, 7.77),
    ),
    ("us_2026_peak", "dangerous_goods", "small_standard"): (
        (3.74, 4.63, 4.89),
        (3.80, 4.73, 4.99),
        (3.88, 4.77, 5.03),
        (3.98, 4.86, 5.12),
        (4.10, 5.01, 5.27),
        (4.14, 5.10, 5.36),
        (4.25, 5.24, 5.50),
        (4.32, 5.33, 5.59),
    ),
    ("us_2026_peak", "dangerous_goods", "large_standard"): (
        (4.32, 5.14, 5.40),
        (4.56, 5.38, 5.64),
        (4.82, 5.64, 5.90),
        (5.04, 5.86, 6.12),
        (5.51, 6.33, 6.59),
        (5.91, 6.73, 6.99),
        (6.08, 6.90, 7.16),
        (6.24, 7.06, 7.32),
        (6.33, 7.15, 7.41),
        (6.53, 7.35, 7.61),
        (6.70, 7.52, 7.78),
        (7.12, 7.94, 8.20),
        (7.51, 8.33, 8.59),
    ),
    # ---------------------------------------------------------------- 2027 非旺季
    ("us_2027_off_peak", "non_apparel", "small_standard"): (
        (2.64, 3.75, 4.31),
        (2.72, 3.78, 4.34),
        (2.80, 3.92, 4.47),
        (2.83, 3.97, 4.53),
        (2.87, 4.06, 4.62),
        (2.92, 4.16, 4.73),
        (2.96, 4.30, 4.85),
        (2.99, 4.32, 4.88),
    ),
    ("us_2027_off_peak", "non_apparel", "large_standard"): (
        (2.91, 3.79, 4.35),
        (3.13, 4.01, 4.57),
        (3.38, 4.26, 4.83),
        (3.78, 4.67, 5.23),
        (4.22, 5.19, 5.75),
        (4.60, 5.57, 6.14),
        (4.75, 5.72, 6.29),
        (5.00, 5.98, 6.54),
        (5.10, 6.08, 6.64),
        (5.28, 6.26, 6.83),
        (5.44, 6.42, 6.99),
        (5.85, 6.85, 7.41),
        (6.15, 7.16, 7.73),
    ),
    ("us_2027_off_peak", "dangerous_goods", "small_standard"): (
        (3.57, 4.59, 5.15),
        (3.62, 4.61, 5.17),
        (3.69, 4.75, 5.32),
        (3.71, 4.80, 5.37),
        (3.74, 4.88, 5.45),
        (3.75, 4.97, 5.54),
        (3.79, 5.01, 5.57),
        (3.83, 5.04, 5.60),
    ),
    ("us_2027_off_peak", "dangerous_goods", "large_standard"): (
        (3.73, 4.62, 5.18),
        (3.94, 4.83, 5.40),
        (4.17, 5.06, 5.63),
        (4.37, 5.27, 5.83),
        (4.82, 5.79, 6.36),
        (5.20, 6.18, 6.74),
        (5.35, 6.33, 6.90),
        (5.49, 6.47, 7.04),
        (5.56, 6.55, 7.11),
        (5.74, 6.73, 7.29),
        (5.90, 6.89, 7.45),
        (6.31, 7.32, 7.88),
        (6.61, 7.63, 8.20),
    ),
}

BAND_KEYS = ("under_10", "10_to_50", "over_50")

FeeSeedRow = tuple[str, str, str, float, float | None, str, float, float, float | None]

ROW_NOTE = (
    "口径：官方费率页（2026-11-21 抓取），美国站、标准尺寸、未含 3.5% 燃油与物流附加费。"
    "2026-11-21 一次抓取 + 与 docs/04 第 2 节 12 个既有锚点逐值比对一致，"
    "未做 HTML 表格逐格人工复核，故逐格复核列为 docs/07 H12。"
)


def fee_rows() -> list[FeeSeedRow]:
    """展开成可插入的行：(version, product_class, size_tier, min_oz, max_oz,
    price_band, base_fee, per_increment_usd, increment_oz)。"""
    out: list[FeeSeedRow] = []
    for (version_key, product_class, size_tier), triples in PRICES.items():
        bands = TIER_BANDS[size_tier]
        if len(triples) != len(bands):
            # 档位与价格条数不一致时立刻炸掉：少一行会让最后一档悄悄用错值。
            raise AssertionError(
                f"{version_key}/{product_class}/{size_tier} 有 {len(triples)} 组价格，"
                f"但重量档有 {len(bands)} 个"
            )
        extra = INCREMENT.get(size_tier, {})
        for (lo, hi), (p_low, p_mid, p_high) in zip(bands, triples):
            is_last = hi == bands[-1][1]
            for band, fee in zip(BAND_KEYS, (p_low, p_mid, p_high)):
                out.append(
                    (
                        version_key,
                        product_class,
                        size_tier,
                        lo,
                        hi,
                        band,
                        fee,
                        extra.get("per_increment_usd", 0.0) if is_last else 0.0,
                        extra.get("increment_oz") if is_last and extra else None,
                    )
                )
    return out
