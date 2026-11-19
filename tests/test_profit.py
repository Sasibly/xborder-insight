"""利润模型回归测试。

这个文件的核心作用不是「测试覆盖率」，而是把**口径**钉死：
贸易术语如何影响关税与头程、退货四段各自的基数是什么。
口径一旦被改，这里会立刻红——那正是我们要的，因为口径变化会悄悄
让所有历史结论失效，而输出格式看起来毫无变化。

所有期望值都是手工算出来的，不用「跑一遍把结果存下来当期望」的写法：
后者只能证明「代码没变」，不能证明「算得对」。
"""

from __future__ import annotations

import pytest

from xborder_insight.domain import incoterm as incoterm_mod
from xborder_insight.domain.profit import CostInputs, ReturnsLoss, compute
from xborder_insight.errors import InputInvalid

# 一个干净的手算基准（无线充电器 × 美国，FOB 口径）
BASE = dict(
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
)


def _inputs(**overrides) -> CostInputs:
    return CostInputs(**{**BASE, **overrides})


# ---------- 1. 全链路手算对齐 ----------


def test_fob_matches_hand_calculation():
    """FOB 口径的完整手算校验。

    落地成本 = 8.00 + 1.20 + 8.00×0.075 = 9.80
    佣金     = 29.99×0.15 = 4.4985
    退款冲减 = 29.99×0.08 = 2.3992
    退货费用 = 0.08×0.80 + 0.08×0.20×4.4985 + 0.08×0.30×8.00 + 0.08×1.00×1.20
             = 0.064 + 0.071976 + 0.192 + 0.096 = 0.423976
    退货影响 = 2.3992 + 0.423976 = 2.823176
    毛利     = 29.99 − 9.80 − 4.4985 − 3.50 − 2.823176 = 9.368324
    """
    result = compute(_inputs(incoterm="FOB"))

    assert result.freight == pytest.approx(1.20)
    assert result.duty == pytest.approx(0.60)
    assert result.landed_cost == pytest.approx(9.80)
    assert result.referral_fee == pytest.approx(4.4985)
    assert result.returns.revenue_reversal == pytest.approx(2.3992)
    assert result.returns.handling == pytest.approx(0.064)
    assert result.returns.refund_admin == pytest.approx(0.071976)
    assert result.returns.goods == pytest.approx(0.192)
    assert result.returns.inbound == pytest.approx(0.096)
    assert result.returns.total == pytest.approx(2.823176)
    assert result.gross_profit == pytest.approx(9.368324)
    assert result.gross_margin == pytest.approx(0.3123816, abs=1e-6)


def test_breakeven_acos_equals_margin_when_profitable():
    """盈亏平衡 ACOS 的定义：毛利 ÷ 售价，即广告最多能吃掉多少比例。

    它恰好等于毛利率，这不是巧合——毛利就是「除广告外都扣完之后剩下的额度」。
    """
    result = compute(_inputs(incoterm="FOB"))
    assert result.breakeven_acos == pytest.approx(result.gross_margin)
    assert result.breakeven_reason is None


# ---------- 2. 术语分支：条件生效 ----------


def test_ddp_does_not_stack_duty_again():
    """DDP 报价已含税，再叠加关税就是重复计税。

    这是本模型最容易出错、也最难被发现的一处：
    多算一道关税后，毛利率依然是个漂亮的百分数，页面上看不出任何异常。
    """
    result = compute(_inputs(incoterm="DDP", quoted_price=10.50))

    assert result.duty == 0.0
    assert result.freight == 0.0
    assert result.landed_cost == pytest.approx(10.50)  # 落地成本即报价
    assert result.duty_included_in_quote is True
    # 头程损失也随之为 0：那部分沉没成本已包含在报价里
    assert result.returns.inbound == 0.0


def test_ddp_reports_warning_when_duty_rate_still_passed():
    """含税术语下仍传税率 → 必须告警，而不是静默置零。

    静默置零会让「调用方以为算了关税」和「模型确实没算」这两件事无法区分。
    """
    result = compute(_inputs(incoterm="DDP", quoted_price=10.50))
    joined = " ".join(result.warnings)
    assert "重复计税" in joined
    assert "FOB" in joined  # 告警要给出修正路径，不能只说「错了」


def test_cif_does_not_stack_freight():
    """CIF 报价已含运费，头程不可重复计入，但关税仍要算。"""
    result = compute(_inputs(incoterm="CIF", quoted_price=9.40))

    assert result.freight == 0.0
    assert result.duty == pytest.approx(9.40 * 0.075)  # 关税照算
    assert any("头程" in w for w in result.warnings)


def test_fob_keeps_both_freight_and_duty():
    """FOB 下两项都要计入——它是「什么都没含」的那一端。"""
    result = compute(_inputs(incoterm="FOB"))
    assert result.freight > 0
    assert result.duty > 0


@pytest.mark.parametrize("code", ["EXW", "FOB", "CFR", "CIF", "DAP"])
def test_only_ddp_has_duty_included(code):
    """术语表的横切校验：只有 DDP 的关税是含在报价里的。

    这条断言防的是「有人为了省事给 CIF 也标上 duty_included」——
    那会让 CIF 的关税凭空消失，且不产生任何告警。
    """
    assert incoterm_mod.terms(code).duty_included is False


def test_unknown_incoterm_is_rejected_as_input_error():
    """拼错的术语必须报 422 型错误。填 FOBb 时静默按某个分支计价是最坏的结果。

    同时验证错误码语义：这是调用方输入问题，不是服务端 500。
    """
    with pytest.raises(InputInvalid, match="FOBb") as exc:
        compute(_inputs(incoterm="FOBb"))
    assert exc.value.status_code == 422
    assert exc.value.code == "input_invalid"


def test_default_incoterm_is_echoed():
    """用了默认值必须回显，不能悄悄按 FOB 算完就当没发生。"""
    result = compute(_inputs(incoterm=None))
    assert result.incoterm == incoterm_mod.DEFAULT_INCOTERM
    assert result.incoterm_assumed is True
    assert any("默认" in a for a in result.assumptions)


def test_explicit_incoterm_is_not_marked_assumed():
    result = compute(_inputs(incoterm="FOB"))
    assert result.incoterm_assumed is False


# ---------- 3. 退货四段的基数 ----------


def test_refund_admin_is_based_on_commission_not_price():
    """退款管理费按原佣金取比例，不是按售价。

    基数是售价时会放大一个数量级（29.99 vs 4.4985），
    毛利被系统性低估，而这种偏差在测试里几乎不可能偶然发现。
    """
    result = compute(_inputs(incoterm="FOB"))
    expected = 0.08 * 0.20 * result.referral_fee
    wrong_basis = 0.08 * 0.20 * BASE["selling_price"]
    assert result.returns.refund_admin == pytest.approx(expected)
    assert result.returns.refund_admin != pytest.approx(wrong_basis, rel=1e-3)


def test_refund_admin_fee_capped_at_five_dollars():
    """官方规则：退款管理费取 min($5.00, 原佣金的 20%)，按每笔退款计。

    原文见 Seller Central 费率表："the lesser of $5.00 or 20% of the
    applicable referral fee"。佣金超过 $25 后上限生效——
    不封顶会高估退货成本，且高估幅度随售价增长。
    """
    result = compute(_inputs(incoterm="FOB", selling_price=200.00, return_rate=1.0))

    referral = 200.00 * 0.15  # 30.00
    assert referral * 0.20 > 5.00  # 确认这一档确实触发上限
    assert result.returns.refund_admin == pytest.approx(5.00)
    assert any("上限" in a for a in result.assumptions)


def test_refund_admin_fee_not_capped_for_low_price():
    """低价商品碰不到上限，说明封顶逻辑没有误伤常规档位。"""
    result = compute(_inputs(incoterm="FOB", return_rate=1.0))
    per_return = 0.20 * (29.99 * 0.15)  # ≈ 0.90 < 5.00
    assert result.returns.refund_admin == pytest.approx(per_return)


def test_returns_total_is_sum_of_all_parts():
    """五段必须等于合计。防止将来有人新增一段却忘了改 total。"""
    result = compute(_inputs(incoterm="FOB"))
    parts = result.returns
    assert parts.total == pytest.approx(
        parts.revenue_reversal + parts.handling + parts.refund_admin + parts.goods + parts.inbound
    )


def test_revenue_reversal_is_what_makes_high_return_rate_lose_money():
    """回归锁：没有退款冲减时，50% 退货率会算出「盈利」。

    这是本项目到目前为止最贵的一个建模错误——数值合法、格式漂亮、结论相反。
    本测试同时验证两件事：修复后为亏损；且把修复项摘掉就会重现旧错误结论。
    如果哪天有人「简化」掉 revenue_reversal，这条会立刻失败。
    """
    result = compute(_inputs(incoterm="FOB", return_rate=0.5))

    assert result.gross_profit < 0

    fees_only = (
        result.returns.handling
        + result.returns.refund_admin
        + result.returns.goods
        + result.returns.inbound
    )
    without_reversal = (
        result.selling_price - result.landed_cost - result.referral_fee - result.fba_fee - fees_only
    )
    assert without_reversal > 0  # 旧模型的错误结论：明确记录它长什么样


def test_zero_return_rate_zeroes_all_returns():
    result = compute(_inputs(incoterm="FOB", return_rate=0.0))
    assert result.returns.total == 0.0


def test_full_return_rate_does_not_raise():
    """退货率 100% 是极端但合法的输入：结论应为亏损，而不是抛异常。"""
    result = compute(_inputs(incoterm="FOB", return_rate=1.0))
    assert result.gross_profit < 0
    assert result.breakeven_acos is None


# ---------- 4. 负毛利时 ACOS 必须缺席 ----------


def test_negative_margin_returns_none_instead_of_negative_acos():
    """亏损时不返回负数 ACOS。

    「盈亏平衡 ACOS = −0.20」会被读成「还有 20% 空间」，
    这是模型能犯的最危险的错误之一：数值合法、含义相反。
    """
    result = compute(_inputs(incoterm="FOB", selling_price=12.00))

    assert result.gross_profit < 0
    assert result.breakeven_acos is None
    assert result.breakeven_reason is not None
    assert "广告" in result.breakeven_reason


# ---------- 5. 输入边界 ----------


def test_zero_price_rejected():
    with pytest.raises(InputInvalid, match="售价必须大于 0"):
        compute(_inputs(incoterm="FOB", selling_price=0))


def test_return_rate_above_one_rejected_with_hint():
    """退货率 > 1 通常是「百分比没除以 100」，报错要直接点出这种可能。"""
    with pytest.raises(InputInvalid, match="除以 100"):
        compute(_inputs(incoterm="FOB", return_rate=8))


def test_negative_cost_rejected():
    with pytest.raises(InputInvalid, match="不能为负"):
        compute(_inputs(incoterm="FOB", fba_fee=-1))


# ---------- 6. 确定性与序列化 ----------


def test_same_input_same_output():
    """同输入同输出。没有这一步，后面「结论可复现」的承诺就是空的。"""
    a = compute(_inputs(incoterm="FOB"))
    b = compute(_inputs(incoterm="FOB"))
    assert a.as_dict() == b.as_dict()


def test_returns_loss_dict_shape_is_stable():
    """看板与 API 依赖这个结构，字段名不能悄悄改。"""
    payload = ReturnsLoss(
        revenue_reversal=0.5,
        handling=0.1,
        refund_admin=0.2,
        goods=0.3,
        inbound=0.4,
    ).as_dict()
    assert set(payload) == {
        "revenue_reversal",
        "handling_fee",
        "refund_admin_fee",
        "goods_loss",
        "inbound_loss",
        "total",
    }
    assert payload["total"] == pytest.approx(1.5)


def test_assumptions_explain_every_conditional_branch():
    """每个「条件生效」的分支都要留下口径记录。

    这是可审计性的最低要求：看到结果的人能知道当时按什么口径算的。
    """
    fob = compute(_inputs(incoterm="FOB"))
    ddp = compute(_inputs(incoterm="DDP", quoted_price=10.50))

    assert any("关税" in a for a in fob.assumptions)
    assert any("已含进口关税" in a for a in ddp.assumptions)
    assert any("已含头程运费" in a for a in ddp.assumptions)
