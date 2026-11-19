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
    if profit.gross_profit <= 0:
        margin_result = FAIL
        margin_detail = (
            f"单件毛利 ${profit.gross_profit:.2f}，落地成本与平台费用已吃掉全部售价；"
            "加大广告只会加速亏损"
        )
    elif profit.gross_margin < MIN_GROSS_MARGIN:
        margin_result = FAIL
        margin_detail = (
            f"毛利率 {profit.gross_margin:.1%} 低于假设下限 {MIN_GROSS_MARGIN:.0%}，"
            "余量不足以覆盖固定开销与退货波动"
        )
    else:
        margin_result = PASS
        margin_detail = f"毛利率 {profit.gross_margin:.1%}，高于假设下限 {MIN_GROSS_MARGIN:.0%}"

    reasons.append(
        Reason(
            factor="单件毛利",
            observed=f"${profit.gross_profit:.2f}（{profit.gross_margin:.1%}）",
            result=margin_result,
            detail=margin_detail,
            ref="profit.gross_margin",
        )
    )

    # ---------- 广告空间 ----------
    if profit.breakeven_acos is None:
        reasons.append(
            Reason(
                factor="广告空间",
                observed="不存在",
                result=FAIL,
                detail=profit.breakeven_reason or "毛利非正，无广告空间",
                ref="profit.breakeven_acos",
            )
        )
    elif profit.breakeven_acos < MIN_BREAKEVEN_ACOS:
        reasons.append(
            Reason(
                factor="广告空间",
                observed=f"盈亏平衡 ACOS {profit.breakeven_acos:.1%}",
                result=FAIL,
                detail=(
                    f"低于假设下限 {MIN_BREAKEVEN_ACOS:.0%}：新品期真实 ACOS 普遍高于此，"
                    "意味着有毛利但很难投出量"
                ),
                ref="profit.breakeven_acos",
            )
        )
    else:
        reasons.append(
            Reason(
                factor="广告空间",
                observed=f"盈亏平衡 ACOS {profit.breakeven_acos:.1%}",
                result=PASS,
                detail=f"高于假设下限 {MIN_BREAKEVEN_ACOS:.0%}，具备投放空间",
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
    """
    if compliance.status == STATUS_BLOCKED:
        has_unverified = compliance.has_unverified_blocker
        return (
            NO_GO,
            "存在阻断级合规要求，当前不能上架"
            + ("（其中含未核实项，核实后可能解除）" if has_unverified else ""),
        )

    if profit.gross_profit <= 0:
        return NO_GO, f"单件毛利 ${profit.gross_profit:.2f}，结构性亏损"

    if profit.gross_margin < MIN_GROSS_MARGIN:
        return NO_GO, f"毛利率 {profit.gross_margin:.1%} 低于可接受下限"

    if profit.breakeven_acos is None or profit.breakeven_acos < MIN_BREAKEVEN_ACOS:
        return NO_GO, "广告空间不足，有毛利但难以起量"

    if opportunity_score < MIN_OPPORTUNITY_SCORE:
        return NO_GO, f"机会分 {opportunity_score:.2f} 不足以支撑投入"

    # 到这里利润与需求都过了，只剩合规的确定性
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
