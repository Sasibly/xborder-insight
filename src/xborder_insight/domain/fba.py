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

4. 【费率版本是「时段」的载体，不是一个注释字段】
   时段原先只是行上的一个枚举。但费率期并不是按年份切的：
   2026 旺季从 10/15 一直到 2027-01-14，2027 非旺季从 2027-01-15 起，
   而 2027 旺季（10/15/2027 起）官方尚未发布。单靠 `season='peak'`
   无法区分「2026 旺季」与「尚未发布的 2027 旺季」——
   2027-12-01 发货的报价会命中 2026 旺季那一行，价格看起来完全正常。
   现在每一行挂在 `RateVersion` 上，版本带生效区间；匹配先按发货日期
   找版本，找不到就报「缺版本」，绝不借用上一期。

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

SIZE_TIERS = (
    "small_standard",
    "large_standard",
    "small_bulky",
    "large_bulky",
    "extra_large",
)
PRICE_BANDS = ("under_10", "10_to_50", "over_50")
SEASONS = ("off_peak", "peak")

# 旺季 = 10/15 – 次年 1/14。这是一个跨年的闭区间，
# 所以不能写成 `start <= md <= end` 的单段比较，必须拆成两段或用元组比较。
PEAK_START = (10, 15)
PEAK_END = (1, 14)

SIZE_TIER_LABELS = {
    "small_standard": "小号标准件",
    "large_standard": "大号标准件",
    "small_bulky": "小号大件",
    "large_bulky": "大件",
    "extra_large": "超大件",
}
PRICE_BAND_LABELS = {
    "under_10": "售价 < $10",
    "10_to_50": "售价 $10–50",
    "over_50": "售价 > $50",
}
# 官方把标准尺寸分三套表：非服装、服装、危险品。
# 差异不是小数点级别的：2026 非旺季 large_standard ≤4 oz 的 $10–50 档
# 非服装 $3.73、危险品 $4.55，一件差 $0.82。
# 含锂电池的产品（无线充电器、扫地机、宠物饮水机）在 FBA 口径里属于危险品，
# 按非服装报价会静默低估成本，所以这一维没有默认值——必须由调用方声明。
PRODUCT_CLASSES = ("non_apparel", "apparel", "dangerous_goods")
PRODUCT_CLASS_LABELS = {
    "non_apparel": "非服装",
    "apparel": "服装",
    "dangerous_goods": "危险品（含锂电池等）",
}
SEASON_LABELS = {"off_peak": "非旺季（1/15–10/14）", "peak": "旺季（10/15–1/14）"}

# 尺寸段自身的重量上界（oz）：超过它的重量根本不属于这一段。
# 取值来自官方尺寸段定义（小号标准件 ≤16 oz，大号标准件 ≤20 lb，
# 小号大件与大件均 ≤50 lb），刻意**不**从表里的最后一行反推——否则漏录最后一档时，
# 上界会跟着缩，缺口就被自己的缺失掩盖了。
# 未登记上界的尺寸段（extra_large）保持空：那一段整段未录，
# 「上界是多少」本身属于未知，不能顺手抄一个。
TIER_CEILING_OZ: dict[str, float] = {
    "small_standard": 16.0,
    "large_standard": 320.0,
    "small_bulky": 800.0,
    "large_bulky": 800.0,
}


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


# ---------------------------------------------------------------- 费率版本


@dataclass(frozen=True)
class RateVersion:
    """一期官方费率表：一张表管一段时间。

    `effective_from` / `effective_to` 都是**闭区间**：官方的表头写作
    「January 15, 2026 – October 14, 2026」，端点属于该期。
    相邻两期共享端点（10/14 与 10/15 相接，1/14 与 1/15 相接），
    所以不会出现「某天落在两期之间」的问题——若真出现，说明区间录错了。
    """

    version_key: str
    marketplace: str
    season: str
    effective_from: date
    effective_to: date
    status: str = "draft"
    source_url: str | None = None
    verified_at: str | None = None
    note: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    @property
    def period_label(self) -> str:
        return f"{self.effective_from.isoformat()} 至 {self.effective_to.isoformat()}"

    def covers(self, day: date) -> bool:
        return self.effective_from <= day <= self.effective_to

    def dimension_label(self) -> str:
        return (
            f"{self.version_key}（{self.period_label}，"
            f"{SEASON_LABELS.get(self.season, self.season)}）"
        )


def pick_version(versions: list[RateVersion], ship_date: date) -> RateVersion | None:
    """按发货日期挑一期费率。返回 None 表示这一期官方没发布、或还没录。

    「没发布」与「还没录」在本函数里无法区分，但对调用方是同一句话：
    这个日期报价不了，别用别的期顶。具体的缺哪个日期段由 message 给出。
    """
    if not isinstance(ship_date, date):
        raise InputInvalid(f"发货日期必须是 date，当前类型: {type(ship_date).__name__}")
    hits = [version for version in versions if version.covers(ship_date)]
    if len(hits) > 1:
        # 区间重叠 = 录入错误。此时无论挑哪一期都是猜的，必须停。
        raise ParamUnverified(
            f"发货日期 {ship_date.isoformat()} 同时落在 {len(hits)} 个费率版本内",
            detail=(
                "版本区间重叠，无法判定该用哪一期："
                + "、".join(v.version_key for v in hits)
                + "。重叠只能由数据录错导致，请先修版本区间"
            ),
        )
    if not hits:
        return None
    version = hits[0]
    expected = season_of(ship_date)
    if version.season != expected:
        # 版本的时段标注与它自己的区间不自洽：意味着同一行会被两套口径引用。
        raise ParamUnverified(
            f"费率版本 {version.version_key} 的时段标注与生效区间不自洽",
            detail=(
                f"版本标注为 {version.season}，但 {ship_date.isoformat()} 按 "
                f"10/15–1/14 口径属于 {expected}；两者必须一致"
            ),
        )
    return version


def version_overlaps(versions: list[RateVersion]) -> tuple[tuple[str, str], ...]:
    """版本之间是否有生效区间重叠。重叠时 pick_version 会拒绝服务，
    但审计应该在报价之前就把这件事说出来。"""
    ordered = sorted(versions, key=lambda v: v.effective_from)
    overlaps: list[tuple[str, str]] = []
    for earlier, later in zip(ordered, ordered[1:]):
        if later.effective_from <= earlier.effective_to:
            overlaps.append((earlier.version_key, later.version_key))
    return tuple(overlaps)


# ---------------------------------------------------------------- 数据行


@dataclass(frozen=True)
class FeeRow:
    """费率表的一行。字段名与 `fba_fee_schedule` 的列一一对应。

    `included_oz` 是「base_fee 已覆盖到多少 oz」。
    为空时取 `weight_min_oz` 作为起算点——大多数档位 base 就覆盖整段，
    只有 3–20 lb 这类「首 3 lb 收 base、其后每 4 oz 加价」的档需要单独声明。

    时段（旺季/非旺季）不在这里：它属于 `RateVersion`。行上再存一份，
    就会出现「行说 peak、版本区间落在 6 月」这种两个真相源打架的局面。
    """

    version_key: str
    size_tier: str
    product_class: str
    weight_min_oz: float
    weight_max_oz: float | None
    price_band: str
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
            f"{PRODUCT_CLASS_LABELS.get(self.product_class, self.product_class)}"
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
    version_key: str,
    size_tier: str,
    weight_oz: float,
    price_band: str,
    product_class: str,
) -> FeeRow | None:
    """在版本文本 + 尺寸段 + 产品类别 + 价格带 + 重量这五个约束下挑一行。
    多个候选时取 `weight_min_oz` 最大者。

    只在这些维度上比对，从不「降到相邻尺寸段去找」，也从不
    「用非服装的费率替代服装/危险品」。
    返回 None 表示这一格里真的没有数值。
    """
    candidates = [
        row
        for row in rows
        if row.version_key == version_key
        and row.size_tier == size_tier
        and row.product_class == product_class
        and row.price_band == price_band
        and row.covers_weight(weight_oz)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row.weight_min_oz)


@dataclass(frozen=True)
class FbaQuote:
    version_key: str
    rate_period: str
    size_tier: str
    product_class: str
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
            "version_key": self.version_key,
            "rate_period": self.rate_period,
            "size_tier": self.size_tier,
            "product_class": self.product_class,
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
    versions: list[RateVersion],
    *,
    size_tier: str,
    product_class: str,
    selling_price: float,
    ship_date: date,
    weight_oz: float | None = None,
    weight_lb: float | None = None,
    fuel_surcharge_rate: float | None = None,
) -> FbaQuote:
    """查表得到一单的 FBA 费（含燃油附加费）。

    执行顺序固定：先按发货日期定「用哪一期表」，再在该期里查档位。
    反过来（先查档位、再挑版本）会在跨期时把两期数据混在一起。

    `fuel_surcharge_rate=None` 表示「本次没有可核实的附加费率」，
    此时按 0 计提，但在 assumptions 里显式记一笔——
    与「附加费是 0」在审计上必须能区分，否则成本被静默低估 3.5%。
    """
    if size_tier not in SIZE_TIERS:
        raise InputInvalid(
            f"未知的尺寸段: {size_tier!r}",
            detail=f"可选值: {', '.join(SIZE_TIERS)}。尺寸段由包装后尺寸决定，不能猜",
        )
    if product_class not in PRODUCT_CLASSES:
        # 没有默认值：猜「非服装」会让含锂电池的商品每件少算约 $0.8。
        raise InputInvalid(
            f"未知的产品类别: {product_class!r}",
            detail=(
                f"可选值: {', '.join(PRODUCT_CLASSES)}。"
                "官方按非服装 / 服装 / 危险品分三套费率，按哪套报取决于商品本身，不能默认"
            ),
        )

    weight = normalize_weight_oz(oz=weight_oz, lb=weight_lb)
    band = price_band_of(selling_price)
    season = season_of(ship_date)

    version = pick_version(versions, ship_date)
    if version is None:
        known = "、".join(
            v.version_key for v in sorted(versions, key=lambda v: v.effective_from)
        ) or "（一个版本都没登记）"
        raise ParamUnverified(
            f"发货日期 {ship_date.isoformat()}（{SEASON_LABELS[season]}）没有对应的费率版本",
            detail=(
                f"已登记的版本：{known}。查表不做跨期外推：上一期费率不能替代本期，"
                "官方未发布的期次（如 2027 旺季）必须报缺，不能拿旧表顶上"
            ),
        )
    if not version.verified:
        raise ParamUnverified(
            f"费率版本未核实：{version.dimension_label()}",
            detail=(
                f"该版本处于 {version.status} 状态，缺少官方来源或核实时间。"
                f"{version.note or ''}"
            ).strip(),
        )

    row = pick_row(
        rows,
        version_key=version.version_key,
        size_tier=size_tier,
        weight_oz=weight,
        price_band=band,
        product_class=product_class,
    )
    if row is None:
        raise ParamUnverified(
            f"FBA 费率表缺少该组合：{SIZE_TIER_LABELS[size_tier]} · "
            f"{weight:g} oz · {PRICE_BAND_LABELS[band]} · "
            f"{PRODUCT_CLASS_LABELS[product_class]} · 版本 {version.version_key}",
            detail=(
                "查表不做跨档外推：相邻尺寸段、相邻重量档不能替代本档；"
                "服装/危险品与普通商品是三套表，也不能互相替代。"
                "请补充该格的官方费率后重试，或改用已覆盖的尺寸段/重量重新量测"
            ),
        )
    if not row.verified:
        raise ParamUnverified(
            f"FBA 费率行未核实：{row.dimension_label()}",
            detail=(
                f"该行处于 {row.status} 状态，缺少官方来源。"
                f"{row.note or ''}"
            ).strip(),
        )

    assumptions: list[str] = [
        f"费率版本 {version.version_key}（{version.period_label}）："
        f"发货日期 {ship_date.isoformat()} 落在该期内，"
        f"口径为{SEASON_LABELS[season]}",
        f"价格带按售价 ${float(selling_price):.2f} 归入「{PRICE_BAND_LABELS[band]}」，"
        "边界口径为 <$10 / $10–50 / >$50",
        f"产品类别按「{PRODUCT_CLASS_LABELS[product_class]}」取费率；"
        "该维度由调用方声明，系统不代替判断商品是否含锂电池",
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

    # 2027 起低价 FBA 资格收窄：汽车、紧凑型家电、乐器/音视频制作、媒体类、电脑
    # 不再享 <$10 档，改按 $10–50 档计。这条对我们的咖啡机、电水壶、空气净化器
    # 是直接命中的——若售价 <$10 却按 <$10 档报，成本会被低估约 $0.6–1.0。
    if band == "under_10" and version.effective_from.year >= 2027:
        warnings.append(
            "2027 年起低价 FBA 资格收窄：紧凑型家电、汽车用品、电脑等类目不再享 "
            "<$10 档费率，改按 $10–50 档计。若本商品属这些类目，本次报价偏低，"
            "需按后台 Fee Preview 核实后再用于决策"
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
        version_key=version.version_key,
        rate_period=version.period_label,
        size_tier=size_tier,
        product_class=product_class,
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


# ---------------------------------------------------------------- 费用包络


@dataclass(frozen=True)
class FeeEnvelope:
    """知道售价、发货日、产品类别，但**不知道包装尺寸与实重**时的 FBA 费范围。

    这不是估计。上下界都是表里的真实费率，只是「哪一格」还没定，
    所以它给出的是可证明的界：只要该 SKU 落在已录的尺寸段内，
    真实费用一定在 [low_usd, high_usd] 里（区间内每个点都有对应的真实格子）。

    存在的理由：没有真实尺寸时，硬填一个「大概 $4」会让毛利率看起来
    精确到小数点后两位，而实际可能差一倍。包络把这件事摆到明面上——
    区间整体在阈值一侧时可以直接定论，只有跨过阈值才需要去量箱子。
    """

    version_key: str
    rate_period: str
    product_class: str
    price_band: str
    season: str
    low_usd: float
    high_usd: float
    low_row: FeeRow
    high_row: FeeRow
    row_count: int
    size_tiers: tuple[str, ...]
    fuel_surcharge_rate: float
    assumed_size_tiers: tuple[str, ...] = ()
    assumed_weight_oz: tuple[float, float] | None = None
    #: 按尺寸段分别裁剪时，各段的重量上界。见 `package.classify()`。
    assumed_weight_by_tier: tuple[tuple[str, float], ...] = ()
    #: 计费重量上界与体积重，仅在包装事实参与时给出（oz）。
    billable_weight_oz: float | None = None
    dim_weight_oz: float | None = None
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def spread_usd(self) -> float:
        return round(self.high_usd - self.low_usd, 4)

    @property
    def narrowed(self) -> bool:
        """是否用上了「部分已知」（尺寸段、重量范围或包装事实）。"""
        return (
            bool(self.assumed_size_tiers)
            or self.assumed_weight_oz is not None
            or bool(self.assumed_weight_by_tier)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "version_key": self.version_key,
            "rate_period": self.rate_period,
            "product_class": self.product_class,
            "price_band": self.price_band,
            "season": self.season,
            "low_usd": round(self.low_usd, 4),
            "high_usd": round(self.high_usd, 4),
            "spread_usd": self.spread_usd,
            "low_at": self.low_row.dimension_label(),
            "high_at": self.high_row.dimension_label(),
            "row_count": self.row_count,
            "size_tiers": list(self.size_tiers),
            "assumed_size_tiers": list(self.assumed_size_tiers),
            "assumed_weight_oz": (
                None
                if self.assumed_weight_oz is None
                else [round(self.assumed_weight_oz[0], 4), round(self.assumed_weight_oz[1], 4)]
            ),
            "assumed_weight_by_tier": {
                tier: round(limit, 4) for tier, limit in self.assumed_weight_by_tier
            },
            "billable_weight_oz": (
                None if self.billable_weight_oz is None else round(self.billable_weight_oz, 4)
            ),
            "dim_weight_oz": (
                None if self.dim_weight_oz is None else round(self.dim_weight_oz, 4)
            ),
            "fuel_surcharge_rate": round(self.fuel_surcharge_rate, 6),
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
        }


def _fee_at(row: FeeRow, weight_oz: float, fuel_rate: float) -> float:
    """某一行在某个重量上的费用（含燃油）。行的费用随重量单调不减。"""
    base = row.base_fee_usd + row.extra_weight_fee(weight_oz)
    return base * (1.0 + fuel_rate)


def fee_envelope(
    rows: list[FeeRow],
    versions: list[RateVersion],
    *,
    product_class: str,
    selling_price: float,
    ship_date: date,
    fuel_surcharge_rate: float | None = None,
    size_tiers: tuple[str, ...] | list[str] | None = None,
    weight_range_oz: tuple[float, float] | None = None,
    weight_ranges_by_tier: dict[str, tuple[float, float]] | None = None,
) -> FeeEnvelope:
    """在缺少精确尺寸与重量时，用已核实的费率行算出 FBA 费的上下界。

    取的是**端点样本**：每一行在区间下端（最便宜）与上端（最贵）各算一次，
    全部样本的最小/最大值即为包络。端点都是表里的真实数字，中间不插值——
    插值会造出一个「官方没有的费率」，而这正是本项目一直在拒绝的事。

    只扫已核实的行：draft 行的数值可能还没核对过，让它参与包络等于
    用一个未核实的数去界定一个看起来可证明的区间，比不报更糟。

    `size_tiers` / `weight_range_oz` 是**可选的收窄条件**，对应「部分已知」：
    不知道精确重量，但知道「这是大号标准件、肯定不超过 2 lb」，就该把
    这个已知条件用上，而不是拿全表 20 lb 的极值当自己的风险范围。
    不传就是全区间——那是「什么都不知道」时的正确答案，不是缺陷。

    `weight_ranges_by_tier` 是更细的收窄：不同尺寸段的重量上界本来就不同
    （小号标准件 16 oz 封顶，大号标准件按「实重与体积重取大」到 20 lb）。
    用一个统一的重量范围套两段，会让大号段的体积重被小号段的上界误伤。
    """
    if product_class not in PRODUCT_CLASSES:
        raise InputInvalid(
            f"未知的产品类别: {product_class!r}",
            detail=(
                f"可选值: {', '.join(PRODUCT_CLASSES)}。"
                "官方按非服装 / 服装 / 危险品分三套费率，包络也必须先确定用哪套"
            ),
        )

    wanted_tiers: tuple[str, ...] | None = None
    if size_tiers:
        unknown = [tier for tier in size_tiers if tier not in SIZE_TIERS]
        if unknown:
            raise InputInvalid(
                f"未知的尺寸段: {', '.join(unknown)}",
                detail=f"可选值: {', '.join(SIZE_TIERS)}。尺寸段由包装后尺寸决定，不能猜",
            )
        wanted_tiers = tuple(size_tiers)

    if weight_range_oz is not None:
        wlo, whi = (float(weight_range_oz[0]), float(weight_range_oz[1]))
        if not (math.isfinite(wlo) and math.isfinite(whi)):
            raise InputInvalid(f"重量范围必须是有限数值，当前值: {weight_range_oz!r}")
        if wlo <= 0 or whi < wlo:
            raise InputInvalid(
                f"重量范围不合法: {wlo:g}–{whi:g} oz",
                detail="下界必须大于 0，上界不得小于下界（单位 oz）",
            )
        weight_range = (wlo, whi)
    else:
        weight_range = None

    ranges_by_tier: dict[str, tuple[float, float]] = {}
    if weight_ranges_by_tier:
        unknown_tier = [t for t in weight_ranges_by_tier if t not in SIZE_TIERS]
        if unknown_tier:
            raise InputInvalid(
                f"未知的尺寸段: {', '.join(unknown_tier)}",
                detail=f"可选值: {', '.join(SIZE_TIERS)}",
            )
        for tier, pair in weight_ranges_by_tier.items():
            try:
                tlo, thi = float(pair[0]), float(pair[1])
            except (TypeError, ValueError, IndexError) as exc:
                raise InputInvalid(
                    f"尺寸段 {tier} 的重量范围不合法: {pair!r}",
                    detail="格式为 (下界, 上界)，单位 oz",
                ) from exc
            if not (math.isfinite(tlo) and math.isfinite(thi)) or tlo <= 0 or thi < tlo:
                raise InputInvalid(
                    f"尺寸段 {tier} 的重量范围不合法: {tlo:g}–{thi:g} oz",
                    detail="下界必须大于 0，上界不得小于下界",
                )
            ranges_by_tier[tier] = (tlo, thi)

    band = price_band_of(selling_price)
    season = season_of(ship_date)
    version = pick_version(versions, ship_date)
    if version is None:
        known = "、".join(
            v.version_key for v in sorted(versions, key=lambda v: v.effective_from)
        ) or "（一个版本都没登记）"
        raise ParamUnverified(
            f"发货日期 {ship_date.isoformat()}（{SEASON_LABELS[season]}）没有对应的费率版本",
            detail=f"已登记的版本：{known}。没有版本就无法界定费率范围，不能跨期借用",
        )
    if not version.verified:
        raise ParamUnverified(
            f"费率版本未核实：{version.dimension_label()}",
            detail=f"该版本处于 {version.status} 状态，缺少官方来源或核实时间",
        )

    in_version = [
        row
        for row in rows
        if row.version_key == version.version_key
        and row.product_class == product_class
        and row.price_band == band
        and row.verified
    ]
    if not in_version:
        raise ParamUnverified(
            f"费率版本 {version.version_key} 没有「"
            f"{PRODUCT_CLASS_LABELS[product_class]} · {PRICE_BAND_LABELS[band]}」的已核实行",
            detail=(
                "包络不能借用别的价格带或别的产品类别：售价落在哪个带由价格带边界决定，"
                "类别由商品本身决定，两者都不是可以四舍五入的维度"
            ),
        )

    candidates = [
        row for row in in_version if wanted_tiers is None or row.size_tier in wanted_tiers
    ]
    if not candidates:
        raise ParamUnverified(
            "指定的尺寸段在费率表里没有已核实行："
            + "、".join(SIZE_TIER_LABELS.get(t, t) for t in wanted_tiers or ()),
            detail=(
                "该期该类别已录的是："
                + "、".join(
                    sorted({SIZE_TIER_LABELS.get(r.size_tier, r.size_tier) for r in in_version})
                )
                + "。包络不能跨尺寸段借用"
            ),
        )

    capped = [row for row in candidates if row.weight_max_oz is None]
    if capped:
        # 上界未知就无法给出「最贵是多少」。当前数据全档封顶，走到这里说明录入了
        # 一个开区间档位，需要人工确认它的收费上限，不能默默取下界当上界。
        raise ParamUnverified(
            f"存在未封顶的档位，无法确定费用上界：{capped[0].dimension_label()}",
            detail="该档 weight_max_oz 为空，包络的上界无从计算，请补齐官方档位上限",
        )

    rate = 0.0 if fuel_surcharge_rate is None else float(fuel_surcharge_rate)
    samples: list[tuple[float, FeeRow]] = []
    for row in candidates:
        # 先按尺寸段的重量上界裁剪，再退回统一的重量范围。
        # 顺序不能反：统一的 2 lb 上界套在大号段上会抹掉体积重的影响。
        limit = ranges_by_tier.get(row.size_tier) or weight_range
        low_oz = row.weight_min_oz
        high_oz = float(row.weight_max_oz)
        if limit is not None:
            low_oz = max(low_oz, limit[0])
            high_oz = min(high_oz, limit[1])
            if low_oz > high_oz + _EPS:
                continue
        samples.append((_fee_at(row, low_oz, rate), row))
        samples.append((_fee_at(row, high_oz, rate), row))

    if not samples:
        if ranges_by_tier:
            shown = "、".join(
                f"{SIZE_TIER_LABELS.get(t, t)} {lo:g}–{hi:g} oz"
                for t, (lo, hi) in ranges_by_tier.items()
            )
        elif weight_range is not None:
            shown = f"{weight_range[0]:g}–{weight_range[1]:g} oz"
        else:
            shown = "（未给范围）"
        raise ParamUnverified(
            f"假定的重量范围（{shown}）不落在任何已核实行内",
            detail=(
                "该期该类别该价格带的重量覆盖是 "
                + "、".join(row.coverage_label for row in candidates[:3])
                + ("……" if len(candidates) > 3 else "")
                + "；范围完全落在表外时无法给出包络，也不能外推"
            ),
        )

    low_fee, low_row = min(samples, key=lambda item: item[0])
    high_fee, high_row = max(samples, key=lambda item: item[0])

    size_tier_set = tuple(sorted({row.size_tier for row in candidates}))
    assumptions = [
        f"费率版本 {version.version_key}（{version.period_label}）"
        f"按发货日期 {ship_date.isoformat()} 定档，口径为{SEASON_LABELS[season]}",
        f"售价 ${float(selling_price):.2f} 归入「{PRICE_BAND_LABELS[band]}」价格带",
        f"包络扫过 {len(candidates)} 个已核实行、覆盖 "
        f"{'、'.join(SIZE_TIER_LABELS.get(t, t) for t in size_tier_set)}；"
        "上下界取自重量的档位端点，中间不插值——区间内每个点都对应一个真实费率",
        "该区间不是「平均值」也不是「经验值」：它回答的是「还不知道尺寸重量时，"
        "费用最坏能到多少、最好能到多少」",
    ]
    warnings: list[str] = []
    if wanted_tiers:
        assumptions.append(
            "只在该尺寸段内取包络："
            + "、".join(SIZE_TIER_LABELS.get(t, t) for t in wanted_tiers)
            + "（由包装事实或调用方指定）；若实际不属于这些段，区间不适用"
        )
    if weight_range is not None:
        assumptions.append(
            f"重量按调用方假定的 {weight_range[0]:g}–{weight_range[1]:g} oz 取包络；"
            "这是假定范围，不是实测值——若真实重量超出该范围，费用可能高于上界"
        )
    if fuel_surcharge_rate is None:
        assumptions.append(
            "本次没有可核实的燃油附加费率，包络按 0 计提；"
            "若实际有附加费，真实费用会比上界更高，毛利率比区间下界更低"
        )
    if ranges_by_tier:
        assumptions.append(
            "各尺寸段分别按自己的计费重量上界取包络："
            + "、".join(
                f"{SIZE_TIER_LABELS.get(t, t)} ≤{hi:g} oz"
                for t, (_lo, hi) in sorted(ranges_by_tier.items())
            )
            + "（由包装事实推出，见 domain/package.py）"
        )
    if not wanted_tiers and not weight_range and not ranges_by_tier:
        warnings.append(
            f"未提供任何尺寸/重量条件，包络覆盖 {len(size_tier_set)} 个尺寸段的全重量区间，"
            f"跨度 ${high_fee - low_fee:.2f}；若知道商品的尺寸段或重量上界，"
            "传入后可显著收窄"
        )

    return FeeEnvelope(
        version_key=version.version_key,
        rate_period=version.period_label,
        product_class=product_class,
        price_band=band,
        season=season,
        low_usd=low_fee,
        high_usd=high_fee,
        low_row=low_row,
        high_row=high_row,
        row_count=len(candidates),
        size_tiers=size_tier_set,
        fuel_surcharge_rate=rate,
        assumed_size_tiers=wanted_tiers or (),
        assumed_weight_oz=weight_range,
        assumed_weight_by_tier=tuple(
            (tier, limits[1]) for tier, limits in sorted(ranges_by_tier.items())
        ),
        assumptions=tuple(assumptions),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class GroupCoverage:
    """一组（版本 × 尺寸段 × 产品类别 × 价格带）的覆盖情况。

    分组键里刻意含 `version_key`：把 2026 与 2027 的行混在一组算「有没有缺口」，
    会得出「覆盖完整」的假结论——两期各有各的洞，合起来却正好补齐。
    """

    version_key: str
    size_tier: str
    product_class: str
    price_band: str
    bands: tuple[tuple[float, float | None], ...]
    gaps: tuple[tuple[float, float | None], ...]
    overlaps: tuple[tuple[float, float], ...]
    verified: int
    draft: int

    @property
    def key(self) -> str:
        return (
            f"{self.version_key}/{self.size_tier}/"
            f"{self.product_class}/{self.price_band}"
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
    version_keys: tuple[str, ...] = ()
    version_overlaps: tuple[tuple[str, str], ...] = ()
    groups: tuple[GroupCoverage, ...] = field(default_factory=tuple)

    @property
    def gap_count(self) -> int:
        return sum(len(g.gaps) for g in self.groups)

    @property
    def overlap_count(self) -> int:
        return sum(len(g.overlaps) for g in self.groups)

    @property
    def banded_dimensions(self) -> int:
        """已声明费率的角组合数（版本 × 尺寸段 × 产品类别 × 价格带）。"""
        return len(self.groups)

    def as_dict(self) -> dict[str, object]:
        return {
            "marketplace": self.marketplace,
            "total_rows": self.total_rows,
            "verified_rows": self.verified_rows,
            "draft_rows": self.draft_rows,
            "version_keys": list(self.version_keys),
            "version_overlaps": [list(pair) for pair in self.version_overlaps],
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


def audit(
    rows: list[FeeRow],
    versions: list[RateVersion] | None = None,
    *,
    marketplace: str = "US",
) -> ScheduleAudit:
    """检查维表的完整性：哪些角有行、哪些重量段是洞、哪些区间重叠、版本区间是否打架。

    这条函数的存在意义，是把「表还不完整」从一句形容词变成一份清单。
    没有它，缺口只能靠人翻 docs 发现；有了它，`make db-fba` 就能直接
    报出「2026 旺季的 large_standard 危险品 16–20 oz 没有费率」。
    """
    grouped: dict[tuple[str, str, str, str], list[FeeRow]] = {}
    for row in rows:
        grouped.setdefault(
            (row.version_key, row.size_tier, row.product_class, row.price_band), []
        ).append(row)

    covers: list[GroupCoverage] = []
    for (version_key, size_tier, product_class, price_band), group in sorted(grouped.items()):
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
        ceiling = TIER_CEILING_OZ.get(size_tier)
        limit = math.inf if ceiling is None else ceiling
        cursor = 0.0
        for low, high in bands:
            if low > cursor + _EPS and cursor < limit - _EPS:
                # 中间断档：只报到尺寸段上界为止，不把「超出行段」算进缺口
                gaps.append((cursor, ceiling if low > limit else low))
            cursor = math.inf if high is None else max(cursor, high)
            if cursor >= limit - _EPS:
                # 已覆盖到该尺寸段的上界，再往上不属于这一段
                cursor = math.inf
                break
        if cursor != math.inf and cursor < limit - _EPS:
            gaps.append((cursor, ceiling))

        covers.append(
            GroupCoverage(
                version_key=version_key,
                size_tier=size_tier,
                product_class=product_class,
                price_band=price_band,
                bands=tuple(bands),
                gaps=tuple(gaps),
                overlaps=tuple(overlaps),
                verified=sum(1 for r in group if r.verified),
                draft=sum(1 for r in group if not r.verified),
            )
        )

    known = list(versions or [])
    return ScheduleAudit(
        marketplace=marketplace,
        total_rows=len(rows),
        verified_rows=sum(1 for r in rows if r.verified),
        draft_rows=sum(1 for r in rows if not r.verified),
        version_keys=tuple(sorted(v.version_key for v in known)),
        version_overlaps=version_overlaps(known),
        groups=tuple(covers),
    )
