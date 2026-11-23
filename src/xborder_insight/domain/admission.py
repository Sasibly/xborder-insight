"""候选准入：这个品在目标毛利线下还剩多少空间。

`docs/04` §18 用一条不等式回答了「售价、采购、头程、FBA 之间必须满足什么」：

    售价 × 0.6176 ≥ 采购 × (1.024 + 税率) + 头程 × 1.016 + $0.24 + FBA

那个式子写成文档只能读，不能筛。这里把它变成三个可执行的东西：

1. `screening_gate()` —— 闸一。只用「售价 + 采购价」两个数，不碰样品，
   所以它能在报价阶段就把不成立的候选剔掉。代价是粗：它不看 FBA、不看尺寸段。
2. `evaluate()` —— 闸二。给了 FBA（或品类占比）之后，用利润模型算出真实毛利，
   判 pass / fail，并在 fail 时直接给出「采购必须降到多少」「差多少点」。
3. `Candidate` —— 输入。**缺项一律 need_info**，不给默认值。

为什么要分两道闸而不是一道：闸一要等样品量箱规，闸二不用。
先做零成本的那道，能把询价的工作量砍掉一个量级。反过来的顺序（先拿样品
再算账）就是这五台已经走过的路——五台全 NO_GO，其中三台连议价空间都没有。

为什么缺项宁可返回 need_info 也不填默认值：
    税率填一个「大概 12.5%」会让毛利率静默偏高 1–11 个点，而输出上没有任何痕迹。
    与 `profit.CostInputs.fba_fee` 不设默认值是同一条纪律：填错的数比没有数危险。

口径状态：0.6176 这个分母来自佣金 15% / 退货 8% / 目标毛利 15% 三个假设，
三个都还没全部核实（佣金见 `H1`，退货见 `H3`）。所以任何对外引用都要连
假设一起写，不要把它当成常数引用。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import profit as p

#: 判定三值。与 `transport` 的 ok/blocked/need_info 同构，但语义相反：
#: 这里 pass 是「有空间」，fail 是「数学上做不成」，need_info 是「还判不了」。
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_NEED_INFO = "need_info"

VERDICT_LABELS = {
    VERDICT_PASS: "有空间",
    VERDICT_FAIL: "做不成",
    VERDICT_NEED_INFO: "还判不了",
}

#: 目标毛利率。换它就要重算 0.6176 与下面所有阈值，不能只改这一个数。
TARGET_MARGIN = 0.15

#: 广告空间线：盈亏平衡 ACOS 的下限，与 `decision.MIN_BREAKEVEN_ACOS` 是同一个数。
#:
#: 为什么要在准入里也算这一条：盈亏平衡 ACOS **数值上等于毛利率**（`profit.py`
#: 里 `breakeven_acos = gross_profit / selling_price`），所以「毛利率 ≥ 15%」
#: 和「ACOS 上限 ≥ 20%」不是两条独立的门槛，后者严格更紧——毛利率 15% 的品，
#: ACOS 上限就是 15%，在真实竞价环境里投不出量（`decision.py` 原文：
#: 「有毛利但卖不动」）。
#:
#: 两条线因此回答两个不同的问题，谈价时不能混用：
#:   15% 线 —— 「这单做下来还剩多少」，是 `docs/04` §18 那条不等式的口径；
#:   20% 线 —— 「还剩的这点够不够把货卖出去」，是 `docs/04` §10.4 与 §15.5 的口径。
#:
#: 分工是拍过板的（H22，11-23 14:52，`docs/04` §22.7）：**15% 决定谈不谈，
#: 20% 决定谈到多少**。所以准入式与闸一仍按 15% 算，而开口价要落在 20% 线上——
#: 否则谈成的价仍然是「有毛利、卖不动」。别把这条线拿去改 `TARGET_MARGIN`：
#: 那会连 `0.6176` 与闸一 $18.30 一起推翻，五台会全灭。
AD_SPACE_FLOOR = 0.20

#: 三个口径假设。改成 `verified` 之前，任何对外数字都要连这三个前提一起写。
DEFAULT_REFERRAL = 0.15
DEFAULT_RETURNS = 0.08
DEFAULT_FREIGHT = 6.00

#: 闸一的第一段：采购金额上限。来源是用户给定的筛法（`docs/07` 第 4 条），
#: 它对应 `docs/04` §18.3 表里「$60 售价 × FBA 15% → $18.90」那一格。
GATE_AMOUNT = 18.30

#: 采购 $18.30 在**最坏**尺寸档（FBA $8.95，小号大件下限、12.5% 税率）下
#: 所需的最低售价。由 `min_price_for_cogs(18.30, 8.95, 0.125)` 反解得到，
#: 不是拍的：低于这个售价，$18.30 的采购在数学上就做不出 15%。
GATE_AMOUNT_MIN_PRICE = 59.0

#: 闸一的中价段：比例上限。低价段 FBA 占比会飙到 30% 以上，金额闸一律失效，
#: 所以中价段改用比例；低于 `GATE_RATIO_MIN_PRICE` 则连比例也不可靠。
GATE_RATIO = 0.20
GATE_RATIO_MIN_PRICE = 40.0


class AdmissionInputInvalid(ValueError):
    """输入本身不成立（负数、售价为 0 等），不是「还判不了」。"""


@dataclass(frozen=True)
class Candidate:
    """一个候选品的已知信息。**允许缺项**，缺什么由 `evaluate()` 报出来。

    duty_rate 没有默认值：候选通常还没做 HTS 归类，但归类是判定的一部分，
    猜一个税率会让结论静默偏乐观。要试算请显式传最坏档（如 0.237），
    那样结论里会带上「按最坏税率」的前提。
    """

    name: str
    selling_price: float | None
    quoted_cost: float | None
    duty_rate: float | None
    fba_fee: float | None = None
    fba_ratio: float | None = None
    inbound_freight: float = DEFAULT_FREIGHT
    referral_rate: float = DEFAULT_REFERRAL
    return_rate: float = DEFAULT_RETURNS
    incoterm: str = "FOB"


@dataclass(frozen=True)
class GateVerdict:
    """闸一的结论：只回答「按售价带，采购价是否还在上限内」。"""

    verdict: str
    band: str
    limit: float | None
    reason: str

    @property
    def label(self) -> str:
        return VERDICT_LABELS[self.verdict]


@dataclass(frozen=True)
class Admission:
    """闸二的结论。fail 时四个数一起给，因为它们回答的是不同的问题：

    - `max_cogs`：谈判目标价（「采购必须 ≤ 这个数」）
    - `cogs_reduction_pct`：还价幅度（「要降几个点」）
    - `overrun_pts`：缺口（「差多少毛利点」，与 `docs/04` §18.2 同一口径）
    - `min_price`：定价目标（「售价要提到多少才够」）
    """

    name: str
    verdict: str
    reason: str | None
    missing: tuple[str, ...]
    gross_margin: float | None = None
    gross_profit: float | None = None
    max_cogs: float | None = None
    cogs_reduction_pct: float | None = None
    overrun_pts: float | None = None
    min_price: float | None = None
    #: 广告空间线（`AD_SPACE_FLOOR`）上的两个对应数。`max_cogs` / `min_price`
    #: 是 15% 线的，两者混用会把「有毛利、卖不动」当成谈得成。
    max_cogs_ad_space: float | None = None
    min_price_ad_space: float | None = None
    gate: GateVerdict | None = None
    notes: tuple[str, ...] = ()
    #: 原样带回输入的两个数。批量输出要显示它们，但 `Admission` 本身不该
    #: 让调用方去别处找——那会逼着 CLI 再维护一份 name→输入 的映射。
    selling_price: float | None = None
    quoted_cost: float | None = None

    @property
    def label(self) -> str:
        return VERDICT_LABELS[self.verdict]


def _finite(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise AdmissionInputInvalid(f"{name} 必须是有限数值，当前值: {value!r}")
    if number <= 0:
        raise AdmissionInputInvalid(f"{name} 必须为正数，当前值: {value!r}")
    return number


def _model(
    candidate: Candidate, cogs: float, fba_fee: float, price: float | None = None
) -> p.ProfitResult:
    return p.compute(
        p.CostInputs(
            quoted_price=cogs,
            selling_price=float(candidate.selling_price if price is None else price),
            inbound_freight=candidate.inbound_freight,
            # 走到这里时 duty_rate 已确认非 None。
            duty_rate=float(candidate.duty_rate or 0.0),
            referral_rate=candidate.referral_rate,
            fba_fee=fba_fee,
            return_rate=candidate.return_rate,
            returns_handling_fee=3.0,
            returns_refund_admin_rate=0.20,
            returns_goods_loss_ratio=0.30,
            returns_inbound_loss_ratio=0.20,
            incoterm=candidate.incoterm,
        )
    )


def screening_gate(price: float | None, cogs: float | None) -> GateVerdict:
    """闸一：按售价带判断采购价是否还在上限内（`docs/04` §18.7 的三段）。

    它**故意**不看 FBA 与尺寸段——那要样品。所以结论只能当粗筛：

        pass  → 值得进入闸二（量箱规、做归类）
        fail  → 可以现在就放弃
        need_info → 售价太低，金额闸不成立，先定尺寸段
    """
    price = _finite("售价", price)
    cogs = _finite("采购价", cogs)
    if price is None or cogs is None:
        return GateVerdict(
            verdict=VERDICT_NEED_INFO,
            band="unknown",
            limit=None,
            reason="闸一需要售价与采购价两个数，缺一个就判不了",
        )
    if price >= GATE_AMOUNT_MIN_PRICE:
        limit = GATE_AMOUNT
        band = "amount"
        rule = f"售价 ≥ ${GATE_AMOUNT_MIN_PRICE:.0f}：采购 ≤ ${GATE_AMOUNT:.2f}"
    elif price >= GATE_RATIO_MIN_PRICE:
        limit = GATE_RATIO * price
        band = "ratio"
        rule = f"售价 ${GATE_RATIO_MIN_PRICE:.0f}–{GATE_AMOUNT_MIN_PRICE:.0f}：采购 ≤ 售价的 {GATE_RATIO:.0%}"
    else:
        return GateVerdict(
            verdict=VERDICT_NEED_INFO,
            band="size_first",
            limit=None,
            reason=(
                f"售价 ${price:.2f} < ${GATE_RATIO_MIN_PRICE:.0f}：这个价位 FBA 占比会到 30% 以上，"
                "任何金额闸都会放过做不成的候选。先确认尺寸段（落小号大件/大件直接放弃）"
            ),
        )
    if cogs <= limit:
        return GateVerdict(
            VERDICT_PASS, band, limit, f"{rule} → 采购 ${cogs:.2f} 在限内"
        )
    return GateVerdict(
        VERDICT_FAIL,
        band,
        limit,
        f"{rule} → 采购 ${cogs:.2f} 超 ${cogs - limit:.2f}",
    )


def _solve_max_cogs(
    candidate: Candidate, fba_fee: float, target: float = TARGET_MARGIN
) -> float | None:
    """二分求「毛利率正好等于 target」时的采购价。没有闭式解可用：
    售价 ≥ $166.67 时退款管理费撞 `min($5, 20%×佣金)` 的上限，线性解会偏高。
    采购价为 0 都达不到目标时返回 None（说明售价太低，不是采购太贵）。

    `target` 显式传参是为了同时解 15% 线与 20% 广告空间线：两条线只差这一个数，
    复制一份函数会让两条线日后各改各的。
    """
    low, high = 0.01, float(candidate.selling_price or 0.0) * 2
    if _model(candidate, low, fba_fee).gross_margin < target:
        return None
    for _ in range(120):
        mid = (low + high) / 2
        if _model(candidate, mid, fba_fee).gross_margin < target:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def _solve_min_price(
    candidate: Candidate, fba_fee: float, target: float = TARGET_MARGIN
) -> float | None:
    """二分求「毛利率正好等于 target」时的售价。采购为 0 都到不了目标时返回 None。"""
    cogs = float(candidate.quoted_cost or 0.0)
    low, high = cogs * 1.01, cogs * 30
    if _model(candidate, cogs, fba_fee, price=high).gross_margin < target:
        return None
    for _ in range(120):
        mid = (low + high) / 2
        if _model(candidate, cogs, fba_fee, price=mid).gross_margin < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def _notes(gate: GateVerdict, margin: float) -> tuple[str, ...]:
    """两道闸结论打架时要说明白谁说了算——否则读的人会挑自己爱听的那个。"""
    notes: list[str] = []
    if gate.verdict == VERDICT_FAIL and margin >= TARGET_MARGIN:
        notes.append(
            "闸一判「做不成」但闸二判「有空间」：闸一只看售价带、不看尺寸段，"
            "这一台是它误杀——以闸二为准（`docs/04` §18.7 末尾那个样本）"
        )
    if gate.verdict == VERDICT_NEED_INFO and gate.band == "size_first":
        notes.append(
            "售价 < $40：闸一在低价段失效（FBA 占比会到 30% 以上），"
            "直接看闸二；闸二也缺尺寸时先量箱规"
        )
    return tuple(notes)


def evaluate(candidate: Candidate) -> Admission:
    """闸二：用利润模型算真实毛利，并给出谈判/定价要用的四个数。

    缺项的处理顺序刻意写成「先收集全部缺项再返回」，而不是遇到第一个就抛错：
    候选通常一次缺三四样，一次报全才能一次补全。
    """
    missing: list[str] = []
    price = _finite("售价", candidate.selling_price)
    cogs = _finite("采购价", candidate.quoted_cost)
    duty = candidate.duty_rate

    if price is None:
        missing.append("售价（selling_price）")
    if cogs is None:
        missing.append("采购价（quoted_cost）")
    if duty is None:
        missing.append("关税率（duty_rate）——先做 HTS 归类，或用最坏档显式试算")
    elif not 0 <= float(duty) <= 1:
        raise AdmissionInputInvalid(f"关税率应在 0–1 之间，当前值: {duty!r}")

    fba_fee = candidate.fba_fee
    if fba_fee is None and candidate.fba_ratio is not None and price is not None:
        fba_fee = float(candidate.fba_ratio) * price
    if fba_fee is None:
        missing.append("FBA 费（fba_fee）或品类典型占比（fba_ratio）")

    if missing:
        return Admission(
            name=candidate.name,
            verdict=VERDICT_NEED_INFO,
            reason="输入不全，判不了——补齐后再跑，不会用默认值替你猜",
            missing=tuple(missing),
            gate=screening_gate(price, cogs),
            selling_price=price,
            quoted_cost=cogs,
        )

    assert price is not None and cogs is not None and fba_fee is not None
    result = _model(candidate, cogs, fba_fee)
    margin = result.gross_margin
    gate = screening_gate(price, cogs)
    max_cogs = _solve_max_cogs(candidate, fba_fee)
    min_price = _solve_min_price(candidate, fba_fee)
    max_cogs_ad = _solve_max_cogs(candidate, fba_fee, AD_SPACE_FLOOR)
    min_price_ad = _solve_min_price(candidate, fba_fee, AD_SPACE_FLOOR)

    if margin >= TARGET_MARGIN:
        return Admission(
            name=candidate.name,
            verdict=VERDICT_PASS,
            reason=f"毛利 {margin:.2%} ≥ 目标 {TARGET_MARGIN:.0%}",
            missing=(),
            gross_margin=margin,
            gross_profit=result.gross_profit,
            max_cogs=max_cogs,
            cogs_reduction_pct=None,
            overrun_pts=0.0,
            min_price=min_price,
            max_cogs_ad_space=max_cogs_ad,
            min_price_ad_space=min_price_ad,
            gate=gate,
            notes=_notes(gate, margin),
            selling_price=price,
            quoted_cost=cogs,
        )

    reduction = None
    if max_cogs is not None and cogs > 0:
        reduction = (cogs - max_cogs) / cogs
    return Admission(
        name=candidate.name,
        verdict=VERDICT_FAIL,
        reason=(
            f"毛利 {margin:.2%}，差 {(TARGET_MARGIN - margin) * 100:.2f} 个点"
            + (f"；采购需降 {reduction:.1%}" if reduction is not None else "")
        ),
        missing=(),
        gross_margin=margin,
        gross_profit=result.gross_profit,
        max_cogs=max_cogs,
        cogs_reduction_pct=reduction,
        overrun_pts=(TARGET_MARGIN - margin) * 100,
        min_price=min_price,
        max_cogs_ad_space=max_cogs_ad,
        min_price_ad_space=min_price_ad,
        gate=gate,
        notes=_notes(gate, margin),
        selling_price=price,
        quoted_cost=cogs,
    )


def min_price_for_cogs(cogs: float, fba_fee: float, duty_rate: float) -> float:
    """闭式解：给定采购、FBA、税率，15% 线要求的最低售价。

    只在售价 < $166.67 时与模型逐分一致（退款管理费 $5 封顶见 `docs/04` §18.1）。
    闸一的 `GATE_AMOUNT_MIN_PRICE` 就是用它反解出来的，所以函数与常量互为校验。
    """
    a = 1 + duty_rate + DEFAULT_RETURNS * 0.30
    fixed = 1.016 * DEFAULT_FREIGHT + 0.24
    denominator = 1 - DEFAULT_REFERRAL - DEFAULT_RETURNS - 0.0024 - TARGET_MARGIN
    return (cogs * a + fixed + fba_fee) / denominator


# --- 批量导入：CSV → Candidate ------------------------------------------------
#
# 这一段的重点不是「读文件」，是**区分三种空**：
#
#   单元格留空      → None，交给 evaluate 判 need_info（「这个数我不知道」）
#   单元格写了但解析不了 → 报格式错并跳过该行（「这个数写错了」）
#   整列缺失        → 报列名错（「这张表结构不对」）
#
# 三者混同的后果：把一个写错的数当成「不知道」，批量筛完得到一片 need_info，
# 看起来像数据没准备好，实际是表填错了——而 nobody 会去重看第二遍。

#: 列名别名。左起第一个是规范名，其余是容忍写法（含中文，方便从文档直接粘贴）。
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "sku", "候选", "名称", "型号"),
    "selling_price": ("price", "selling_price", "售价"),
    "quoted_cost": ("cogs", "quoted_cost", "采购价", "采购"),
    "duty_rate": ("duty", "duty_rate", "关税", "税率"),
    "fba_fee": ("fba", "fba_fee", "fba费"),
    "fba_ratio": ("fba_ratio", "fba占比"),
}

REQUIRED_COLUMNS = ("selling_price", "quoted_cost")


@dataclass(frozen=True)
class RowProblem:
    """一行的问题。`line` 用 CSV 的行号（含表头，从 1 数），方便直接跳过去改。"""

    line: int
    message: str


def _normalize_header(name: str) -> str:
    return name.strip().lower().replace(" ", "").replace("_", "")


_CANONICAL: dict[str, str] = {}
for _canon, _aliases in COLUMN_ALIASES.items():
    for _alias in _aliases:
        _CANONICAL[_normalize_header(_alias)] = _canon


def _parse_cell_number(raw: str) -> float | None:
    """把单元格转成数。空 → None；`$1,234.5` / `23.7%` 这类写法容忍。

    `%` 一律按百分数转小数：关税列写 `23.7%` 与 `0.237` 是同一个意思，
    两种都收——但 `23.7`（不带 %）会被当成 2370%，所以超过 1 的税率要带 %。
    """
    text = raw.strip()
    if not text:
        return None
    text = text.replace(",", "").replace("$", "").replace("＄", "").replace("％", "%")
    if text.endswith("%"):
        return float(text[:-1]) / 100.0
    return float(text)


def parse_rows(rows: list[dict[str, str]]) -> tuple[list[Candidate], list[RowProblem]]:
    """把 CSV 行（dict 列表）转成 Candidate，并把问题按行报出来。

    列名大小写、下划线、空格都不敏感；`price`/`售价`/`selling_price` 等价。
    缺列时抛 `AdmissionInputInvalid`——那是整张表的问题，不该逐行报。
    """
    if not rows:
        return [], []

    header_map: dict[str, str] = {}
    # 遍历所有行的键而不是只看第一行：CSV 的 DictReader 保证列一致，
    # 但调用方手工拼的 dict 列表未必，漏掉别的行独有的列会静默变成 None。
    for key in {key for row in rows for key in row}:
        canon = _CANONICAL.get(_normalize_header(key))
        if canon is not None:
            header_map[key] = canon

    present = set(header_map.values())
    missing_cols = [col for col in REQUIRED_COLUMNS if col not in present]
    if missing_cols:
        raise AdmissionInputInvalid(
            "CSV 缺列：" + "、".join(missing_cols)
            + "。至少要有 price 与 cogs 两列（也接受 售价 / 采购价 等写法）"
        )

    candidates: list[Candidate] = []
    problems: list[RowProblem] = []
    for index, row in enumerate(rows, start=2):  # 表头是第 1 行
        mapped = {header_map[key]: value for key, value in row.items() if key in header_map}
        try:
            numbers = {
                col: _parse_cell_number(mapped.get(col, "") or "")
                for col in ("selling_price", "quoted_cost", "duty_rate", "fba_fee", "fba_ratio")
            }
        except ValueError as exc:
            problems.append(RowProblem(index, f"数值格式错（{exc}）"))
            continue
        candidates.append(
            Candidate(
                name=(mapped.get("name") or f"row{index}").strip(),
                selling_price=numbers["selling_price"],
                quoted_cost=numbers["quoted_cost"],
                duty_rate=numbers["duty_rate"],
                fba_fee=numbers["fba_fee"],
                fba_ratio=numbers["fba_ratio"],
            )
        )
    return candidates, problems
