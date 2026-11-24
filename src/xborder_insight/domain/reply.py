"""供应商回复 → 下一步动作：动作由回复决定，不由人记。

为什么单独成模块，而不是继续写在 `docs/07` 的动作卡里：

动作卡是一张静态表。五种回复对应五种动作，读的人要在表里找到自己那一行——
找错行、跳过行、或者遇到「箱规能做但报了高价」这种跨行情形，就回到凭感觉。
这里把**回复当输入、动作当输出**，判定与数字一起算出来，且判定只有这一份：
CLI 打印它，测试钉它，以后接口也复用它。

四个判断写死在这里（每个都能讲清「为什么不是另一种」）：

1. **箱规不明时一律先问，不报价。** 箱规决定 FBA 档位（咖啡机 $11.85 vs $14.40，
   差 $2.55/件 ≈ 16% 的价格空间）。不知道它就报价，只能报最保守那条，
   等于开局先让掉一档。
2. **说「改不了」但没说原因时，追问原因，而不是先降价。** 纸板受限常有替代
   （换纸板档、改内衬），模具受限基本没有。两者处置方向相反，猜错一边就白让 $2.55。
3. **降幅按「他这次报价」算，不按历史报价算。** 用 $18.50（原报价）到上限算降幅，
   会把「他刚让到 $15.50」这种情况也算成 −25%，从而误判成「不开口」。
4. **降幅超过议价带（15%）就换杠杆。** 带外压价拿不到（净化器 −15.1% 能谈、
   宠物饮水机 −23.8% 判不开口），此时只有两条路：提售价（券后价）或先放。

一个反直觉的结论由这套规则自己得出：咖啡机 B 档（箱规不改）在 $18.50 上，
`resolve` 给的是「先放」，而不是「开口 $13.00」——因为要降到 $13.72 得让步 25.8%，
而把售价提过 15% 线要 $74.99（+17.2%）。这与 `docs/04` §33 的结论一致，
但这里是**算出来**的，不是抄进去的。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .admission import (
    TARGET_MARGIN,
    Admission,
    AdmissionInputInvalid,
    Candidate,
    StressResult,
    evaluate,
    stress_test,
)

# ---------- 动作 ----------

ACTION_REASK_BOX = "reask_box"
ACTION_REASK_BLOCKER = "reask_blocker"
ACTION_OPEN = "open"
ACTION_ACCEPT = "accept"
ACTION_COUNTER = "counter"
ACTION_REPRICE = "reprice"
ACTION_DROP = "drop"

ACTION_LABELS = {
    ACTION_REASK_BOX: "先问箱规",
    ACTION_REASK_BLOCKER: "问清受限在哪",
    ACTION_OPEN: "发开口价",
    ACTION_ACCEPT: "直接签",
    ACTION_COUNTER: "还价",
    ACTION_REPRICE: "改提售价",
    ACTION_DROP: "先放",
}

#: 从对方报价到签字线的最大让步幅度。与 `docs/04` §33.3 的议价带同一口径：
#: 净化器 −15.1% 能谈、宠物饮水机 −23.8% 判「不开口」。取 15%。
NEGOTIABLE_DROP_PCT = 0.15

#: 提售价的容忍上限。超过它就不是「调价」而是「换品」：
#: 咖啡机 $63.99 → $65.99 是 +3.1%，可以；到 $75 就是 +17%，转化率撑不住。
PRICE_HIKE_MAX_PCT = 0.05

#: 开口 = 签字线往下的余量。没有余量就没有「再让一点」这个动作可用。
OPEN_GAP_PCT = 0.045

#: 签字线向下取整的步长。**不能报到分**：精确解（如 13.719）四舍五入到分
#: 就是 14.998%，报到分的数字没有余量，一次进位就签在线上。
SIGN_ROUND = 0.10

#: 开口价向下取整的步长。$15.02 → $15.00、$24.12 → $24.00——
#: 报精确解等于告诉对方「我算过、一分不让」（`docs/04` §27.2）。
OPEN_ROUND = 0.50

#: 箱规受限的两类原因。方向相反：纸板常有替代，模具基本没有。
BLOCKER_PAPERBOARD = "paperboard"
BLOCKER_TOOLING = "tooling"
BLOCKER_LABELS = {
    BLOCKER_PAPERBOARD: "纸板规格",
    BLOCKER_TOOLING: "模具/内衬",
}

#: 问箱规的话术（`docs/04` §35.2 定稿，2026-11-24 10:26）。
#:
#: 三处硬约束都在里面，缺一句他就会给出一个「看着照做、其实没用」的答复：
#: ① **同时卡长 ≤18、中 ≤14**——只压高度，他可能把中边放到 15，整单跳回小号大件；
#: ② **「8 英寸或以下（8.0 也算达标）」**——平台边界含 8.0，中文「以内」会被读成 <8；
#: ③ **「箱内总容积不用缩」**——不说这句，他默认要压缩 15.8%，很可能直接答做不到；
#:    实际口径是三边进 18/14/8 且体积 ≤2015 in³（现箱 1976），把 1.5 英寸挪到
#:    长或宽上就行，体积只降 3.6%。
#:
#: 全文**不带任何美元数**：带了就会收到「箱规改不了、就这个价」这种无法回收的答复
#: （上一版 §7.11 的 $15.00 就是这么作废的）。`tests/test_reply_actions.py` 钉住这一点。
BOX_QUESTION = (
    "关于咖啡机外箱：现在最短边 9.5 英寸（外箱 16×13×9.5）。麻烦评估一下，"
    "在长边不超过 18、中边不超过 14 的前提下，最短边能不能做到 8 英寸或以下"
    "（8.0 也算达标）？平台按三边分别卡档，超任何一边就整单按大一级计费，"
    "所以三个方向要一起看。箱内总容积不用缩——现在 1976 立方英寸，"
    "改完 2015 立方英寸以内都合规，把 1.5 英寸从高度挪到长或宽上就行。"
    "这两天给个结论：能做的，把那三边尺寸、机身净尺寸、单侧缓冲厚度发我；"
    "做不到的说下卡在哪儿（纸板/模具/内部结构）。确认物理上能做到，"
    "价格我内部核算一下再跟你对。"
)

#: 问原因的话术。会在「箱规改不了但没说原因」时使用，也可以附在别的动作后面。
BLOCKER_QUESTION = (
    "箱规那边是纸板规格受限，还是模具/内衬受限？"
    "如果是纸板，能不能换薄一档的纸板、或把内衬改薄？"
    "这个差别对我这边影响挺大，麻烦你确认下具体是哪种。"
)


@dataclass(frozen=True)
class SupplierReply:
    """供应商这一次回了什么。**只装事实，不装我方判断。**

    三个字段对应谈判里真正会变的三个变量：
    `box_fits` 决定 FBA 档位，`box_blocker` 决定下一步是追问还是改杠杆，
    `counter_price` 决定签、还价、还是换路子。
    """

    #: True = 最短边能做到 8 英寸内；False = 改不了；None = 没答 / 答得不明确
    box_fits: bool | None = None
    #: 只在 `box_fits is False` 时有意义：paperboard / tooling
    box_blocker: str | None = None
    #: 他的反报价（$/件）。None = 这一轮没谈价
    counter_price: float | None = None
    #: 他明确表示「就这个价，不再让」。决定「继续还价」还是「换杠杆」：
    #: 降幅不大但他咬死不放时，再谈一轮也拿不到，只能动售价。
    firm: bool = False
    #: 回复日期（写台账用，不参与判定）
    replied_at: str | None = None

    def __post_init__(self) -> None:
        if self.counter_price is not None and self.counter_price <= 0:
            raise AdmissionInputInvalid(
                f"对方报价必须为正，当前值: {self.counter_price!r}"
            )
        if self.firm and self.counter_price is None:
            raise AdmissionInputInvalid(
                "firm 只在有反报价时有意义：没有报价就谈不上「不让」"
            )
        if self.box_blocker is not None:
            if self.box_fits is not False:
                raise AdmissionInputInvalid(
                    "box_blocker 只在「箱规改不了」时才有意义；"
                    "箱规能做或未答时传它，说明两个字段写倒了"
                )
            if self.box_blocker not in BLOCKER_LABELS:
                raise AdmissionInputInvalid(
                    f"未知的受限原因 {self.box_blocker!r}，"
                    f"只接受 {sorted(BLOCKER_LABELS)}"
                )

    @property
    def asked_box(self) -> bool:
        return self.box_fits is not None

    @property
    def blocker_label(self) -> str | None:
        return BLOCKER_LABELS.get(self.box_blocker) if self.box_blocker else None


@dataclass(frozen=True)
class Resolution:
    """一次回复对应的动作 + 支撑它的数字。数字与动作一起给，不让调用方自己比。"""

    action: str
    reasons: tuple[str, ...]
    branch: str | None = None
    #: 可直接发给供应商的话术；内部动作（REPRICE / DROP）为 None
    next_message: str | None = None
    fba_fee: float | None = None
    #: 15% 线的精确采购上限（未取整）
    sign_ceiling: float | None = None
    #: 报到 $0.10 的签字线（已校验毛利 ≥ 15%）
    sign_price: float | None = None
    #: 签字线下的毛利（打印用；判定已在 `_sign_ladder` 里做过）
    sign_margin: float | None = None
    #: 开口价（≤ 签字线）
    open_price: float | None = None
    #: 20% 广告空间线的上限；它比 15% 线更紧，决定开口能有多高
    ad_space_ceiling: float | None = None
    #: 他报价下的毛利
    reply_margin: float | None = None
    #: 从他报价降到签字线要降的比例（正数 = 要降）
    need_drop_pct: float | None = None
    #: REPRICE 时要提到的售价（含 .99 取整）
    price_hike_to: float | None = None
    stress: StressResult | None = None
    #: 还欠对方一句追问（箱规改不了但没说原因）
    pending_question: str | None = None

    @property
    def label(self) -> str:
        return ACTION_LABELS[self.action]

    @property
    def needs_external_input(self) -> bool:
        """这个动作是不是在等对方，而不是等我们自己算。"""
        return self.action in (ACTION_REASK_BOX, ACTION_REASK_BLOCKER)


def _floor_to(value: float, step: float) -> float:
    """向下取整到 step 的整数倍。加 1e-9 吃掉浮点尾差
    （15.700000000001 不该被降到 15.60）。"""
    return round(math.floor(value / step + 1e-9) * step, 2)


def _ceil_to_99(value: float) -> float:
    """向上取到下一个 x.99。售价的心理价位，且保证不低于线。"""
    return round(math.ceil(value) - 0.01, 2)


def _margin_at(candidate: Candidate, cogs: float) -> float | None:
    return evaluate(replace(candidate, quoted_cost=cogs)).gross_margin


def _sign_ladder(candidate: Candidate, admission: Admission) -> tuple[float, float, float]:
    """返回 (精确上限, 签字线, 签字线下的毛利)。

    签字线取「向下 $0.10」而不是「四舍五入到分」：精确解 13.719 进到 13.72
    就是 14.998%（差 0.002 个点掉线），而 13.72 看起来正是「算出来的那个数」。
    取整后再复核毛利，防止某些价位上佣金/退款管理费的拐点把边界推走。
    """
    ceiling = admission.max_cogs
    if ceiling is None:
        raise AdmissionInputInvalid(
            "15% 线解不出采购上限：售价太低，采购降到 0 也过不了线。"
            "这种候选不该谈价，应改提售价或直接放掉"
        )
    price = _floor_to(ceiling, SIGN_ROUND)
    for _ in range(60):
        if price <= 0:
            break
        margin = _margin_at(candidate, price)
        if margin is not None and margin >= TARGET_MARGIN:
            return ceiling, price, margin
        price = round(price - SIGN_ROUND, 2)
    raise AdmissionInputInvalid(
        f"签字线取整后仍过不了 {TARGET_MARGIN:.0%} 线（上限 ${ceiling:.4f}），"
        "售价与 FBA 的组合不成立"
    )


def _open_price(
    candidate: Candidate,
    admission: Admission,
    sign_ceiling: float,
    sign_price: float,
    fair_cogs: float | None,
) -> tuple[float, str | None]:
    """开口价 = min(签字线 − 4.5%, 不超过 20% 线的那个上限)，返回 (价, 提示)。

    为什么还要看 20% 线：15% 线回答「这单赚不赚」，20% 线回答「赚的这点够不够
    把货卖出去」。开口报到 15% 线附近，谈成了也只能靠自然单（`docs/04` §22.7）。

    为什么有公允锚时取 max(20% 线, 公允 × 0.85)：20% 线单独用会给出
    「没人理的开价」（净化器 $20.89 = 公允 −25.4%），用公允价托底；
    两条一起用，再被 15% 线压住上限。
    """
    cap = sign_ceiling * (1 - OPEN_GAP_PCT)
    ad = admission.max_cogs_ad_space
    note = None
    if ad is not None and fair_cogs is not None:
        cap = min(cap, max(ad, fair_cogs * 0.85))
    elif ad is not None:
        # 没有公允锚时按 20% 线走：保守方向，但开口会明显低于 15% 线，
        # 必须说出来，否则读的人会以为这就是该报的价。
        if ad < cap:
            note = (
                f"没给公允锚（--fair），开口被 20% 线压到 ${ad:,.2f} 附近——"
                "这个价可能低到没人理，补上公允价重算才是真开口"
            )
        cap = min(cap, ad)
    price = min(_floor_to(cap, OPEN_ROUND), sign_price)
    return price, note


def _hike_price(candidate: Candidate, at_cogs: float) -> tuple[float, float] | None:
    """在某个采购价下，15% 线要求售价提到多少。返回 (目标售价, 涨幅)；无解返回 None。"""
    probe = evaluate(replace(candidate, quoted_cost=at_cogs))
    if probe.min_price is None or candidate.selling_price in (None, 0):
        return None
    target = _ceil_to_99(probe.min_price)
    assert candidate.selling_price is not None
    return target, target / candidate.selling_price - 1



def _lever(
    candidate: Candidate,
    counter: float,
    sign_ceiling: float,
    *,
    source: str,
    firm: bool,
    question: str | None,
) -> tuple[str, tuple[str, ...], float | None]:
    """压价拿不到时换杠杆：提售价 or 先放。返回 (动作, 理由, 提价目标)。

    两个动作都不再找他谈价——「先放」还发消息，等于告诉他我们放弃了，
    这个价以后谈不回来。所以话术由调用方决定（通常只剩箱规追问）。

    `firm` 是「他咬住这个价、不再让」：这是提价真正会发生的场景。
    没有它，提价几乎不可能触发——数字上，降幅超过 15% 时把售价拉回 15% 线
    通常要涨 8% 以上，而涨价容忍上限只有 5%，两个条件几乎不同时成立。
    所以「提价」对应的是「降幅其实不大，但对方一步不让」，不是「差得远」。
    """
    hike = _hike_price(candidate, counter)
    need_drop = (counter - sign_ceiling) / counter
    can_reprice = hike is not None and hike[1] <= PRICE_HIKE_MAX_PCT
    if can_reprice:
        assert hike is not None
        target, pct = hike
        why = "他咬住这个价、不再让" if firm else "这个降幅超出议价带"
        return (
            ACTION_REPRICE,
            (
                f"{source} ${counter:,.2f}，{why}（要降到签字线得让步 {need_drop:.1%}，"
                f"议价带 {NEGOTIABLE_DROP_PCT:.0%}）",
                f"换杠杆：券后价 ${candidate.selling_price:,.2f} → ${target:,.2f}"
                f"（+{pct:.1%}）就能在 ${counter:,.2f} 上过 15% 线，不动供应商",
            ),
            target,
        )
    tail = (
        f"要提到 ${hike[0]:,.2f}（+{hike[1]:.1%}）"
        if hike
        else "连 0 采购都过不了 15% 线"
    )
    reasons = [
        f"{source} ${counter:,.2f}，要降到签字线得让步 {need_drop:.1%}"
        f"（议价带 {NEGOTIABLE_DROP_PCT:.0%}）",
        f"提售价也救不回来：{tail}，超出 +{PRICE_HIKE_MAX_PCT:.0%} 的容忍上限 → 这台先放",
    ]
    if question:
        # 价格先放，但「卡在纸板还是模具」是免费信息：问对了箱子可能进 A 档，
        # 那 $2.55/件 比这一轮谈价的值大得多。
        reasons.append("价格先放，但箱规原因仍值得问一句：问对了箱子可能进 A 档")
    return ACTION_DROP, tuple(reasons), None


def resolve(
    candidate: Candidate,
    reply: SupplierReply,
    *,
    fair_cogs: float | None = None,
    stress_returns: float | None = None,
) -> Resolution:
    """把「他回了什么」翻成「我下一步做什么」。

    `candidate.quoted_cost` 是**他当前的报价**（这一轮的起点），
    `candidate.fba_fee` 必须是**箱规答复对应的那一档**——箱规未答时本函数
    不碰 FBA，也不需要它。
    """
    # ① 箱规没答：先问，不报价
    if reply.box_fits is None:
        reasons = ["箱规没答 → FBA 档位定不下来，毛利算不出来，报价也无从定"]
        if reply.counter_price is not None:
            reasons.append(
                f"他报了 ${reply.counter_price:,.2f}，但箱规不明时这个价判不了——"
                "先拿到箱规再回价，别先接价"
            )
        else:
            reasons.append("先只问箱规，别顺带报价：报了就要认")
        return Resolution(
            action=ACTION_REASK_BOX,
            reasons=tuple(reasons),
            next_message=BOX_QUESTION,
        )

    # ② 说改不了但没说原因：要追问，但不阻断价格判断（他常同时报个价）
    ask_blocker = reply.box_fits is False and reply.box_blocker is None
    question = BLOCKER_QUESTION if ask_blocker else None

    if ask_blocker and reply.counter_price is None:
        return Resolution(
            action=ACTION_REASK_BLOCKER,
            branch="B",
            reasons=(
                "他说箱规改不了，但没说卡在哪——纸板受限常有替代（换纸板档、改内衬、"
                "减配件），模具受限基本没有。两种处置方向相反，先问清再动价格",
                "此时不要报任何价：B 档的上限比 A 档低一档，报了收不回来",
            ),
            next_message=question,
        )

    if candidate.fba_fee is None:
        raise AdmissionInputInvalid(
            "箱规已定，需要**对应档位**的 FBA 费用（--fba）："
            "A 档与 B 档差 $2.55/件，用错档整条价格线都会偏"
        )

    branch = "A" if reply.box_fits else "B"
    admission = evaluate(candidate)
    if admission.gross_margin is None:
        raise AdmissionInputInvalid(
            "还判不了：" + "、".join(admission.missing) + "；补齐后再判动作"
        )
    if admission.upper_bound:
        # 上界反解出的签字线偏高——这正是 H24 关掉的那条路，不能从 CLI 绕回来。
        # 缺哪几项要说出来，否则调用方只能猜（通常就是头程没给）。
        missing = "、".join(admission.missing) or "头程 / 退货率"
        raise AdmissionInputInvalid(
            f"上界口径下不给价格线：{missing}未指定时反解出的签字线偏高，"
            "按它报价必亏；补齐后再判动作"
        )
    sign_ceiling, sign_price, sign_margin = _sign_ladder(candidate, admission)
    open_price, open_note = _open_price(
        candidate, admission, sign_ceiling, sign_price, fair_cogs
    )

    common = dict(
        branch=branch,
        fba_fee=candidate.fba_fee,
        sign_ceiling=sign_ceiling,
        sign_price=sign_price,
        sign_margin=sign_margin,
        open_price=open_price,
        ad_space_ceiling=admission.max_cogs_ad_space,
        pending_question=question,
    )

    stress = None
    if stress_returns is not None:
        stress = stress_test(replace(candidate, quoted_cost=sign_price), stress_returns)
        if not stress.worth_negotiating:
            return Resolution(
                action=ACTION_DROP,
                reasons=(
                    f"压力档 {stress_returns:.0%} 下毛利 {stress.stress_margin:.2%}，"
                    "两档不同时为正——退货风险已经吃掉利润，别在价格上花时间",
                    "换杠杆：箱规（若能进 A 档）或售价，不是继续压采购价",
                ),
                stress=stress,
                **common,
            )

    # ③ 他没谈价：先看他现在的报价能不能进议价带
    if reply.counter_price is None:
        current = float(candidate.quoted_cost or 0)
        need_now = (current - sign_ceiling) / current if current else 1.0
        if need_now > NEGOTIABLE_DROP_PCT:
            action, reasons, hike_to = _lever(
                candidate, current, sign_ceiling,
                source="他现在的报价", firm=False, question=question,
            )
            return Resolution(
                action=action,
                reasons=reasons + (
                    f"所以不开口：报 ${open_price:,.2f} 等于要他一次降 {need_now:.1%}，"
                    "这个幅度不会有人接（`docs/04` §33.3 B 档的情形）",
                ),
                # 让步幅度与毛利照样带出去：结论是「先放」，但读者要能看见
                # 「差多远」——否则无法判断过两周价格松动时要不要重算。
                price_hike_to=hike_to,
                reply_margin=_margin_at(candidate, current),
                need_drop_pct=need_now,
                stress=stress,
                **common,
            )
        reasons = [
            f"{branch} 档（FBA ${candidate.fba_fee:,.2f}）15% 线的采购上限 "
            f"${sign_ceiling:,.3f}，报出去取 ${sign_price:,.2f}"
            f"（毛利 {sign_margin:.2%}）",
            f"开口 ${open_price:,.2f}：留 ${sign_price - open_price:,.2f} 让步带，"
            "否则对方一还价就直接签在线上",
        ]
        if admission.max_cogs_ad_space is not None:
            reasons.append(
                f"20% 广告空间线上限 ${admission.max_cogs_ad_space:,.2f}——"
                "低于它签下来只能靠自然单"
            )
        if open_note:
            reasons.append(open_note)
        return Resolution(
            action=ACTION_OPEN,
            reasons=tuple(reasons),
            stress=stress,
            next_message=(
                f"${open_price:,.2f}/台（FOB含包装含税），能做到的话目标签约定在"
                f"${sign_price:,.2f}/台。你确认下，我们推进下一步。"
            ),
            **common,
        )

    # ④ 他报了价
    counter = reply.counter_price
    reply_margin = _margin_at(candidate, counter)
    assert reply_margin is not None
    need_drop = (counter - sign_ceiling) / counter

    if counter <= sign_price:
        reasons = [
            f"他报 ${counter:,.2f}，在签字线 ${sign_price:,.2f} 以内"
            f"（毛利 {reply_margin:.2%}）→ 直接签，不要再压",
            "这一轮不再还价：已经在线上方，再压会把关系用在一个换不来钱的数上",
        ]
        if counter <= open_price:
            reasons.append(
                f"比我们准备开口的 ${open_price:,.2f} 还低 "
                f"${open_price - counter:,.2f}：不用再谈"
            )
        return Resolution(
            action=ACTION_ACCEPT,
            reasons=tuple(reasons),
            reply_margin=reply_margin,
            need_drop_pct=need_drop,
            stress=stress,
            next_message=(
                f"${counter:,.2f} 可以，就按这个价。我把 PI 发你确认，"
                "含单台彩盒、FOB 深圳、首批量与量产同价。"
            ),
            **common,
        )

    if need_drop <= NEGOTIABLE_DROP_PCT and not reply.firm:
        reasons = [
            f"他报 ${counter:,.2f}（毛利 {reply_margin:.2%}）；"
            f"15% 线在 ${sign_ceiling:,.3f}，报出去取 ${sign_price:,.2f}"
            f"（让步 {need_drop:.1%}，在 {NEGOTIABLE_DROP_PCT:.0%} 议价带内）→ 还价",
            f"还价报到 ${sign_price:,.2f}（15% 线），别再往下让——"
            "让到线下签下来就是亏",
        ]
        hint = _hike_price(candidate, counter)
        if hint is not None and hint[1] <= PRICE_HIKE_MAX_PCT:
            reasons.append(
                f"他若不接 ${sign_price:,.2f}：券后价提到 ${hint[0]:,.2f}"
                f"（+{hint[1]:.1%}）也能在这个价上过线，这是第二条路"
            )
        return Resolution(
            action=ACTION_COUNTER,
            reasons=tuple(reasons),
            reply_margin=reply_margin,
            need_drop_pct=need_drop,
            price_hike_to=hint[0] if hint is not None and hint[1] <= PRICE_HIKE_MAX_PCT else None,
            stress=stress,
            next_message=(
                f"${counter:,.2f} 这个价我这边算不过来。${sign_price:,.2f} 的话"
                "今天就能定，首批量与量产同价，量我可以锁 12 个月。"
            ),
            **common,
        )

    action, reasons, hike_to = _lever(
        candidate, counter, sign_ceiling,
        source="他报", firm=reply.firm, question=question,
    )
    if action == ACTION_REPRICE:
        reasons = reasons + (
            f"他报价下的毛利 {reply_margin:.2%} 仍在，只是不够 15%——"
            "所以先动售价，不动供应商",
        )
    return Resolution(
        action=action,
        reasons=reasons,
        price_hike_to=hike_to,
        reply_margin=reply_margin,
        need_drop_pct=need_drop,
        stress=stress,
        **common,
    )
