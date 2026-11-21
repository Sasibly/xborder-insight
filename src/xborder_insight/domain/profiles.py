"""包装规格 → 尺寸段与费用能不能算得出来。**两档口径，两个文件，不混装。**

`cost_params` 存的是官方发布或实测的参数，状态只有 `draft` / `verified`，
`verified` 必须带来源与核实日期。这个模块存的是包装规格，分两档：

| 文件 | status | 是什么 | 能当事实用吗 |
| --- | --- | --- | --- |
| `market_typical.json` | `assumed` | 在售典型机型的公开包装规格，由品类推断单品 | **不能**。永远不得升级为 verified |
| `measured_skus.json` | `measured` | 对某台具体机器的实测，带量测人与日期 | 尺寸重量的部分可以；含锂另有 `battery_confirmed` 标记 |

为什么必须分开存：一个文件里既有猜的又有量过的，「哪几行能当事实用」就只能靠人记，
而那正是这类错误发生的地方。所以 status 是**文件级**的，加载器拒绝混装。

assumed 档存在的理由：H2 要求按 SKU 给四列，但在拿到实测值之前决策链一直停在
`NEED_MORE_INFO`。用典型规格跑一遍，能回答一个比「不知道」更有用的问题：

    这个品类里，包装尺寸会不会把商品挤出「标准尺寸」段？

如果答案是「几乎肯定会」，那这一列就不是填空题，而是必须实测的前置条件。

**假设层的结论必须自己交代可信度**，所以每个品类给两台不同机型的实机规格：

1. 两台结论一致 → 假设至少在这个品类内站得住，但仍是假设；
2. 两台结论相反 → 单点假设不可靠，结论只能是「必须实测」，不能取平均；
3. 差一点就超/差一点就过（裕度 < 5%）→ 同样按「必须实测」处理。

5% 这个阈值不是拍出来的：18×14×8 的 5% 是 0.9 / 0.7 / 0.4 英寸，比常见纸箱
公差与卷尺读数误差都小。落在这个带宽里的判定，换一把尺子就会翻转。

实测档（`measured`）要守的是另一条：**量过的和猜过的必须长得不一样**。
所以它强制每台带 `measured_at` 与 `battery_confirmed`——含锂没逐台核过就写
`battery_confirmed: false`，下游看到这个标记就知道那一列是推的，不是查的。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputInvalid, ParamUnverified
from . import package as pkg

#: 判定为「只是勉强」的裕度带宽。见模块注释：5% 小于纸箱公差与读数误差。
TIGHT_MARGIN_RATIO = 0.05

#: 两档数据口径。**一个文件只能是一种**，不允许混装。
#:   assumed  = 由品类推断单品的假设，永远不得升级（本模块的原意）
#:   measured = 对某台具体机器的实测，带量测人与日期
#: 混装会让「哪几行是猜的」变得看不出来，所以这里用文件级 status 而不是行级标记。
PROFILE_ASSUMED = "assumed"
PROFILE_MEASURED = "measured"
PROFILE_STATUSES = (PROFILE_ASSUMED, PROFILE_MEASURED)

#: 默认画像文件。放 data/ 下而不是打包进包里：它是可替换的数据，不是代码。
DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "profiles" / "market_typical.json"
)

#: 实测档画像（对真实机器的量测）。与 assumed 档**不同文件**，见模块注释。
MEASURED_PROFILE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "profiles" / "measured_skus.json"
)

_EDGE_LABELS = {1: "最长边", 2: "次长边", 3: "最短边"}

# 判定词。分开命名是为了让调用方没法把「勉强通过」当成「通过」用。
FITS_CLEAR = "fits_clear"
FITS_TIGHT = "fits_tight"
EXCEEDS_TIGHT = "exceeds_tight"
EXCEEDS_CLEAR = "exceeds_clear"

_SIZE_STATUS_LABELS = {
    FITS_CLEAR: "在标准尺寸内",
    FITS_TIGHT: "在标准尺寸内（裕度不足 5%，实测可能翻转）",
    EXCEEDS_TIGHT: "超出标准尺寸（超出不足 5%，实测可能翻转）",
    EXCEEDS_CLEAR: "超出标准尺寸",
}


@dataclass(frozen=True)
class EdgeMargin:
    """某一条边离 18×14×8 上限的距离。

    `ratio = (上限 - 实测值) / 上限`，负数表示超出。
    用相对值而不是绝对值：8 英寸上限的 1 英寸余量和 18 英寸上限的 1 英寸余量
    不是一回事，前者只要换个包装就没了。
    """

    position: int
    value: float
    limit: float
    ratio: float

    @property
    def label(self) -> str:
        return _EDGE_LABELS[self.position]

    @property
    def exceeded(self) -> bool:
        return self.ratio < 0

    @property
    def tight(self) -> bool:
        return abs(self.ratio) < TIGHT_MARGIN_RATIO

    def describe(self) -> str:
        state = "超" if self.exceeded else "余"
        return f"{self.label} {self.value:g}/{self.limit:g} 英寸，{state} {abs(self.ratio):.1%}"


@dataclass(frozen=True)
class VariantSpec:
    """一台在售机型（或一台实测机）的包装规格。"""

    model: str
    dims_in: tuple[float, float, float]
    weight_oz: float
    weight_source: str
    has_lithium_battery: bool
    battery_basis: str
    source_url: str
    source_quote: str
    #: 含锂这一列本身是否被确认过。**false 表示「取值是按证据推的，没人逐台核对过」。**
    #: assumed 档默认 True（它本来就没打算当事实）；measured 档必须显式写。
    battery_confirmed: bool = True
    #: 实测日期。只有 measured 档要求，assumed 档留空。
    measured_at: str = ""


@dataclass(frozen=True)
class CategorySpec:
    key: str
    display_name: str
    variants: tuple[VariantSpec, ...]
    #: 表格里用的短名。display_name 带括号注释，放进列里会把表撑歪。
    short_name: str = ""

    @property
    def label(self) -> str:
        return self.short_name or self.display_name


@dataclass(frozen=True)
class VariantReport:
    spec: VariantSpec
    edges: tuple[EdgeMargin, ...]
    size_status: str

    @property
    def product_class(self) -> str:
        return "dangerous_goods" if self.spec.has_lithium_battery else "non_apparel"

    @property
    def volume_in3(self) -> float:
        length, width, height = (float(v) for v in self.spec.dims_in)
        return length * width * height

    @property
    def dim_weight_oz(self) -> float:
        return pkg.dimensional_weight_oz(self.spec.dims_in)

    @property
    def billable_oz(self) -> float:
        """大号标准件计费重量上界 = max(实重, 体积重)。"""
        return max(self.spec.weight_oz, self.dim_weight_oz)

    @property
    def fits(self) -> bool:
        return self.size_status in (FITS_CLEAR, FITS_TIGHT)

    @property
    def worst_edge(self) -> EdgeMargin:
        return min(self.edges, key=lambda e: e.ratio)

    def as_facts(self) -> pkg.PackageFacts:
        return pkg.PackageFacts(
            has_lithium_battery=self.spec.has_lithium_battery,
            unit_weight_oz=self.spec.weight_oz,
            dims_in=self.spec.dims_in,
        )


# 聚合判定：这一套假设能不能直接支撑报价。
VERDICT_QUOTE = "QUOTE"
VERDICT_QUOTE_TIGHT = "QUOTE_TIGHT"
VERDICT_NOT_STANDARD = "NOT_STANDARD"
VERDICT_NOT_STANDARD_TIGHT = "NOT_STANDARD_TIGHT"
VERDICT_DIVERGENT = "DIVERGENT"

_VERDICT_LABELS = {
    VERDICT_QUOTE: "可报区间",
    VERDICT_QUOTE_TIGHT: "可报区间，但有边裕度不足 5%，须实测确认",
    VERDICT_NOT_STANDARD: "超出标准尺寸，费率未录，无法报价",
    VERDICT_NOT_STANDARD_TIGHT: "按典型规格均超出，但其中有机型超出不足 5%，实测可能翻转",
    VERDICT_DIVERGENT: "同品类两台机型结论相反，典型规格不足以定论，必须实测",
}


@dataclass(frozen=True)
class CategoryReport:
    spec: CategorySpec
    variants: tuple[VariantReport, ...]

    @property
    def size_divergent(self) -> bool:
        return len({v.fits for v in self.variants}) > 1

    @property
    def battery_divergent(self) -> bool:
        """同品类内是否既有无电池款又有含锂款。"""
        return len({v.spec.has_lithium_battery for v in self.variants}) > 1

    @property
    def verdict(self) -> str:
        if self.size_divergent:
            return VERDICT_DIVERGENT
        if any(not v.fits for v in self.variants):
            # 只要有一台只是勉强超（<5%），整个品类的「进不了标准尺寸」就不牢：
            # 换同系列另一款就可能翻进来，而费率段一翻就是「有报价/无报价」的差别。
            # 反过来，全部超得很明显时，实测也救不回来，直接给定论。
            tight = any(v.worst_edge.tight for v in self.variants)
            return VERDICT_NOT_STANDARD_TIGHT if tight else VERDICT_NOT_STANDARD
        return VERDICT_QUOTE_TIGHT if any(v.worst_edge.tight for v in self.variants) else VERDICT_QUOTE

    @property
    def bulky_quotable(self) -> bool:
        """超标准尺寸的机型是否都还能按大件报价。

        大件两段（small_bulky / large_bulky）2026 两期的费率已录（迁移 v12），
        所以「进不了标准尺寸」不再等于「报不了价」——只有超出大件范围、
        落到超大件的那部分才是真缺。判据直接复用 `package.classify`，
        不在这里另写一套尺寸比较，避免两层口径打架。
        """
        for variant in self.variants:
            if variant.fits:
                continue
            try:
                pkg.classify(variant.as_facts())
            except ParamUnverified:
                return False
        return True

    @property
    def verdict_label(self) -> str:
        if self.verdict.startswith(VERDICT_NOT_STANDARD):
            if self.bulky_quotable:
                return "超出标准尺寸，但可按大件报价（2026 两期已录，须给三边）"
            return "超过大件范围（超大件），费率未录，无法报价"
        return _VERDICT_LABELS[self.verdict]

    def headline(self) -> str:
        """一句话结论：做什么能把它变成可报价。"""
        if self.verdict == VERDICT_DIVERGENT:
            return "取一台真实在售机型量箱子，不要用品类均值"
        if self.verdict == VERDICT_NOT_STANDARD_TIGHT:
            closest = max(
                (v for v in self.variants if not v.fits),
                key=lambda v: v.worst_edge.ratio,
            )
            edge = closest.worst_edge
            if self.bulky_quotable:
                return (
                    f"先量 {closest.spec.model}：{edge.label}只超 {abs(edge.ratio):.1%}"
                    f"（{edge.value:g} vs {edge.limit:g} 英寸）。翻转进标准尺寸就能"
                    "直接报价，不翻转也落在小号大件——两条路都有价，只是差 $2–4/件"
                )
            return (
                f"先量 {closest.spec.model}：{edge.label}只超 {abs(edge.ratio):.1%}"
                f"（{edge.value:g} vs {edge.limit:g} 英寸），翻转即变成可报价"
            )
        if self.verdict == VERDICT_NOT_STANDARD:
            if self.bulky_quotable:
                return (
                    "进不了标准尺寸，但落在小号大件：给三边即可按大件费率报价，"
                    "不必改小包装"
                )
            return "超过大件范围：要么改小包装，要么等超大件费率录入"
        if self.battery_divergent:
            return "尺寸够用，但含不含锂电池必须逐 SKU 确认（费率差近两成）"
        if self.verdict == VERDICT_QUOTE_TIGHT:
            return "量一次箱子即可收窄到单一费率格"
        return "可直接用当前假设出结论，仍建议实测一次留证"


def edge_margins(dims_in) -> tuple[EdgeMargin, ...]:
    """按长度降序比对三条边，返回每条边的裕度。

    先排序再比对，与 `package.standard_edge_violation()` 同一口径——
    两边算法若不一致，会出现「这边判超、那边判过」的自相矛盾。
    """
    ordered = sorted((float(v) for v in dims_in), reverse=True)
    return tuple(
        EdgeMargin(
            position=index,
            value=value,
            limit=limit,
            ratio=(limit - value) / limit,
        )
        for index, (value, limit) in enumerate(zip(ordered, pkg.STANDARD_MAX_DIMS_IN), start=1)
    )


def _size_status(edges: tuple[EdgeMargin, ...]) -> str:
    worst = min(edges, key=lambda e: e.ratio)
    if worst.exceeded:
        return EXCEEDS_TIGHT if worst.tight else EXCEEDS_CLEAR
    return FITS_TIGHT if worst.tight else FITS_CLEAR


def build_variant(spec: VariantSpec) -> VariantReport:
    edges = edge_margins(spec.dims_in)
    return VariantReport(spec=spec, edges=edges, size_status=_size_status(edges))


def build_category(spec: CategorySpec) -> CategoryReport:
    if not spec.variants:
        raise InputInvalid(f"品类 {spec.key} 没有给任何机型规格")
    return CategoryReport(spec=spec, variants=tuple(build_variant(v) for v in spec.variants))


# ---------------------------------------------------------------- 读盘

@dataclass(frozen=True)
class Profile:
    """一整个画像文件：口径 + 证据来源 + 品类。

    `status` 是文件级的，不是行级的——一个文件里不允许既有猜的又有量过的。
    否则「哪几行能当事实用」就得靠人去记，而那正是这类错误发生的地方。
    """

    status: str
    captured_at: str
    evidence: str
    categories: tuple[CategorySpec, ...]
    marketplace: str = "US"
    path: Path | None = None

    @property
    def measured(self) -> bool:
        return self.status == PROFILE_MEASURED

    @property
    def exact_weight(self) -> bool:
        """实测档的实重是量出来的，不再表示「不确定会不会更轻」→ 区间收成单点。"""
        return self.measured

    @property
    def label(self) -> str:
        if self.measured:
            return f"实测（{self.captured_at}，来源：{self.evidence}）"
        return "假设（品类推断单品，不得当作该 SKU 的事实）"

    @property
    def unconfirmed_battery(self) -> tuple[VariantSpec, ...]:
        """含锂这一列只是推断、没人逐台核对过的机型。"""
        return tuple(
            v for c in self.categories for v in c.variants if not v.battery_confirmed
        )


def _require(obj: dict, key: str, where: str):
    if key not in obj:
        raise InputInvalid(f"{where} 缺少字段 {key!r}")
    return obj[key]


def _parse_variant(raw: dict, where: str, *, status: str) -> VariantSpec:
    dims = _require(raw, "dims_in", where)
    if not isinstance(dims, list) or len(dims) != 3:
        raise InputInvalid(f"{where} 的 dims_in 必须是三个数值（英寸），收到 {dims!r}")
    battery = _require(raw, "has_lithium_battery", where)
    if not isinstance(battery, bool):
        raise InputInvalid(
            f"{where} 的 has_lithium_battery 必须是布尔值，收到 {battery!r}",
            detail="这一列决定用哪套费率表，不能留空也不能用字符串'unknown'——"
            "未知就换一台规格已知的机型，别在这里猜",
        )
    measured_at = str(raw.get("measured_at", ""))
    battery_confirmed = raw.get("battery_confirmed", status != PROFILE_MEASURED)
    if not isinstance(battery_confirmed, bool):
        raise InputInvalid(
            f"{where} 的 battery_confirmed 必须是布尔值，收到 {battery_confirmed!r}"
        )
    if status == PROFILE_MEASURED:
        # 实测档要求「什么时候量的」和「含锂有没有核过」都写出来。
        # 少这两项，一条实测记录就和一条猜测长得一样了。
        if not measured_at:
            raise InputInvalid(
                f"{where} 属于 measured 档，但缺少 measured_at",
                detail="实测值必须带量测日期，否则无法判断它对应哪一版包装/哪一批货",
            )
        if "battery_confirmed" not in raw:
            raise InputInvalid(
                f"{where} 属于 measured 档，但缺少 battery_confirmed",
                detail="含锂决定费率表。没逐台核过就写 false——"
                "留空会被当成「已确认不含锂」，那是把未知当成通过",
            )
    return VariantSpec(
        model=str(_require(raw, "model", where)),
        dims_in=tuple(float(v) for v in dims),  # type: ignore[arg-type]
        weight_oz=float(_require(raw, "weight_oz", where)),
        weight_source=str(raw.get("weight_source", "未注明")),
        has_lithium_battery=battery,
        battery_basis=str(raw.get("battery_basis", "未注明")),
        source_url=str(raw.get("source_url", "")),
        source_quote=str(raw.get("source_quote", "")),
        battery_confirmed=battery_confirmed,
        measured_at=measured_at,
    )


def load_profile(path: Path | str | None = None) -> Profile:
    """读画像文件。坏数据在这里就报，不留到算出结论之后。

    查找顺序：显式路径 → `XBORDER_PROFILE_PATH` → 仓库内默认位置。
    留环境变量是因为默认路径按「源码树」推出来，装成包之后那几层目录不一定还在。

    两种口径（`assumed` / `measured`）不能混在一个文件里：文件级 status 决定
    整份数据能不能被当事实用，行级标记做不到这一点。
    """
    if path is not None:
        target = Path(path)
    elif override := os.environ.get("XBORDER_PROFILE_PATH"):
        target = Path(override)
    else:
        target = DEFAULT_PROFILE_PATH
    if not target.exists():
        raise InputInvalid(
            f"画像文件不存在：{target}",
            detail="市场典型规格与实测记录都是需要人工维护的数据集，不在包内",
        )
    payload = json.loads(target.read_text(encoding="utf-8"))
    status = payload.get("status")
    if status not in PROFILE_STATUSES:
        raise InputInvalid(
            f"{target.name} 的 status 必须是 {' 或 '.join(PROFILE_STATUSES)}，"
            f"收到 {status!r}",
            detail="假设不能标成 verified（下游会把它当事实用）；"
            "实测也不叫 verified——这个文件只是包装规格记录，不是成本参数",
        )
    evidence = str(payload.get("evidence", "")).strip()
    if status == PROFILE_MEASURED and not evidence:
        raise InputInvalid(
            f"{target.name} 属于 measured 档，但没有 evidence",
            detail="必须写明这批数是谁、怎么量出来的（例如「用户口述，无照片留存」）——"
            "「实测」两个字本身不构成证据",
        )
    raw_categories = payload.get("categories")
    if not isinstance(raw_categories, list) or not raw_categories:
        raise InputInvalid(f"{target.name} 没有 categories")

    categories: list[CategorySpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_categories):
        where = f"{target.name} categories[{index}]"
        key = str(_require(raw, "key", where))
        if key in seen:
            raise InputInvalid(f"{target.name} 里品类 {key!r} 重复")
        seen.add(key)
        raw_variants = _require(raw, "variants", where)
        if not isinstance(raw_variants, list) or not raw_variants:
            raise InputInvalid(f"{where} 的 variants 不能为空")
        categories.append(
            CategorySpec(
                key=key,
                display_name=str(raw.get("display_name", key)),
                variants=tuple(
                    _parse_variant(v, f"{where}.variants[{i}]", status=status)
                    for i, v in enumerate(raw_variants)
                ),
                short_name=str(raw.get("short_name", "")),
            )
        )
    return Profile(
        status=status,
        captured_at=str(payload.get("captured_at", "")),
        evidence=evidence,
        categories=tuple(categories),
        marketplace=str(payload.get("marketplace", "US")),
        path=target,
    )


def load_reports(path: Path | str | None = None) -> tuple[CategoryReport, ...]:
    return tuple(build_category(spec) for spec in load_profile(path).categories)


def find_category(reports, key: str) -> CategoryReport:
    for report in reports:
        if report.spec.key == key:
            return report
    known = ", ".join(r.spec.key for r in reports)
    raise InputInvalid(f"未知品类 {key!r}，可选：{known}")
