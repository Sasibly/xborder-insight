"""包装事实 → FBA 查表条件。

这一层解决的是「调用方知道什么」到「费率表要什么」之间的翻译：

    知道：含不含锂电池 / 包装后实重 / 三边尺寸（或只知道装不装得进 18×14×8）
    费率表要：product_class、size_tier、计费重量

不认识这一层，就会犯两类错，而且两类都不报错：

1. **含锂电池按普货算**。官方分非服装 / 危险品两套表，同一格差 18.8–25.9%。
   所以 `has_lithium_battery` 没有默认值——它是 BOM 事实，不是偏好。
2. **把实重当计费重量**。小号标准件按单位重量计费，大号标准件取「实重与体积重
   较大者」。一个实重 2 lb、包装 18×14×8 的商品，体积重 14.5 lb，按 2 lb 报价
   会少算 $5 上下。所以这里给出的从来不是「计费重量 = 实重」，而是
   「计费重量上界」，并且把它是怎么来的写清楚。

三个数字都是官方口径，不在这里重新定义：
    体积重（oz）   = 长 × 宽 × 高 ÷ 139（英寸 → 磅）× 16
    小号标准件上限 = 单位重量 16 oz + 三边 ≤ 15 / 12 / 0.75 英寸（且不取体积重）
    大号标准件上限 = 计费重量 20 lb，三边分别 ≤ 18 / 14 / 8 英寸

尺寸段定义出处：`Product size tiers`
<https://sellercentral.amazon.com/help/hub/reference/external/GG5KW835AHDJCH8W>
（2026-11-21 抓取；该页无登录墙，2026 定义表的原文即可引用）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..errors import InputInvalid, ParamUnverified

# ---------------------------------------------------------------- 官方口径

#: 体积重除数（英寸³ → 磅）。官方用 139，不是 166（166 是部分承运商的口径）。
DIM_WEIGHT_DIVISOR = 139.0

#: 大号标准件的三边上限，**按长度降序**：最长 ≤18、次长 ≤14、最短 ≤8。
STANDARD_MAX_DIMS_IN = (18.0, 14.0, 8.0)

#: 小号标准件的三边上限，**按长度降序**：最长 ≤15、次长 ≤12、最短 ≤0.75。
#: 官方原文（`Product size tiers`，2026 定义，2027-01-14 前有效）：
#: "Small standard-size | Up to 16 oz | Up to 15 inches | Up to 12 inches |
#: Up to 0.75 inch"。同一页的示例也印证：13.8 × 9 × 0.7 英寸 / 2.88 oz
#: 判为 Small standard。
SMALL_STANDARD_MAX_DIMS_IN = (15.0, 12.0, 0.75)

#: 小号标准件的单位重量上限。这一档**不取体积重**。
SMALL_STANDARD_MAX_OZ = 16.0

#: 大件的三边上限与「长 + 围」上限，`Product size tiers` 2026 定义表原文：
#: small bulky ≤ 37 / 28 / 20 英寸、large bulky ≤ 59 / 33 / 33 英寸，两者均要求
#: 长 + 围 ≤130 英寸、计费重量 ≤50 lb。按长度降序存放，与 standard 同一套比对方式。
BULKY_MAX_DIMS_IN: dict[str, tuple[float, float, float]] = {
    "small_bulky": (37.0, 28.0, 20.0),
    "large_bulky": (59.0, 33.0, 33.0),
}

#: 「长 + 围」上限。围（girth）= 2 ×（次长边 + 最短边），官方术语表原文。
BULKY_MAX_GIRTH_IN = 130.0

#: 大件两段的计费重量上限：50 lb。超过 50 lb 归超大件，费率未录。
BULKY_MAX_OZ = 50.0 * 16.0

#: 大件 / 超大件的体积重把宽、高各按**至少 2 英寸**算（官方 FAQ：
#: "For bulky items and oversize items, we assume a minimum width and height of
#: 2 inches"）。与标准尺寸口径不同，所以不能复用 `dimensional_weight_oz`。
BULKY_MIN_SIDE_IN = 2.0

#: 中文名只在本模块的说明文字里用；页面上的显示名归 `domain/fba.py` 管，
#: 这里不反过来 import 它，避免 schedule 层与 package 层互相依赖。
_BULKY_TIER_CN = {"small_bulky": "小号大件", "large_bulky": "大件"}

#: 大号标准件的计费重量上限（20 lb）。超过即不属于标准尺寸。
LARGE_STANDARD_MAX_OZ = 20.0 * 16.0

#: 重量区间的下界。费率表不接受 0 重量，用 0.1 oz 代表「最轻的那一档」。
MIN_WEIGHT_OZ = 0.1

_EPS = 1e-9


def dimensional_weight_oz(dims_in: tuple[float, float, float]) -> float:
    """体积重（oz）= 长 × 宽 × 高 ÷ 139 × 16。

    单位必须是英寸。承运商与平台的体积重口径不止一种（139 / 166 / 5000），
    用错除数的方向是**低估**费用，所以除数在这里写死，并由测试钉住。
    """
    length, width, height = (float(v) for v in dims_in)
    return length * width * height / DIM_WEIGHT_DIVISOR * 16.0


def edge_violation(
    dims_in: tuple[float, float, float],
    limits: tuple[float, float, float] = STANDARD_MAX_DIMS_IN,
) -> tuple[int, float, float] | None:
    """检查三边是否都装得进给定的三边上限。

    返回 `(第几长边(1/2/3), 实际值, 上限)`；全部满足返回 None。
    先按长度降序排，再逐位比对——不排序的话，18×8×14 这种写法会把
    次长边 14 拿去和上限 14 比、最短边 8 拿去和上限 18 比，判成通过。
    """
    ordered = sorted((float(v) for v in dims_in), reverse=True)
    for index, (value, limit) in enumerate(zip(ordered, limits), start=1):
        if value > limit + _EPS:
            return index, value, limit
    return None


def standard_edge_violation(
    dims_in: tuple[float, float, float],
) -> tuple[int, float, float] | None:
    """三边是否都装得进大号标准件的 18×14×8。"""
    return edge_violation(dims_in, STANDARD_MAX_DIMS_IN)


def small_standard_edge_violation(
    dims_in: tuple[float, float, float],
) -> tuple[int, float, float] | None:
    """三边是否都装得进小号标准件的 15×12×0.75。"""
    return edge_violation(dims_in, SMALL_STANDARD_MAX_DIMS_IN)


def girth_in(dims_in: tuple[float, float, float]) -> float:
    """长 + 围（英寸）。围 = 2 ×（次长边 + 最短边），官方术语表口径。"""
    ordered = sorted((float(v) for v in dims_in), reverse=True)
    return ordered[0] + 2.0 * (ordered[1] + ordered[2])


def bulky_dim_weight_oz(dims_in: tuple[float, float, float]) -> float:
    """大件 / 超大件的体积重：宽、高各按至少 2 英寸算。

    为什么不能复用 `dimensional_weight_oz`：一件 40×3×3 的扁长货，标准口径
    体积重 2.6 lb，官方对大件按 40×3×3 里「宽高不小于 2」算——虽然这里恰好
    都 ≥2 没有差别，但 40×1×1 这种就会从 0.29 lb 变成 1.15 lb。
    口径不同就得是两个函数，别为了省一个函数名把两套规则合并。
    """
    ordered = sorted((float(v) for v in dims_in), reverse=True)
    length, median, shortest = ordered
    return (
        length
        * max(median, BULKY_MIN_SIDE_IN)
        * max(shortest, BULKY_MIN_SIDE_IN)
        / DIM_WEIGHT_DIVISOR
        * 16.0
    )


def bulky_tier(
    dims_in: tuple[float, float, float], shipping_weight_oz: float
) -> str | None:
    """落在小件大件 / 大件 / 超大件（返回 None 表示超大件，费率未录）。

    判定顺序不能颠倒：先重量（>50 lb 直接是超大件），再「长 + 围」
    （超 130 也不是 bulky），最后才比三边。只比三边会漏掉细长件——
    59×33×33 的长+围是 191 英寸，远超 130，但它三边都没超。
    """
    if shipping_weight_oz > BULKY_MAX_OZ + _EPS:
        return None
    if girth_in(dims_in) > BULKY_MAX_GIRTH_IN + _EPS:
        return None
    for tier in ("small_bulky", "large_bulky"):
        if edge_violation(dims_in, BULKY_MAX_DIMS_IN[tier]) is None:
            return tier
    return None


def fits_standard(dims_in: tuple[float, float, float]) -> bool:
    return standard_edge_violation(dims_in) is None


# ---------------------------------------------------------------- 输入


@dataclass(frozen=True)
class PackageFacts:
    """调用方对包装知道的那点事。

    `has_lithium_battery` 故意没有默认值：它决定用哪套费率表，
    猜错的代价是每件 $1 上下且完全看不出来。
    """

    has_lithium_battery: bool
    #: 包装后实重（oz）。可以是实测值，也可以是上界——按上界用，不额外放宽。
    unit_weight_oz: float | None = None
    #: 包装后三边（英寸），顺序无所谓，内部会按长度降序。
    dims_in: tuple[float, float, float] | None = None
    #: 只知道「装不装得进 18×14×8」时用这个；给了 dims 就以 dims 为准。
    fits_standard_size: bool | None = None
    #: 包装体积上界（立方英寸）。不知道三边、但知道「肯定没超过一个鞋盒」时用这个：
    #: 它直接约束体积重，而「装得进 18×14×8」只是个二元判断，对费用几乎不构成约束。
    volume_ceiling_in3: float | None = None


@dataclass(frozen=True)
class TierLimit:
    """某个尺寸段在本次事实下的计费重量上界。"""

    size_tier: str
    max_oz: float
    basis: str


@dataclass(frozen=True)
class PackageClass:
    """包装事实的推导结果：用哪套表、可能是哪几个尺寸段、各段重量上界。"""

    product_class: str
    tiers: tuple[TierLimit, ...]
    unit_weight_oz: float | None
    dim_weight_oz: float | None
    dims_in: tuple[float, float, float] | None
    volume_ceiling_in3: float | None = None
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def weight_ranges_by_tier(
        self, *, exact: bool = False
    ) -> dict[str, tuple[float, float]]:
        """给 `fee_envelope()` 用的「按尺寸段分别裁剪重量」。

        默认下端从 `MIN_WEIGHT_OZ` 起算：实重被当作**上界**用（「不超过这么多」），
        所以不能假定实际不会更轻，区间下端必须敞着——把它收成实数，等于
        用一次可能的称量误差换一个看起来精确的数。

        `exact=True` 只在实重是**实测值**、且上游明确接受「按这一磅计费」时用：
        此时上下端都取计费重量，返回的是单点而不是区间。计量方式（不足 1 lb
        向上取整）仍由 `fee_envelope` 按官方口径处理，这里不自行取整。
        """
        if exact:
            return {t.size_tier: (t.max_oz, t.max_oz) for t in self.tiers}
        return {t.size_tier: (MIN_WEIGHT_OZ, t.max_oz) for t in self.tiers}

    @property
    def size_tiers(self) -> tuple[str, ...]:
        return tuple(t.size_tier for t in self.tiers)

    def as_dict(self) -> dict[str, object]:
        return {
            "product_class": self.product_class,
            "size_tiers": list(self.size_tiers),
            "unit_weight_oz": None if self.unit_weight_oz is None else round(self.unit_weight_oz, 4),
            "dim_weight_oz": None if self.dim_weight_oz is None else round(self.dim_weight_oz, 4),
            "dims_in": None if self.dims_in is None else [round(v, 4) for v in self.dims_in],
            "volume_ceiling_in3": self.volume_ceiling_in3,
            "tiers": [
                {
                    "size_tier": t.size_tier,
                    "max_oz": round(t.max_oz, 4),
                    "basis": t.basis,
                }
                for t in self.tiers
            ],
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------- 推导

_PRODUCT_CLASS_BY_BATTERY = {True: "dangerous_goods", False: "non_apparel"}
_PRODUCT_CLASS_LABEL = {
    "non_apparel": "普通商品（非服装）",
    "dangerous_goods": "危险品（含锂电池）",
}


def _bulky_class(
    *,
    product_class: str,
    dims: tuple[float, float, float],
    unit: float,
    reasons: list[str],
    assumptions: list[str],
) -> PackageClass:
    """按大件口径定档：先说清为什么掉出标准尺寸，再判小件大件 / 大件 / 超大件。"""
    dim_weight = bulky_dim_weight_oz(dims)
    shipping = max(unit, dim_weight)
    tier = bulky_tier(dims, shipping)
    if tier is None:
        girth = girth_in(dims)
        raise ParamUnverified(
            f"包装已超出大件范围（长+围 {girth:g} 英寸 / 计费重量 "
            f"{shipping / 16:.2f} lb），属超大件",
            detail=(
                f"大件的界线是：计费重量 ≤50 lb、长+围 ≤130 英寸、三边 ≤ "
                f"{BULKY_MAX_DIMS_IN['small_bulky'][0]:g}×"
                f"{BULKY_MAX_DIMS_IN['small_bulky'][1]:g}×"
                f"{BULKY_MAX_DIMS_IN['small_bulky'][2]:g}（小号）或 "
                f"{BULKY_MAX_DIMS_IN['large_bulky'][0]:g}×"
                f"{BULKY_MAX_DIMS_IN['large_bulky'][1]:g}×"
                f"{BULKY_MAX_DIMS_IN['large_bulky'][2]:g}（大号）英寸。"
                "超大件（extra_large）费率尚未录入，不能拿大件费率顶替"
            ),
        )
    assumptions.extend(reasons)
    assumptions.append(
        f"按大件口径计体积重：宽、高各按至少 {BULKY_MIN_SIDE_IN:g} 英寸算 → "
        f"{dim_weight:.1f} oz；计费重量取实重与体积重较大者 {shipping:.1f} oz"
        f"（{shipping / 16:.2f} lb）"
    )
    assumptions.append(
        f"尺寸段 = {_BULKY_TIER_CN[tier]}：计费重量 ≤50 lb、长+围 "
        f"{girth_in(dims):g} 英寸 ≤{BULKY_MAX_GIRTH_IN:g} 英寸，且三边均在"
        f" 37×28×20（小号）与 59×33×33（大号）之内"
    )
    return PackageClass(
        product_class=product_class,
        tiers=(
            TierLimit(
                tier,
                min(shipping, BULKY_MAX_OZ),
                "实重与体积重取大（大件体积重的宽高按 ≥2 英寸算）",
            ),
        ),
        unit_weight_oz=unit,
        dim_weight_oz=dim_weight,
        dims_in=dims,
        assumptions=tuple(assumptions),
        warnings=(),
    )


def classify(facts: PackageFacts) -> PackageClass:
    """把包装事实翻成「产品类别 + 可能的尺寸段 + 各段计费重量上界」。

    保守方向只有一个：**宁可把费用算高，不把费用算低**。所以
    「实重 2 lb 但不知道包装多大」不会得到 2 lb 的计费重量，
    而是得到 18×14×8 允许的最大体积重 14.5 lb——因为你没排除那种箱子。
    """
    if not isinstance(facts.has_lithium_battery, bool):
        raise InputInvalid(
            f"has_lithium_battery 必须是布尔值，收到 {facts.has_lithium_battery!r}",
            detail=(
                "是否含锂离子/锂金属/锂聚合物电池是 BOM 事实，没有第三态。"
                "判成普货会按非服装表计费，每件少算约 $1"
            ),
        )

    product_class = _PRODUCT_CLASS_BY_BATTERY[facts.has_lithium_battery]
    assumptions: list[str] = [
        (
            "含锂电池（锂离子/锂金属/锂聚合物任一）→ 按危险品费率表"
            if facts.has_lithium_battery
            else "不含锂电池 → 按普通商品（非服装）费率表"
        )
        + "；两套表不可混用"
    ]
    warnings: list[str] = []

    # ---------- 1. 三边：先确认还在标准尺寸范围内 ----------
    dims = _validate_dims(facts.dims_in)
    edge = None
    if dims is not None:
        edge = standard_edge_violation(dims)
        if edge is not None:
            position, value, limit = edge
            ordinal = {1: "最长边", 2: "次长边", 3: "最短边"}[position]
            unit_for_bulky = _validate_unit_weight(facts.unit_weight_oz)
            if unit_for_bulky is None:
                raise ParamUnverified(
                    f"包装{ordinal} {value:g} 英寸超过标准尺寸上限 {limit:g} 英寸，"
                    "已进入大件范围；但缺少包装实重",
                    detail=(
                        "大件费率已录（2026 两期），但定档要先知道计费重量是否 ≤50 lb："
                        "≤50 lb 是大件，>50 lb 是超大件（费率未录）。"
                        "没给实重就判不了这一点，而体积重不足以替代——实重大的箱子"
                        "可能直接落到超大件"
                    ),
                )
            return _bulky_class(
                product_class=product_class,
                dims=dims,
                unit=unit_for_bulky,
                reasons=[
                    f"包装{ordinal} {value:g} 英寸超过标准尺寸上限 {limit:g} 英寸"
                    "（三条须同时满足 18×14×8），因此不属于标准尺寸"
                ],
                assumptions=assumptions,
            )
        if facts.fits_standard_size is False:
            raise InputInvalid(
                "dims_in 三边均在 18×14×8 以内，与 fits_standard_size=False 矛盾",
                detail="两处说法不一致时不能猜哪个对，请改掉其中一个",
            )
    else:
        if facts.fits_standard_size is None:
            raise InputInvalid(
                "缺少包装三边，也未声明能否装进 18×14×8",
                detail=(
                    "这是判断题不是估算题：最长边是否 ≤18、次长边是否 ≤14、"
                    "最短边是否 ≤8。三条同时满足才算标准尺寸；不知道就没法定尺寸段，"
                    "而尺寸段不能猜——猜小一档费率低 $0.5–1"
                ),
            )
        if not facts.fits_standard_size:
            raise ParamUnverified(
                "声明装不进 18×14×8，但没给三边，无法在大件与小件大件之间定档",
                detail=(
                    "大件费率已录（small_bulky / large_bulky，2026 两期），但这两段的"
                    "界线是 37×28×20 与 59×33×33 英寸、长+围 130 英寸——只知道"
                    "「装不进 18×14×8」的话，这三种可能都还在，费用差一倍以上。"
                    "给三边（长×宽×高，英寸）即可定档"
                ),
            )
        assumptions.append(
            "按调用方声明「三边均在 18×14×8 以内」定标准尺寸；"
            "这一条是声明，不是实测——任一条超出即不属于标准尺寸"
        )

    # ---------- 2. 体积重 ----------
    volume_ceiling = _validate_volume(facts.volume_ceiling_in3)
    if dims is not None:
        dim_weight = dimensional_weight_oz(dims)
        actual_volume = dims[0] * dims[1] * dims[2]
        if volume_ceiling is not None and actual_volume > volume_ceiling + _EPS:
            raise InputInvalid(
                f"三边算出的体积 {actual_volume:g} in³ 超过声明的体积上界 "
                f"{volume_ceiling:g} in³",
                detail="两处说法不一致时不能猜哪个对，请改掉其中一个",
            )
        assumptions.append(
            f"体积重 {dim_weight:.1f} oz（{dims[0]:g}×{dims[1]:g}×{dims[2]:g} 英寸"
            f" ÷ {DIM_WEIGHT_DIVISOR:g} × 16）；大号标准件取实重与此值较大者计费"
        )
    elif volume_ceiling is not None:
        # 有体积上界：体积重随之有上界，这比「装得进 18×14×8」有用得多。
        dim_weight = volume_ceiling / DIM_WEIGHT_DIVISOR * 16.0
        assumptions.append(
            f"未给三边，体积按声明的上界 {volume_ceiling:g} in³ 计 → "
            f"体积重上界 {dim_weight:.1f} oz（{dim_weight / 16:.2f} lb）"
        )
    else:
        # 没给三边，也没有体积上界：体积重的可能上界就是 18×14×8 装满时。
        dim_weight = dimensional_weight_oz(STANDARD_MAX_DIMS_IN)
        assumptions.append(
            f"未给三边，体积重按 18×14×8 的上界 {dim_weight:.1f} oz"
            f"（={dim_weight / 16:.2f} lb）计；要收窄就补三边尺寸或体积上界"
        )
        warnings.append(
            f"大号标准件按「实重与体积重较大者」计费，体积重上界 "
            f"{dim_weight:.1f} oz 远高于你给的实重上界——若包装接近 18×14×8，"
            "费用按体积重走，不按实重走。只知道「装得进」不足以收窄费用："
            "那是个二元判断，不约束体积重"
        )

    # ---------- 3. 实重 ----------
    unit = _validate_unit_weight(facts.unit_weight_oz)

    tiers: list[TierLimit] = []
    if unit is None:
        billable = LARGE_STANDARD_MAX_OZ
        assumptions.append(
            "未给实重，小号标准件按 16 oz 上界、大号标准件按 20 lb 上界取包络"
        )
        small_edge = None if dims is None else small_standard_edge_violation(dims)
        if small_edge is not None:
            # 三边已经超出 15×12×0.75，无论多重都不可能是小号标准件。
            position, value, limit = small_edge
            ordinal = {1: "最长边", 2: "次长边", 3: "最短边"}[position]
            assumptions.append(
                f"{ordinal} {value:g} 英寸超过小号标准件上限 {limit:g} 英寸，"
                "已排除 small_standard"
            )
        else:
            tiers.append(
                TierLimit("small_standard", SMALL_STANDARD_MAX_OZ, "单位重量（不取体积重）")
            )
        tiers.append(
            TierLimit("large_standard", LARGE_STANDARD_MAX_OZ, "实重与体积重取大")
        )
        return PackageClass(
            product_class=product_class,
            tiers=tuple(tiers),
            unit_weight_oz=None,
            dim_weight_oz=dim_weight,
            dims_in=dims,
            volume_ceiling_in3=volume_ceiling,
            assumptions=tuple(assumptions),
            warnings=tuple(warnings),
        )

    if unit > LARGE_STANDARD_MAX_OZ + _EPS:
        if dims is not None:
            # 三边在 18×14×8 以内但实重过 20 lb：官方按计费重量定档，
            # 这一段属于大件，不是「装不进去」。
            return _bulky_class(
                product_class=product_class,
                dims=dims,
                unit=unit,
                reasons=[
                    f"包装实重 {unit / 16:.2f} lb 超过大号标准件上限 20 lb，"
                    "虽然三边装得进 18×14×8，但按计费重量定档仍不属于标准尺寸"
                ],
                assumptions=assumptions,
            )
        shipping = max(unit, dim_weight)
        if shipping > BULKY_MAX_OZ + _EPS:
            raise ParamUnverified(
                f"计费重量 {shipping / 16:.2f} lb 超过大件上限 50 lb，属超大件",
                detail="超大件（extra_large）费率尚未录入，不能拿大件费率顶替",
            )
        assumptions.append(
            f"三边按声明的 18×14×8 上界计体积重 {dim_weight:.1f} oz，"
            f"计费重量取实重与体积重较大者 {shipping:.1f} oz（{shipping / 16:.2f} lb）；"
            "三边未量，长+围按 18×14×8 的上界 62 英寸算，仍在大件的 130 英寸以内"
        )
        return PackageClass(
            product_class=product_class,
            tiers=(
                TierLimit(
                    "small_bulky",
                    min(shipping, BULKY_MAX_OZ),
                    "实重与体积重取大（大件体积重的宽高按 ≥2 英寸算）",
                ),
            ),
            unit_weight_oz=unit,
            dim_weight_oz=dim_weight,
            dims_in=None,
            volume_ceiling_in3=volume_ceiling,
            assumptions=tuple(assumptions),
            warnings=tuple(warnings),
        )

    billable_dims = max(unit, dim_weight)
    if billable_dims > LARGE_STANDARD_MAX_OZ + _EPS:
        raise ParamUnverified(
            f"计费重量 {billable_dims / 16:.2f} lb 超过大号标准件上限 20 lb，"
            "不属于标准尺寸",
            detail=(
                f"实重 {unit / 16:.2f} lb、体积重 {dim_weight / 16:.2f} lb，取大者 "
                f"{billable_dims / 16:.2f} lb。体积重超标同样会掉出标准尺寸——"
                "大件费率已录，但需要三边才能定档（体积重与长+围都要三边）"
            ),
        )

    # 小号标准件：单位重量 ≤16 oz **且**三边都装得进 15×12×0.75，且不取体积重。
    definitely_small = False
    if unit <= SMALL_STANDARD_MAX_OZ + _EPS:
        if dims is None:
            tiers.append(
                TierLimit("small_standard", unit, "单位重量（不取体积重）")
            )
            warnings.append(
                "实重 ≤16 oz 只说明**够得着**小号标准件；没给三边就判不了它的"
                "15×12×0.75 上限（尤其最短边 0.75 英寸，多数纸箱都不满足），"
                "所以 small_standard 与 large_standard 两段都留在区间里。"
                "补三边即可收敛到一段"
            )
        else:
            small_edge = small_standard_edge_violation(dims)
            if small_edge is None:
                # 三边与小号标准件上限相符，且实重 ≤16 oz：只可能是小号标准件。
                tiers.append(
                    TierLimit("small_standard", unit, "单位重量（不取体积重）")
                )
                definitely_small = True
                assumptions.append(
                    "实重 ≤16 oz 且三边均在 15×12×0.75 以内 → 小号标准件，"
                    "只可能是这一段（不再保留大号标准件）"
                )
            else:
                position, value, limit = small_edge
                ordinal = {1: "最长边", 2: "次长边", 3: "最短边"}[position]
                assumptions.append(
                    f"实重 ≤16 oz，但{ordinal} {value:g} 英寸超过小号标准件上限 "
                    f"{limit:g} 英寸 → 只能是大号标准件，已排除 small_standard"
                )

    if not definitely_small:
        tiers.append(
            TierLimit(
                "large_standard",
                min(billable_dims, LARGE_STANDARD_MAX_OZ),
                "实重与体积重取大",
            )
        )
        assumptions.append(
            f"实重 {unit:.1f} oz、体积重 {dim_weight:.1f} oz，"
            f"大号标准件计费重量按较大者 {billable_dims:.1f} oz 取上界；"
            "实重按上界用，费用区间下端仍从最轻档起算——给的是「不超过多少」，"
            "不能据此假定实际不会更轻"
        )
    return PackageClass(
        product_class=product_class,
        tiers=tuple(tiers),
        unit_weight_oz=unit,
        dim_weight_oz=dim_weight,
        dims_in=dims,
        volume_ceiling_in3=volume_ceiling,
        assumptions=tuple(assumptions),
        warnings=tuple(warnings),
    )


def _validate_dims(
    dims: tuple[float, float, float] | None,
) -> tuple[float, float, float] | None:
    if dims is None:
        return None
    try:
        values = tuple(float(v) for v in dims)
    except (TypeError, ValueError) as exc:
        raise InputInvalid(
            f"包装三边必须是三个数值（英寸），收到 {dims!r}",
            detail="例：(12, 9, 6) 表示 12×9×6 英寸；单位必须统一为英寸",
        ) from exc
    if len(values) != 3:
        raise InputInvalid(f"包装三边必须给满三个值，收到 {len(values)} 个：{dims!r}")
    if any(not math.isfinite(v) or v <= 0 for v in values):
        raise InputInvalid(
            f"包装三边必须是正的有限数值，收到 {dims!r}",
            detail="量不出某一边时不要填 0 或估算值：填 0 会让体积重变成 0，"
            "等于把费用算成最低档",
        )
    return values  # type: ignore[return-value]


def _validate_unit_weight(unit_oz: float | None) -> float | None:
    if unit_oz is None:
        return None
    try:
        value = float(unit_oz)
    except (TypeError, ValueError) as exc:
        raise InputInvalid(f"实重必须是数值（oz），收到 {unit_oz!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise InputInvalid(
            f"实重必须是正的有限数值，收到 {unit_oz!r}",
            detail="没有实重就传 None，由包络按尺寸段上限取界；填 0 会让费用按最轻档算",
        )
    return value


def _validate_volume(volume_in3: float | None) -> float | None:
    if volume_in3 is None:
        return None
    try:
        value = float(volume_in3)
    except (TypeError, ValueError) as exc:
        raise InputInvalid(
            f"体积上界必须是数值（立方英寸），收到 {volume_in3!r}"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise InputInvalid(
            f"体积上界必须是正的有限数值，收到 {volume_in3!r}",
            detail="没有体积上界就传 None；填 0 会让体积重变成 0，等于按最轻档计费",
        )
    return value


# ------------------------------------------------ 两套读数能否同时成立
#
# 同一个 SKU 常常有两套「尺寸 + 重量」：一套是量箱子量出来的，一套是供应商
# 规格书或商品详情页抄的。两套都填进费率表不报错，但会得到差 19% 的费用。
#
# 这两套读数本身受一条硬约束：**彩盒包着机器，所以只能更大、更重**。
# 于是不需要知道哪套是真的，就能判出「哪套不可能是彩盒」：
#
#   体积更大、重量也更重  → 那套是箱，另一套是机（可以放心用箱那套）
#   体积更大、反而更轻    → 两套里至少一套错了
#   三边几乎相同、重量不同 → 同一外箱尺寸不可能既含箱又不含箱，至少一套错了
#
# 这条判定不做「取保守值」这种折中——折中出来的数既不等于箱也不等于机，
# 只是看起来像个决策。

#: 两套三边算作「同一外箱」的相对体积容差。2% 对应几毫米的测量噪声。
SAME_BOX_VOLUME_TOL = 0.02

#: 毛重判作「不同」的最小差值（磅）。低于它属于秤的重复性误差。
SAME_BOX_WEIGHT_TOL_LB = 0.1

SPEC_BOX_VS_UNIT = "box_vs_unit"
SPEC_CONTRADICTION_WEIGHT = "contradiction_weight"
SPEC_CONTRADICTION_SAME_BOX = "contradiction_same_box"
SPEC_INDISTINGUISHABLE = "indistinguishable"


@dataclass(frozen=True)
class SpecReading:
    """一条「三边 + 重量」读数。`source` 要写清出处，否则两套数没有可比性。"""

    label: str
    dims_in: tuple[float, float, float]
    weight_lb: float
    source: str = ""

    @property
    def volume_in3(self) -> float:
        length, width, height = self.dims_in
        return float(length * width * height)


@dataclass(frozen=True)
class SpecRelation:
    """两条读数之间的关系。`box` / `unit` 只在判得出时才给，判不出就是 None。"""

    verdict: str
    reason: str
    box: str | None
    unit: str | None
    volume_ratio: float
    weight_delta_lb: float


def compare_packaging(first: SpecReading, second: SpecReading) -> SpecRelation:
    """判两条读数哪条是彩盒、哪条是裸机，或判它们不可能同时成立。"""
    for reading in (first, second):
        if reading.volume_in3 <= 0 or reading.weight_lb <= 0:
            raise InputInvalid(
                f"读数 {reading.label!r} 的三边与重量必须为正，收到 "
                f"{reading.dims_in!r} / {reading.weight_lb!r}"
            )

    small, large = sorted((first, second), key=lambda r: r.volume_in3)
    volume_ratio = small.volume_in3 / large.volume_in3
    weight_delta = abs(first.weight_lb - second.weight_lb)
    same_volume = volume_ratio >= 1.0 - SAME_BOX_VOLUME_TOL
    same_weight = weight_delta <= SAME_BOX_WEIGHT_TOL_LB

    if same_volume and not same_weight:
        return SpecRelation(
            verdict=SPEC_CONTRADICTION_SAME_BOX,
            reason=(
                f"三边几乎相同（体积比 {volume_ratio:.1%}）却差 {weight_delta:.2f} lb。"
                "同一个外箱尺寸不可能一套含箱、一套不含箱——至少一套不是这台的"
            ),
            box=None,
            unit=None,
            volume_ratio=volume_ratio,
            weight_delta_lb=weight_delta,
        )

    if same_volume and same_weight:
        return SpecRelation(
            verdict=SPEC_INDISTINGUISHABLE,
            reason="三边与重量都在噪声范围内，两套是同一个数，不构成分歧",
            box=None,
            unit=None,
            volume_ratio=volume_ratio,
            weight_delta_lb=weight_delta,
        )

    if large.weight_lb > small.weight_lb:
        return SpecRelation(
            verdict=SPEC_BOX_VS_UNIT,
            reason=(
                f"体积大的那套同时更重（+{weight_delta:.2f} lb），符合「箱包着机」；"
                f"体积比 {volume_ratio:.1%}，小的那套只能是裸机"
            ),
            box=large.label,
            unit=small.label,
            volume_ratio=volume_ratio,
            weight_delta_lb=weight_delta,
        )

    return SpecRelation(
        verdict=SPEC_CONTRADICTION_WEIGHT,
        reason=(
            f"体积大的那套反而轻 {weight_delta:.2f} lb（体积比 {volume_ratio:.1%}）。"
            "箱子不可能比它装的机器还轻，两套里至少一套错了"
        ),
        box=None,
        unit=None,
        volume_ratio=volume_ratio,
        weight_delta_lb=weight_delta,
    )


def usable_as_box(*readings: SpecReading) -> SpecReading | None:
    """从若干条读数里挑出唯一可用作「彩盒」的那条；判不出就返回 None。

    单条读数直接返回它自己（没有第二条可对照，只能用它，但调用方应把它标成
    未核实）。两条争不出结果时返回 None——此时**不能**按体积或重量挑一个
    更保守的顶上，因为「保守」在这里没有方向：更小的箱意味着更低的费，
    而错的低费正是这类错误最贵的形态。
    """
    if not readings:
        return None
    if len(readings) == 1:
        return readings[0]
    if len(readings) > 2:
        raise InputInvalid("目前只支持两套读数互校；三套以上请先人工对齐口径")
    relation = compare_packaging(readings[0], readings[1])
    if relation.verdict != SPEC_BOX_VS_UNIT:
        return None
    return readings[0] if relation.box == readings[0].label else readings[1]
