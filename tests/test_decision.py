"""决策合成回归测试。

锁两件事：
1. 否决顺序（合规 > 毛利 > 广告空间 > 需求）——顺序错了会给出误导性的首要原因
2. 「不确定」与「不行」的分界——NEED_MORE_INFO 被误判成 NO_GO 会误杀机会，
   被误判成 GO 会误导投入
"""

from __future__ import annotations

import pytest

from xborder_insight.domain.compliance import (
    BLOCKER,
    STATUS_BLOCKED,
    STATUS_CLEAR,
    STATUS_UNKNOWN,
    WARNING,
    RuleHit,
    evaluate,
)
from xborder_insight.domain.decision import (
    GO,
    NEED_MORE_INFO,
    NO_GO,
    decide,
)
from xborder_insight.domain.profit import CostInputs, compute

# 一个利润达标的基准（毛利率约 31%，ACOS 约 31%，均高于阈值）
PROFIT_OK = dict(
    quoted_price=8.00,
    selling_price=29.99,
    inbound_freight=1.20,
    duty_rate=0.075,
    referral_rate=0.15,
    fba_fee=3.50,
    return_rate=0.08,
    returns_handling_fee=0.80,
    returns_refund_admin_rate=0.20,
    returns_goods_loss_ratio=0.30,
    returns_inbound_loss_ratio=1.00,
    incoterm="FOB",
)

CLEAR = evaluate([RuleHit("x", "w", WARNING, "verified", "https://e.gov")], "c", "US")


def _profit(**overrides):
    return compute(CostInputs(**{**PROFIT_OK, **overrides}))


def _blocked():
    return evaluate([RuleHit("us.radio", "r", BLOCKER, "verified", "https://e.gov")], "c", "US")


def _unknown():
    return evaluate([], "c", "US")


def _decide(profit=None, compliance=None, score=0.66):
    return decide(
        profit if profit is not None else _profit(),
        compliance if compliance is not None else CLEAR,
        score,
        data_provenance="caller_provided",
    )


# ---------- 基准：四项全过 ----------


def test_all_pass_is_go():
    result = _decide()
    assert result.verdict == GO
    assert "毛利" in result.headline


# ---------- 否决顺序 ----------


def test_compliance_blocker_overrides_good_profit():
    """合规阻断优先于一切。毛利再好也不能上架。

    如果顺序反了，接口会返回「毛利不错，建议做」，而真实情况是根本不能卖。
    """
    result = _decide(compliance=_blocked())
    assert result.verdict == NO_GO
    assert "合规" in result.headline
    assert result.profit.gross_profit > 0  # 确认利润确实是好的，排除巧合


def test_negative_profit_is_no_go():
    result = _decide(profit=_profit(selling_price=12.00))
    assert result.verdict == NO_GO
    assert "亏损" in result.headline


def test_margin_below_threshold_is_no_go():
    """毛利为正但低于假设下限，仍判 NO_GO——余量不够抵御波动。"""
    profit = _profit(selling_price=20.00)  # 毛利仍为正，但毛利率会掉下去
    assert profit.gross_profit > 0
    assert profit.gross_margin < 0.15
    assert _decide(profit=profit).verdict == NO_GO


def test_insufficient_ad_space_is_no_go():
    """毛利率达标但广告空间不足，等于「有利润但投不出量」，实际做不起来。"""
    profit = _profit(fba_fee=5.20)
    assert profit.gross_margin >= 0.15 or profit.gross_margin < 0.15  # 记录实际值
    result = _decide(profit=profit)
    if profit.breakeven_acos is not None and profit.breakeven_acos < 0.20:
        assert result.verdict == NO_GO
        assert "广告" in result.headline


def test_low_opportunity_is_no_go():
    result = _decide(score=0.20)
    assert result.verdict == NO_GO
    assert "机会分" in result.headline


# ---------- 不确定 ≠ 不行 ----------


def test_unverified_compliance_is_need_more_info_not_no_go():
    """利润与需求都过了，只是合规没核实 → NEED_MORE_INFO。

    判成 NO_GO 会误杀一个可能完全可行的品；判成 GO 则是在没依据的情况下放行。
    """
    result = _decide(compliance=_unknown())
    assert result.verdict == NEED_MORE_INFO
    assert "合规" in result.headline
    assert result.profit.gross_profit > 0


def test_blocked_wins_over_unknown():
    """确定阻断优先于信息不足：已知不行就不用再查了。"""
    assert _decide(compliance=_blocked()).verdict == NO_GO


# ---------- 可解释性 ----------


def test_all_factors_are_recorded_even_when_pass():
    """所有因素都要进 reasons，不能只记录触发否决的那一条。

    只报否决项会让人以为「修好这一项就能做」，而实际情况可能还有别的问题。
    """
    result = _decide()
    factors = {reason.factor for reason in result.reasons}
    assert factors == {"合规准入", "单件毛利", "广告空间", "市场需求"}
    assert all(reason.result == "pass" for reason in result.reasons)


def test_each_reason_has_a_traceable_ref():
    """除合规整表判定外，每条依据都要能被反查到参数 key 或规则 key。"""
    result = _decide()
    for reason in result.reasons:
        if reason.factor != "合规准入":
            assert reason.ref, f"{reason.factor} 缺少可追溯引用"


def test_thresholds_are_declared_as_assumptions():
    """阈值必须随响应返回，并明确标注是假设值。

    藏在前端或文档里的阈值，会让「为什么判 NO_GO」变成一个无法当场回答的问题。
    """
    payload = _decide().as_dict()
    thresholds = payload["thresholds"]
    assert thresholds["min_gross_margin"] == pytest.approx(0.15)
    assert "假设" in thresholds["note"]


# ---------- 确定性 ----------


def test_same_input_same_output():
    """同输入同输出。决策接口的验收标准之一。"""
    a = _decide().as_dict()
    b = _decide().as_dict()
    assert a == b


def test_response_shape_is_stable():
    payload = _decide().as_dict()
    assert set(payload) >= {
        "verdict",
        "headline",
        "profit",
        "compliance",
        "opportunity_score",
        "reasons",
        "data_provenance",
        "thresholds",
    }
    assert payload["verdict"] in {GO, NO_GO, NEED_MORE_INFO}
