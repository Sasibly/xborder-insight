"""大件（small_bulky / large_bulky）的官方费率数值，2026 两期。

与 `fba_us_rates.py` 分开是有原因的，不是随手新建一个文件：

1. **枚举先后**：m0011 执行时 `size_tier` 的 CHECK 还只允许 4 个值，
   如果把这些行塞进 `fba_us_rates.PRICES`，m0011 会自己插不进去而炸掉。
2. **计费结构不同**：标准尺寸按重量档给「一格一个价」，大件是
   「base（含首 1 lb）+ $0.38/每 1 lb 区间」，靠 `included_oz` 表达，
   与标准尺寸的档位表不是一套东西，混在一个 dict 里迟早对不齐。

数值来源与抓取口径见 `fba_us_rates.SOURCE_URL` / `VERIFIED_AT`，同一页同一次抓取。

**只录 2026 两期**（`us_2026_off_peak` / `us_2026_peak`）。2027 非旺季的 bulky
已被官方重构为 4 档 bulky + 6 档 oversize，与这里的枚举不是一套，
录进来会造出一个「名字对得上、含义对不上」的格子，所以整段留空（`docs/07` H14）。
"""

from __future__ import annotations

from .fba_us_rates import BAND_KEYS, VERIFIED_AT, SOURCE_URL

MARKET = "US"

#: 大件两段共用的重量口径：0–50 lb 一档到底，超过 50 lb 就不是 bulky 了。
BULKY_MAX_OZ = 50.0 * 16.0

#: 基础费覆盖首 1 lb，其后每满 1 lb 加 $0.38（官方写作
#: "$6.78 + $0.38/lb interval above first lb"）。
BULKY_INCLUDED_OZ = 16.0
BULKY_INCREMENT_OZ = 16.0
BULKY_PER_INCREMENT_USD = 0.38

#: 2026 非旺季官方示例（baby cot，实重 7.90 lb、>$50 档、Small bulky）：
#: "$7.55 for first 1 lb plus $0.38 for each additional 1 lb interval" = $10.21。
#: 这个例子同时验证了两件事：base 含首 1 lb、以及不足 1 lb 向上取整。
#: 测试里用它当锚点，见 `tests/test_fba_bulky.py`。
OFFICIAL_EXAMPLE = {"tier": "small_bulky", "lb": 7.90, "price_band": "over_50", "fee_usd": 10.21}

# key = (version_key, product_class, size_tier) -> (under_10, 10_to_50, over_50)
BULKY_PRICES: dict[tuple[str, str, str], tuple[float, float, float]] = {
    # ---------------------------------------------------------- 2026 非旺季
    ("us_2026_off_peak", "non_apparel", "small_bulky"): (6.78, 7.55, 7.55),
    ("us_2026_off_peak", "non_apparel", "large_bulky"): (8.58, 9.35, 9.35),
    ("us_2026_off_peak", "dangerous_goods", "small_bulky"): (7.50, 8.27, 8.27),
    ("us_2026_off_peak", "dangerous_goods", "large_bulky"): (9.30, 10.07, 10.07),
    # ---------------------------------------------------------- 2026 旺季
    ("us_2026_peak", "non_apparel", "small_bulky"): (7.82, 8.59, 8.59),
    ("us_2026_peak", "non_apparel", "large_bulky"): (9.62, 10.39, 10.39),
    ("us_2026_peak", "dangerous_goods", "small_bulky"): (9.06, 9.83, 9.83),
    ("us_2026_peak", "dangerous_goods", "large_bulky"): (10.86, 11.63, 11.63),
}

BULKY_ROW_NOTE = (
    "口径：官方费率页（2026-11-21 抓取），美国站、大件（small/large bulky）、"
    "0–50 lb、base 含首 1 lb + $0.38/每 1 lb 区间，未含 3.5% 燃油与物流附加费。"
    "抓取做了两次独立提取，同档位跨类别/跨期差值完全自洽（非服装 vs 危险品"
    "非旺季各差 $0.72、旺季各差 $1.24，旺季 vs 非旺季各差 $1.04/$1.56），"
    "并用官方 baby cot 示例（7.90 lb → $10.21）复算通过；未做 HTML 逐格人工复核，"
    "故仍列在 docs/07 H12 的复核范围内。"
)

#: (version, product_class, size_tier, min_oz, max_oz, price_band,
#:  base_fee, per_increment_usd, increment_oz, included_oz)
FeeSeedRow = tuple[str, str, str, float, float, str, float, float, float, float]


def fee_rows() -> list[FeeSeedRow]:
    """展开成 24 行：2 期 × 2 类别 × 2 段 × 3 价格带。"""
    out: list[FeeSeedRow] = []
    for (version_key, product_class, size_tier), fees in BULKY_PRICES.items():
        if len(fees) != len(BAND_KEYS):
            raise AssertionError(
                f"{version_key}/{product_class}/{size_tier} 的价格条数应为 {len(BAND_KEYS)}"
            )
        for band, fee in zip(BAND_KEYS, fees):
            out.append(
                (
                    version_key,
                    product_class,
                    size_tier,
                    0.0,
                    BULKY_MAX_OZ,
                    band,
                    fee,
                    BULKY_PER_INCREMENT_USD,
                    BULKY_INCREMENT_OZ,
                    BULKY_INCLUDED_OZ,
                )
            )
    return out


__all__ = [
    "BULKY_MAX_OZ",
    "BULKY_PRICES",
    "BULKY_ROW_NOTE",
    "MARKET",
    "OFFICIAL_EXAMPLE",
    "SOURCE_URL",
    "VERIFIED_AT",
    "fee_rows",
]
