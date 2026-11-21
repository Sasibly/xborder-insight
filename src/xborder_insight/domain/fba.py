"""FBA 配送费查表：把「一个数」还原成「四维查表」。

这个模块存在的理由，是 FBA 费**不是参数**。
`cost_params` 里一个 key 对一个 value 的模型能表达「佣金 15%」，
但 FBA 费是 `f(size_tier, weight, price_band, season)`。硬填一个数，
等于替调用方默认了「哪个尺寸、多重、卖多少钱、哪天发货」——
换个 SKU 四个维度全变，而返回值看起来一样正常。

三个刻意的取舍：

1. 【查不到就报错，绝不外推到相邻档】
   large_standard 的 20 lb 以上没录，就去用 large_bulky 的费率？
   这是最典型的静默错误：结果是个正常数字，只是错了一个量级。
   尺寸段归错是**产品事实**（量包装尺寸），不是模型能替用户猜的。
   所以缺口一律抛 ParamUnverified，并说清缺哪四维。

2. 【共享端点归更精确的档，而不是靠 epsilon 切】
   官方档位写作「8 to 12 oz」「12 to 16 oz」，12 同时落在两档表述里。
   表里存闭区间，匹配时取 weight_min_oz 更大的一行。
   若反过来用 [min, max) 半开区间，就得回答「4.0000001 oz 算不算
   4 oz 以内」——这个问题的答案取决于浮点表示，而不是业务。

3. 【时段由发货日期决定，不由「今天」决定】
   同一笔报价，11 月发货与 1 月发货的费率差约 $0.32。
   若函数内部读当前时钟，那么「上个月算的毛利」今天重算会变——
   而报表上两个数字都写着同一个报价。日期必须从外面传进来，
   这也让整个函数可以用固定日期做断言。

`quote()` 是纯函数：不读库、不读时钟、不读环境变量。
所有数据缺口都以异常形式冒出来，不做「尽力而为」的猜测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from ..errors import InputInvalid, ParamUnverified

OZ_PER_LB = 16.0
_EPS = 1e-9

SIZE_TIERS = ("small_standard", "large_standard", "large_bulky", "extra_large")
PRICE_BANDS = ("under_10", "10_to_50", "over_50")
SEASONS = ("off_peak", "peak")

# 旺季 = 10/15 – 次年 1/14。这是一个跨年的闭区间，
# 所以不能写成 `start <= md <= end` 的单段比较，必须拆成两段或用元组比较。
PEAK_START = (10, 15)
PEAK_END = (1, 14)

SIZE_TIER_LABELS = {
    "small_standard": "小号标准件",
    "large_standard": "大号标准件",
    "large_bulky": "大件",
    "extra_large": "超大件",
}
PRICE_BAND_LABELS = {
    "under_10": "售价 < $10",
    "10_to_50": "售价 $10–50",
    "over_50": "售价 > $50",
}
SEASON_LABELS = {"off_peak": "非旺季（1/15–10/14）", "peak": "旺季（10/15–1/14）"}


# ---------------------------------------------------------------- 维度归一


def normalize_weight_oz(*, oz: float | None = None, lb: float | None = None) -> float:
    """把重量统一到 oz。必须显式给出单位，且只能给一个。

    为什么不接受「一个裸数字」：FBA 档位在 oz 与 lb 之间来回跳
    （4 oz、3 lb、20 lb），裸数字迟早会被按错单位传进来，
    而 4 与 64 都能算出「一个看起来正常的费率」。
    """
    if (oz is None) == (lb is None):
        raise InputInvalid(
            "重量必须且只能指定 oz 或 lb 中的一个",
            detail=f"当前收到 oz={oz!r}, lb={lb!r}；两者都空或都给都无法判定口径",
        )
    value = float(oz) if oz is not None else float(lb) * OZ_PER_LB
    if not math.isfinite(value):
        raise InputInvalid(f"重量必须是有限数值，当前值: {value!r}")
    if value <= 0:
        raise InputInvalid(f"重量必须大于 0，当前值: {value} oz（0 重量的包裹不存在）")
    return value


def price_band_of(selling_price: float) -> str:
    """售价 → 价格带。边界归属：$10 属中带，$50 属中带，$50.01 属高带。

    官方表述是「<$10」「$10–50」「>$50」，端点被中带吃进去。
    这三条边界被测试钉住，因为差 $0.01 就能让费率跳 $0.26，
    而这类差错在报表上完全看不出来。
    """
    price = float(selling_price)
    if not math.isfinite(price):
        raise InputInvalid(f"售价必须是有限数值，当前值: {selling_price!r}")
    if price <= 0:
        raise InputInvalid("售价必须大于 0：FBA 价格带按售价划分，0 元商品无带可归")
    if price < 10:
        return "under_10"
    if price <= 50:
        return "10_to_50"
    return "over_50"


def season_of(ship_date: date) -> str:
    """发货日期 → 时段。旺季 10/15 至次年 1/14，两端都含。"""
    if not isinstance(ship_date, date):
        raise InputInvalid(f"发货日期必须是 date，当前类型: {type(ship_date).__name__}")
    month_day = (ship_date.month, ship_date.day)
    if month_day >= PEAK_START or month_day <= PEAK_END:
        return "peak"
    return "off_peak"


# ---------------------------------------------------------------- 数据行


@dataclass(frozen=True)
class FeeRow:
    """费率表的一行。字段名与 `fba_fee_schedule` 的列一一对应。

    `included_oz` 是「base_fee 已覆盖到多少 oz」。
    为空时取 `weight_min_oz` 作为起算点——大多数档位 base 就覆盖整段，
    只有 3–20 lb 这类「首 3 lb 收 base、其后每 4 oz 加价」的档需要单独声明。
    """

    size_tier: str
    weight_min_oz: float
    weight_max_oz: float | None
    price_band: str
    season: str
    base_fee_usd: float
    per_increment_usd: float = 0.0
    increment_oz: float | None = None
    included_oz: float | None = None
    status: str = "draft"
    source_url: str | None = None
    note: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    @property
    def coverage_label(self) -> str:
        low = f"{self.weight_min_oz:g}"
        high = "∞" if self.weight_max_oz is None else f"{self.weight_max_oz:g}"
        return f"{low}–{high} oz"

    def covers_weight(self, weight_oz: float) -> bool:
        """闭区间判定。共享端点由 pick_row 的「取更大起点」规则消解。"""
        if weight_oz < self.weight_min_oz - _EPS:
            return False
        return self.weight_max_oz is None or weight_oz <= self.weight_max_oz + _EPS

    def dimension_label(self) -> str:
        return (
            f"{SIZE_TIER_LABELS.get(self.size_tier, self.size_tier)} · "
            f"{self.coverage_label} · "
            f"{PRICE_BAND_LABELS.get(self.price_band, self.price_band)} · "
            f"{SEASON_LABELS.get(self.season, self.season)}"
        )

    def extra_weight_fee(self, weight_oz: float) -> float:
        """超重增量。base_fee 覆盖不到的部分按 `increment_oz` 向上取整计费。

        向上取整（而不是按比例）是官方口径：不足一个增量的部分按一个算。
        用 `- _EPS` 抵消「恰好整档」被浮点误差多算一格的情况：
        (52 - 48) / 4 = 1.0 应为 1 格，若写成 0.9999999 会算出 2 格。
        """
        if self.increment_oz is None or self.per_increment_usd <= 0:
            return 0.0
        included = self.weight_min_oz if self.included_oz is None else self.included_oz
        if weight_oz <= included + _EPS:
            return 0.0
        steps = math.ceil((weight_oz - included) / self.increment_oz - _EPS)
        return steps * self.per_increment_usd


# ---------------------------------------------------------------- 查表


def pick_row(
    rows: list[FeeRow],
    *,
    size_tier: str,
    weight_oz: float,
    price_band: str,
    season: str,
) -> FeeRow | None:
    """在四维约束下挑一行。多个候选时取 `weight_min_oz` 最大者。

    只在这四个维度上比对，从不「降到相邻尺寸段去找」。
    返回 None 表示表里真的没有这一格。
    """
    candidates = [
        row
        for row in rows
        if row.size_tier == size_tier
        and row.price_band == price_band
        and row.season == season
        and row.covers_weight(weight_oz)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row.weight_min_oz)


@dataclass(frozen=True)
class FbaQuote:
    size_tier: str
    weight_oz: float
    price_band: str
    season: str
    base_fee_usd: float
    extra_weight_fee_usd: float
    fuel_surcharge_rate: float
    fuel_surcharge_usd: float
    total_fee_usd: float
    row: FeeRow
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def subtotal_usd(self) -> float:
        return round(self.base_fee_usd + self.extra_weight_fee_usd, 6)

    def as_dict(self) -> dict[str, object]:
        return {
            "size_tier": self.size_tier,
            "weight_oz": round(self.weight_oz, 4),
            "price_band": self.price_band,
            "season": self.season,
            "base_fee_usd": round(self.base_fee_usd, 4),
            "extra_weight_fee_usd": round(self.extra_weight_fee_usd, 4),
            "fuel_surcharge_rate": round(self.fuel_surcharge_rate, 6),
            "fuel_surcharge_usd": round(self.fuel_surcharge_usd, 4),
            "total_fee_usd": round(self.total_fee_usd, 4),
            "matched_range": self.row.coverage_label,
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
        }


def quote(
    rows: list[FeeRow],
    *,
    size_tier: str,
    selling_price: float,
    ship_date: date,
    weight_oz: float | None = None,
    weight_lb: float | None = None,
    fuel_surcharge_rate: float | None = None,
) -> FbaQuote:
    """查表得到一单的 FBA 费（含燃油附加费）。

    `fuel_surcharge_rate=None` 表示「本次没有可核实的附加费率」，
    此时按 0 计提，但在 assumptions 里显式记一笔——
    与「附加费是 0」在审计上必须能区分，否则成本被静默低估 3.5%。
    """
    if size_tier not in SIZE_TIERS:
        raise InputInvalid(
            f"未知的尺寸段: {size_tier!r}",
            detail=f"可选值: {', '.join(SIZE_TIERS)}。尺寸段由包装后尺寸决定，不能猜",
        )

    weight = normalize_weight_oz(oz=weight_oz, lb=weight_lb)
    band = price_band_of(selling_price)
    season = season_of(ship_date)

    row = pick_row(
        rows, size_tier=size_tier, weight_oz=weight, price_band=band, season=season
    )
    if row is None:
        raise ParamUnverified(
            f"FBA 费率表缺少该组合：{SIZE_TIER_LABELS[size_tier]} · "
            f"{weight:g} oz · {PRICE_BAND_LABELS[band]} · {SEASON_LABELS[season]}",
            detail=(
                "查表不做跨档外推：相邻尺寸段或相邻重量档的费率不能替代本档。"
                "请补充该档的官方费率后重试，或改用已覆盖的尺寸段/重量重新量测"
            ),
        )
    if not row.verified:
        raise ParamUnverified(
            f"FBA 费率行未核实：{row.dimension_label()}",
            detail=(
                f"该行处于 {row.status} 状态，缺少官方来源或生效日期。"
                f"{row.note or ''}"
            ).strip(),
        )

    assumptions: list[str] = [
        f"价格带按售价 ${float(selling_price):.2f} 归入「{PRICE_BAND_LABELS[band]}」，"
        "边界口径为 <$10 / $10–50 / >$50",
        f"时段按发货日期 {ship_date.isoformat()} 归入「{SEASON_LABELS[season]}」，"
        "不按「今天」判定——否则同一笔报价换天重算会变价",
        f"命中的重量档为 {row.coverage_label}；表内相邻档共享端点时取起点更大者",
    ]
    warnings: list[str] = []

    # 售价正好压在价格带分界上：$9.99 与 $10.00 是两个价格带，
    # 而 $9.99 是跨境最常见的定价。这里不猜调用方是不是手滑，
    # 只把「$0.01 之差会改变费率」这件事说出来。
    price = float(selling_price)
    if abs(price - 10.0) < _EPS or abs(price - 50.0) < _EPS:
        warnings.append(
            f"售价 ${price:.2f} 正好落在价格带分界：低于该值 $0.01 就换成另一档费率，"
            "请确认定价是刻意压在边界上，而不是四舍五入的结果"
        )

    base = row.base_fee_usd
    extra = row.extra_weight_fee(weight)
    if extra > 0:
        included = row.weight_min_oz if row.included_oz is None else row.included_oz
        assumptions.append(
            f"超出 {included:g} oz 的部分按每 {row.increment_oz:g} oz "
            f"${row.per_increment_usd:.2f} 计，向上取整得 ${extra:.2f}"
        )

    subtotal = base + extra

    if fuel_surcharge_rate is None:
        rate = 0.0
        assumptions.append(
            "未施加燃油与物流附加费（费率参数未核实）：总费用较实际偏低约 3.5%，"
            "该口径下不应据此判定「可做」"
        )
    else:
        rate = float(fuel_surcharge_rate)
        if not math.isfinite(rate) or rate < 0:
            raise InputInvalid(f"燃油附加费率必须是非负有限值，当前值: {fuel_surcharge_rate!r}")
        assumptions.append(
            f"燃油与物流附加费按配送费（base + 超重增量）乘 {rate:.4%} 计提，不按售价"
        )

    surcharge = subtotal * rate
    total = subtotal + surcharge

    return FbaQuote(
        size_tier=size_tier,
        weight_oz=weight,
        price_band=band,
        season=season,
        base_fee_usd=base,
        extra_weight_fee_usd=extra,
        fuel_surcharge_rate=rate,
        fuel_surcharge_usd=surcharge,
        total_fee_usd=total,
        row=row,
        assumptions=tuple(assumptions),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------- 审计


@dataclass(frozen=True)
class GroupCoverage:
    """一组 (size_tier, price_band, season) 的覆盖情况。"""

    size_tier: str
    price_band: str
    season: str
    bands: tuple[tuple[float, float | None], ...]
    gaps: tuple[tuple[float, float | None], ...]
    overlaps: tuple[tuple[float, float], ...]
    verified: int
    draft: int

    @property
    def key(self) -> str:
        return (
            f"{self.size_tier}/{self.price_band}/{self.season}"
        )

    @property
    def complete(self) -> bool:
        return not self.gaps and not self.overlaps and self.draft == 0


@dataclass(frozen=True)
class ScheduleAudit:
    marketplace: str
    total_rows: int
    verified_rows: int
    draft_rows: int
    groups: tuple[GroupCoverage, ...] = field(default_factory=tuple)

    @property
    def gap_count(self) -> int:
        return sum(len(g.gaps) for g in self.groups)

    @property
    def overlap_count(self) -> int:
        return sum(len(g.overlaps) for g in self.groups)

    @property
    def banded_dimensions(self) -> int:
        """已声明费率的角度组合数。用于算「16 个角度里覆盖了几个」。"""
        return len(self.groups)

    def as_dict(self) -> dict[str, object]:
        return {
            "marketplace": self.marketplace,
            "total_rows": self.total_rows,
            "verified_rows": self.verified_rows,
            "draft_rows": self.draft_rows,
            "dimension_combinations": self.banded_dimensions,
            "gap_count": self.gap_count,
            "overlap_count": self.overlap_count,
            "groups": [
                {
                    "key": g.key,
                    "bands": [[lo, hi] for lo, hi in g.bands],
                    "gaps": [[lo, hi] for lo, hi in g.gaps],
                    "overlaps": [[lo, hi] for lo, hi in g.overlaps],
                    "verified": g.verified,
                    "draft": g.draft,
                }
                for g in self.groups
            ],
        }


def audit(rows: list[FeeRow], *, marketplace: str = "US") -> ScheduleAudit:
    """检查维表的完整性：哪些角度有行、哪些重量段是洞、哪些区间重叠。

    这条函数的存在意义，是把「表还不完整」从一句形容词变成一份清单。
    没有它，缺口只能靠人翻 docs 发现；有了它，`make db-fba` 就能直接
    报出「large_standard 的 4–8 oz 没有费率」。
    """
    grouped: dict[tuple[str, str, str], list[FeeRow]] = {}
    for row in rows:
        grouped.setdefault((row.size_tier, row.price_band, row.season), []).append(row)

    covers: list[GroupCoverage] = []
    for (size_tier, price_band, season), group in sorted(grouped.items()):
        ordered_rows = sorted(group, key=lambda r: r.weight_min_oz)
        bands: list[tuple[float, float | None]] = []
        overlaps: list[tuple[float, float]] = []
        for row in ordered_rows:
            if bands:
                prev_low, prev_high = bands[-1]
                # 相邻档共享端点不算重叠：官方写法「8 to 12 / 12 to 16」本就重叠在 12，
                # 由 pick_row「取起点更大者」消解。只有真重叠（起点落在前一档内部）才算错。
                if prev_high is None or row.weight_min_oz < prev_high - _EPS:
                    overlaps.append((row.weight_min_oz, prev_high if prev_high is not None else math.inf))
            bands.append((row.weight_min_oz, row.weight_max_oz))

        gaps: list[tuple[float, float | None]] = []
        cursor = 0.0
        for low, high in bands:
            if low > cursor + _EPS:
                gaps.append((cursor, low))
            cursor = math.inf if high is None else max(cursor, high)
            if cursor == math.inf:
                break
        if cursor != math.inf:
            gaps.append((cursor, None))

        covers.append(
            GroupCoverage(
                size_tier=size_tier,
                price_band=price_band,
                season=season,
                bands=tuple(bands),
                gaps=tuple(gaps),
                overlaps=tuple(overlaps),
                verified=sum(1 for r in group if r.verified),
                draft=sum(1 for r in group if not r.verified),
            )
        )

    return ScheduleAudit(
        marketplace=marketplace,
        total_rows=len(rows),
        verified_rows=sum(1 for r in rows if r.verified),
        draft_rows=sum(1 for r in rows if not r.verified),
        groups=tuple(covers),
    )
