"""决策合成：把机会分、毛利、合规合成 GO / NO-GO / 需补充信息。

三条设计原则：

1. **短路判定，但不短路记录**
   一旦某项是否决性的，结论就定了；但所有因素仍然逐条记进 `reasons`。
   只汇报「因为毛利为负所以 NO-GO」而隐去「合规也没核实」，
   会让对方以为把毛利修好就能做——这是解释性欺骗。

2. **阈值是业务假设，不是官方数据**
   `MIN_GROSS_MARGIN` 这类数字没有任何官方来源，属于经营判断。
   所以它们在代码里显式命名并标注「假设」，而不是散落在比较式里的魔法数。
   报告中若引用，必须说明是假设值。

3. **「不确定」与「不行」必须分开**
   NO_GO 是「按已知信息判断不该做」；NEED_MORE_INFO 是「信息不足，判断不了」。
   把后者说成前者会误杀机会，说成 GO 会误导投入。
"""

from __future__ import annotations

from dataclasses import dataclass

from .compliance import STATUS_BLOCKED, STATUS_UNKNOWN, ComplianceCheck
from .profit import ProfitResult

GO = "GO"
NO_GO = "NO_GO"
NEED_MORE_INFO = "NEED_MORE_INFO"

# ---- 业务假设（无官方来源，属经营判断；报告中引用时须注明） ----
MIN_GROSS_MARGIN = 0.15      # 毛利率下限：低于此不足以覆盖固定开销与经营风险
MIN_OPPORTUNITY_SCORE = 0.55  # 机会分下限：对应 score.py 的三项加权分布中位以上
MIN_BREAKEVEN_ACOS = 0.20    # 广告空间下限：盈亏平衡 ACOS 低于 20% 时，
                              # 在真实竞价环境下基本投不出量，有毛利但卖不动

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Reason:
    """一条可追溯的判定依据。ref 指向参数 key 或规则 key，便于反查。"""

    factor: str
    observed: str
    result: str
    detail: str
    ref: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "factor": self.factor,
            "observed": self.observed,
            "result": self.result,
            "detail": self.detail,
            "ref": self.ref,
        }


@dataclass(frozen=True)
class Decision:
    verdict: str
    headline: str
    profit: ProfitResult
    compliance: ComplianceCheck
    opportunity_score: float
    reasons: tuple[Reason, ...]
    data_provenance: str

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "headline": self.headline,
            "profit": self.profit.as_dict(),
            "compliance": self.compliance.as_dict(),
            "opportunity_score": round(self.opportunity_score, 4),
            "reasons": [reason.as_dict() for reason in self.reasons],
            "data_provenance": self.data_provenance,
            "thresholds": {
                "min_gross_margin": MIN_GROSS_MARGIN,
                "min_opportunity_score": MIN_OPPORTUNITY_SCORE,
                "min_breakeven_acos": MIN_BREAKEVEN_ACOS,
                "note": "业务假设，非官方数据",
            },
        }


def decide(
    profit: ProfitResult,
    compliance: ComplianceCheck,
    opportunity_score: float,
    *,
    data_provenance: str,
) -> Decision:
    reasons: list[Reason] = []

    # ---------- 合规 ----------
    if compliance.status == STATUS_BLOCKED:
        reasons.append(
            Reason(
                factor="合规准入",
                observed=f"{len(compliance.blockers)} 项阻断级要求",
                result=FAIL,
                detail=compliance.reason + "；" + "；".join(
                    f"{hit.rule_key}（{'已核实' if hit.verified else '未核实'}）"
                    for hit in compliance.blockers
                ),
                ref=compliance.blockers[0].rule_key if compliance.blockers else None,
            )
        )
    elif compliance.status == STATUS_UNKNOWN:
        reasons.append(
            Reason(
                factor="合规准入",
                observed="无法判定",
                result=UNKNOWN,
                detail=compliance.reason,
                ref=None,
            )
        )
    else:
        reasons.append(
            Reason(
                factor="合规准入",
                observed=f"{compliance.verified_count} 条规则已核实、无阻断项",
                result=PASS,
                detail=compliance.reason,
                ref=None,
            )
        )

    # ---------- 毛利 ----------
    # FBA 未知时毛利率是一个区间。三种情况必须分开，因为动作不同：
    #   乐观端都不达标 → 这件商品确实不能做（不需要去量箱子）；
    #   保守端都达标   → 可以做，真实值只会更好（同样不需要量箱子）；
    #   跨过阈值       → 做不做取决于包装尺寸，此时才必须量（UNKNOWN）。
    # 把第三种说成 FAIL 会误杀，说成 PASS 会误导投入。
    if not profit.fba_known:
        margin_observed = (
            f"${profit.gross_profit:.2f}–${profit.gross_profit_high:.2f}"
            f"（{profit.gross_margin_low:.1%}–{profit.gross_margin_high:.1%}，"
            f"FBA ${profit.fba_low:.2f}–${profit.fba_high:.2f}）"
        )
        if profit.gross_margin_high < MIN_GROSS_MARGIN:
            margin_result = FAIL
            margin_detail = (
                f"即使按最便宜档（FBA ${profit.fba_low:.2f}），毛利率也只有 "
                f"{profit.gross_margin_high:.1%}，仍低于假设下限 {MIN_GROSS_MARGIN:.0%}；"
                "换档位救不回来，问题在成本结构本身"
            )
        elif profit.gross_margin_low >= MIN_GROSS_MARGIN:
            margin_result = PASS
            margin_detail = (
                f"即使按最贵档（FBA ${profit.fba_high:.2f}），毛利率仍有 "
                f"{profit.gross_margin_low:.1%}，高于假设下限 {MIN_GROSS_MARGIN:.0%}；"
                "尺寸重量只会让结论更好"
            )
        else:
            margin_result = UNKNOWN
            margin_detail = (
                f"毛利率区间 {profit.gross_margin_low:.1%}–{profit.gross_margin_high:.1%} "
                f"跨过假设下限 {MIN_GROSS_MARGIN:.0%}：做不做取决于包装后的尺寸与实重。"
                "量过包装（或给出尺寸段/重量上界）即可定论"
            )
    elif profit.gross_profit <= 0:
        margin_result = FAIL
        margin_observed = f"${profit.gross_profit:.2f}（{profit.gross_margin:.1%}）"
        margin_detail = (
            f"单件毛利 ${profit.gross_profit:.2f}，落地成本与平台费用已吃掉全部售价；"
            "加大广告只会加速亏损"
        )
    elif profit.gross_margin < MIN_GROSS_MARGIN:
        margin_result = FAIL
        margin_observed = f"${profit.gross_profit:.2f}（{profit.gross_margin:.1%}）"
        margin_detail = (
            f"毛利率 {profit.gross_margin:.1%} 低于假设下限 {MIN_GROSS_MARGIN:.0%}，"
            "余量不足以覆盖固定开销与退货波动"
        )
    else:
        margin_result = PASS
        margin_observed = f"${profit.gross_profit:.2f}（{profit.gross_margin:.1%}）"
        margin_detail = f"毛利率 {profit.gross_margin:.1%}，高于假设下限 {MIN_GROSS_MARGIN:.0%}"

    # 缺省成本项时，上面算出的是**上界**：每一项都只能增加成本，
    # 所以真实毛利率一定不高于这个数。两种情况必须分开——
    # 上界都不达标 → FAIL 可以直接采信（真实只会更差，不需要补齐就能否掉）；
    # 上界达标     → 不能称 PASS，PASS 的语义是「已经确认可以」，而上界做不到这一点。
    if profit.missing_inputs and margin_result == PASS:
        margin_result = UNKNOWN
        margin_detail = (
            f"毛利率 {profit.gross_margin:.1%} 是把未指定的成本项"
            f"（{'、'.join(profit.missing_labels)}）按 0 计入得到的**上界**，不是结论："
            "这几项只要不为 0，毛利率就一定低于此值。补齐后才能判断是否达到 "
            f"{MIN_GROSS_MARGIN:.0%}"
        )

    reasons.append(
        Reason(
            factor="单件毛利",
            observed=margin_observed,
            result=margin_result,
            detail=margin_detail,
            ref="profit.gross_margin",
        )
    )

    # ---------- 广告空间 ----------
    # 盈亏平衡 ACOS 在数值上就等于毛利率，所以区间判断与毛利同构：
    # 最便宜档都投不出量 → 确定不行；最贵档都够 → 确定可行；跨阈值 → 待定。
    if not profit.fba_known:
        if profit.gross_profit_high <= 0:
            acos_result = FAIL
            acos_observed = "不存在（最便宜档毛利也非正）"
            acos_detail = (
                "即使按最便宜档计，毛利仍非正，任何广告投入都只放大亏损"
            )
        elif profit.gross_profit_low <= 0:
            acos_result = UNKNOWN
            acos_observed = (
                f"不存在–{profit.gross_margin_high:.1%}"
                f"（毛利区间 ${profit.gross_profit_low:.2f}–${profit.gross_profit_high:.2f}）"
            )
            acos_detail = (
                "按最贵档无广告空间、按最便宜档有——是否可投取决于包装尺寸与实重，"
                "量过包装才能定论"
            )
        elif profit.gross_margin_low < MIN_BREAKEVEN_ACOS <= profit.gross_margin_high:
            acos_result = UNKNOWN
            acos_observed = (
                f"{profit.gross_margin_low:.1%}–{profit.gross_margin_high:.1%}"
                f"（FBA ${profit.fba_low:.2f}–${profit.fba_high:.2f}）"
            )
            acos_detail = (
                f"区间跨过假设下限 {MIN_BREAKEVEN_ACOS:.0%}："
                "量过包装后若落在下半段，则广告投不出量"
            )
        elif profit.gross_margin_high < MIN_BREAKEVEN_ACOS:
            acos_result = FAIL
            acos_observed = f"最高 {profit.gross_margin_high:.1%}"
            acos_detail = (
                f"即使按最便宜档，盈亏平衡 ACOS 也只有 {profit.gross_margin_high:.1%}，"
                f"低于假设下限 {MIN_BREAKEVEN_ACOS:.0%}；换档位救不回来"
            )
        else:
            acos_result = PASS
            acos_observed = f"最低 {profit.gross_margin_low:.1%}"
            acos_detail = (
                f"即使按最贵档，仍有 {profit.gross_margin_low:.1%}，"
                f"高于假设下限 {MIN_BREAKEVEN_ACOS:.0%}"
            )
    elif profit.breakeven_acos is None:
        acos_result = FAIL
        acos_observed = "不存在"
        acos_detail = profit.breakeven_reason or "毛利非正，无广告空间"
    elif profit.breakeven_acos < MIN_BREAKEVEN_ACOS:
        acos_result = FAIL
        acos_observed = f"盈亏平衡 ACOS {profit.breakeven_acos:.1%}"
        acos_detail = (
            f"低于假设下限 {MIN_BREAKEVEN_ACOS:.0%}：新品期真实 ACOS 普遍高于此，"
            "意味着有毛利但很难投出量"
        )
    else:
        acos_result = PASS
        acos_observed = f"盈亏平衡 ACOS {profit.breakeven_acos:.1%}"
        acos_detail = f"高于假设下限 {MIN_BREAKEVEN_ACOS:.0%}，具备投放空间"

    # 同毛利：ACOS 在上界口径下达标 ≠ 确认可投。
    # 「有毛利但投不出量」这件事在下界出现，而上界恰恰是最乐观的那一端。
    if profit.missing_inputs and acos_result == PASS:
        acos_result = UNKNOWN
        acos_detail = (
            f"盈亏平衡 ACOS {profit.breakeven_acos:.1%} 是按缺省成本项为 0 算出的上界；"
            "成本补齐后 ACOS 只会更低，届时可能掉到 "
            f"{MIN_BREAKEVEN_ACOS:.0%} 以下"
        )

    reasons.append(
        Reason(
            factor="广告空间",
            observed=acos_observed,
            result=acos_result,
            detail=acos_detail,
            ref="profit.breakeven_acos",
        )
    )

    # ---------- 机会分 ----------
    if opportunity_score < MIN_OPPORTUNITY_SCORE:
        opportunity_result = FAIL
        opportunity_detail = (
            f"机会分 {opportunity_score:.2f} 低于假设下限 {MIN_OPPORTUNITY_SCORE:.2f}："
            "痛点集中度或负向占比不足，需求强度存疑"
        )
    else:
        opportunity_result = PASS
        opportunity_detail = (
            f"机会分 {opportunity_score:.2f} 达到假设下限 {MIN_OPPORTUNITY_SCORE:.2f}"
        )
    reasons.append(
        Reason(
            factor="市场需求",
            observed=f"机会分 {opportunity_score:.2f}",
            result=opportunity_result,
            detail=opportunity_detail,
            ref="score.opportunity",
        )
    )

    # ---------- 合成 ----------
    verdict, headline = _synthesize(reasons, profit, compliance, opportunity_score)

    return Decision(
        verdict=verdict,
        headline=headline,
        profit=profit,
        compliance=compliance,
        opportunity_score=opportunity_score,
        reasons=tuple(reasons),
        data_provenance=data_provenance,
    )


def _synthesize(
    reasons: list[Reason],
    profit: ProfitResult,
    compliance: ComplianceCheck,
    opportunity_score: float,
) -> tuple[str, str]:
    """按否决力排序。合规 > 毛利 > 广告空间 > 需求。

    顺序不是随意的：合规不过就根本不能上架，此时讨论毛利毫无意义；
    毛利为负则广告空间必然为负。按「最上游的否决项」定结论，才符合实际决策过程。

    这里**不重算**判断，只读 `reasons` 里已经算好的结果。理由是避免两份逻辑漂移：
    一旦「毛利算 FAIL 的条件」在 decide 与 _synthesize 里各写一遍，改一处忘一处时，
    结论与依据会互相矛盾——而报表只展示结论，没人会发现它和依据对不上。
    """
    if compliance.status == STATUS_BLOCKED:
        has_unverified = compliance.has_unverified_blocker
        return (
            NO_GO,
            "存在阻断级合规要求，当前不能上架"
            + ("（其中含未核实项，核实后可能解除）" if has_unverified else ""),
        )

    margin = next(r for r in reasons if r.factor == "单件毛利")
    acos = next(r for r in reasons if r.factor == "广告空间")
    opportunity = next(r for r in reasons if r.factor == "市场需求")

    if margin.result == FAIL:
        if not profit.fba_known and profit.gross_profit_high <= 0:
            return (
                NO_GO,
                f"即使按最便宜档（FBA ${profit.fba_low:.2f}），单件毛利仍只有 "
                f"${profit.gross_profit_high:.2f}，结构性亏损",
            )
        if profit.gross_profit <= 0:
            return NO_GO, f"单件毛利 ${profit.gross_profit:.2f}，结构性亏损"
        return NO_GO, f"毛利率 {profit.gross_margin:.1%} 低于可接受下限"

    if acos.result == FAIL:
        return NO_GO, "广告空间不足，有毛利但难以起量"

    if opportunity.result == FAIL:
        return NO_GO, f"机会分 {opportunity_score:.2f} 不足以支撑投入"

    # 到这里没有确定性否决，剩下的都是「信息不足」。
    # 需要区分缺的是哪一类——因为补它的动作完全不同：
    # 缺成本项要回去问供应商/报关行，缺尺寸重量要去量箱子。
    if margin.result == UNKNOWN or acos.result == UNKNOWN:
        if profit.missing_inputs:
            return (
                NEED_MORE_INFO,
                "成本项未指定（"
                + "、".join(profit.missing_labels)
                + "）：按 0 计入得到的是最乐观上界"
                f"（毛利与盈亏平衡 ACOS 都是 {profit.gross_margin:.1%}）；"
                "真实值只会更低，补齐这几项才能定论",
            )
        return (
            NEED_MORE_INFO,
            "能不能做取决于包装后的尺寸与实重：毛利率区间 "
            f"{profit.gross_margin_low:.1%}–{profit.gross_margin_high:.1%}"
            f"（FBA ${profit.fba_low:.2f}–${profit.fba_high:.2f}）跨过判定阈值，"
            "量过包装或给出尺寸段/重量上界即可定论",
        )

    if compliance.status == STATUS_UNKNOWN:
        return (
            NEED_MORE_INFO,
            "利润与需求均达标，但合规规则尚未核实，需补齐来源后才能定论",
        )

    return (
        GO,
        f"毛利 {profit.gross_margin:.1%}、盈亏平衡 ACOS {profit.breakeven_acos:.1%}、"
        f"机会分 {opportunity_score:.2f}，合规无阻断项",
    )
