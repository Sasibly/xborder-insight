"""护栏：利润模型不许把「没给」当成「是 0」。

11-21 的判断是「先等数，别急着补 profit CLI」：`CostProfileInput` 里六项成本
都有默认值 0，于是漏填与「这一项真的是 0」在模型里长得一模一样——
咖啡机只填售价与采购价能算出 **30.99%**，补齐三项后是 **6.90%**（`docs/04` §14.3），
中间那条路径不报错、不告警。

11-23 按 `H17` 落地：六项改成 `float | None = None`，漏填有了独立的表示。
模型据此知道「算出来的毛利率是上界」，并拒绝把它报成 GO（`docs/04` §31）。

这一组锁的是**算术没变、表态变了**：同一份输入补齐与不补齐，
毛利率数值可以一样，但 `missing_inputs` 与结论必须不同。
单看数字看不出来的那条差异，正是这组测试存在的理由。
"""

from __future__ import annotations

import pytest

from xborder_insight.domain import decision as decision_mod
from xborder_insight.domain import profit as profit_mod
from xborder_insight.domain.compliance import STATUS_CLEAR, RuleHit, WARNING, evaluate
from xborder_insight.domain.decision import NEED_MORE_INFO, NO_GO
from xborder_insight.schemas import CostProfileInput

CLEAR = evaluate([RuleHit("x", "w", WARNING, "verified", "https://e.gov")], "c", "US")

#: 六项可缺省的输入。列在这里而不是逐个写死，是为了以后加字段时
#: 「哪些项算缺失」这件事只需要改一处。
OPTIONAL_FIELDS = (
    "inbound_freight",
    "duty_rate",
    "return_rate",
    "returns_handling_fee",
    "returns_goods_loss_ratio",
    "returns_inbound_loss_ratio",
)


def _inputs_from(profile: CostProfileInput) -> profit_mod.CostInputs:
    """只搬字段，不做任何补全——补全正是被测对象。"""
    return profit_mod.CostInputs(
        quoted_price=profile.quoted_price,
        selling_price=profile.selling_price,
        inbound_freight=profile.inbound_freight,
        duty_rate=profile.duty_rate,
        referral_rate=profile.referral_rate,
        fba_fee=profile.fba_fee,
        return_rate=profile.return_rate,
        returns_handling_fee=profile.returns_handling_fee,
        returns_refund_admin_rate=profile.returns_refund_admin_rate,
        returns_goods_loss_ratio=profile.returns_goods_loss_ratio,
        returns_inbound_loss_ratio=profile.returns_inbound_loss_ratio,
        incoterm=profile.incoterm,
    )


def _decide(profile: CostProfileInput, score: float = 0.66):
    return decision_mod.decide(
        profit_mod.compute(_inputs_from(profile)),
        CLEAR,
        score,
        data_provenance="test",
    )


# ---------- 核心：漏填与显式 0 从此可分辨 ----------


def test_omitted_fields_are_recorded_as_missing_not_silently_zero():
    """两个模型的毛利仍然相等（算术没动），但缺失记录与告警必须不同。

    这条用例的前身断言的是「两条路径完全相同」——那是缺陷的描述，
    不是需求的描述。H17 之后反过来：数值可以相同，**状态不能相同**。
    """
    omitted = CostProfileInput(
        quoted_price=18.0, selling_price=59.99, referral_rate=0.15, fba_fee=14.40
    )
    declared = CostProfileInput(
        quoted_price=18.0,
        selling_price=59.99,
        referral_rate=0.15,
        fba_fee=14.40,
        inbound_freight=0.0,
        duty_rate=0.0,
        return_rate=0.0,
    )
    a = profit_mod.compute(_inputs_from(omitted))
    b = profit_mod.compute(_inputs_from(declared))

    # 算术上仍然按 0 计——不因为「没给」就拒绝算，调用方在报价阶段确实可能还没有头程
    assert a.gross_margin == b.gross_margin == pytest.approx(a.gross_margin)
    # 但状态分开了
    assert set(a.missing_inputs) == {"inbound_freight", "duty_rate", "return_rate"}
    assert b.missing_inputs == ()
    assert a.warnings != b.warnings
    assert any("未指定" in w for w in a.warnings)
    assert "missing_inputs" in a.as_dict()


def test_missing_fields_make_the_margin_an_upper_bound_not_a_verdict():
    """上界达标不能报 GO；补齐后同一件货是 NO_GO。

    这就是 §14.3 那张表里 30.99% → 6.90% 的路径。以前第一步直接是 GO。
    """
    thin = CostProfileInput(
        quoted_price=18.0, selling_price=59.99, referral_rate=0.15, fba_fee=14.40
    )
    full = CostProfileInput(
        quoted_price=18.0,
        selling_price=59.99,
        referral_rate=0.15,
        fba_fee=14.40,
        incoterm="FOB",
        inbound_freight=2.50,
        duty_rate=0.237,
        return_rate=0.08,
        returns_handling_fee=3.0,
        returns_goods_loss_ratio=0.30,
        returns_inbound_loss_ratio=0.20,
    )

    thin_result = _decide(thin)
    full_result = _decide(full)

    assert thin_result.verdict == NEED_MORE_INFO
    assert "未指定" in thin_result.headline
    assert full_result.verdict == NO_GO
    # 数值上的差距没变：不是「差一点点」，是二十个点以上
    assert thin_result.profit.gross_margin - full_result.profit.gross_margin > 0.20


def test_fail_on_the_optimistic_end_is_still_a_fail():
    """缺省只会让成本更低，所以「上界都亏」可以直接否掉，不必等补齐。

    方向不对称是本设计的关键：补齐任何一项都只会让毛利变差。
    这条性质让 FAIL 可以保留、PASS 必须降级——不是随口定的规则。
    """
    hopeless = CostProfileInput(
        quoted_price=28.0, selling_price=29.99, referral_rate=0.15, fba_fee=6.0
    )
    result = _decide(hopeless)
    assert result.profit.missing_inputs  # 确实缺着东西
    assert result.verdict == NO_GO


def test_every_missing_field_can_only_lower_the_margin():
    """单调性：把任一项从「未指定」改成非零，毛利率只会下降。

    这条是上面那条「FAIL 可以保留」的前提。若哪天有某项反向影响毛利
    （比如某种补贴要按比例加回），这个测试会红——那时上界论证就得重写。
    """
    base = dict(
        quoted_price=18.0,
        selling_price=59.99,
        referral_rate=0.15,
        fba_fee=14.40,
        incoterm="FOB",
    )
    empty = profit_mod.compute(
        _inputs_from(CostProfileInput(**base))
    ).gross_margin

    additions = {
        "inbound_freight": 2.50,
        "duty_rate": 0.237,
        "return_rate": 0.08,
        "returns_handling_fee": 3.0,
    }
    for field, value in additions.items():
        filled = profit_mod.compute(_inputs_from(CostProfileInput(**base, **{field: value})))
        assert filled.gross_margin <= empty, f"{field} 填上之后毛利反而升高了"


# ---------- 术语与前提：不是所有字段在所有口径下都「必须有」 ----------


def test_ddp_does_not_count_freight_or_duty_as_missing():
    """DDP 报价已含运费与关税，没给这两项不是缺失，是口径本身。

    缺失判断必须由模型做（它知道术语语义），不能在 schema 里一刀切。
    """
    profile = CostProfileInput(
        quoted_price=18.0,
        selling_price=59.99,
        referral_rate=0.15,
        fba_fee=14.40,
        incoterm="DDP",
        return_rate=0.0,
    )
    result = profit_mod.compute(_inputs_from(profile))
    assert result.missing_inputs == ()
    assert result.freight == 0.0 and result.duty == 0.0


def test_return_ratios_are_only_required_when_there_are_returns():
    """退货率为 0（显式声明）时，三项退货参数乘上去都是 0，缺了不算缺失。

    否则一份完整输入会被判成信息不足——把「严格」做成了「误伤」。
    """
    profile = CostProfileInput(
        quoted_price=18.0,
        selling_price=59.99,
        referral_rate=0.15,
        fba_fee=14.40,
        incoterm="FOB",
        inbound_freight=2.50,
        duty_rate=0.237,
        return_rate=0.0,
    )
    result = profit_mod.compute(_inputs_from(profile))
    assert result.missing_inputs == ()

    # 反过来：退货率非零时，这三项真的会影响数字，缺了必须记
    with_returns = CostProfileInput(
        quoted_price=18.0,
        selling_price=59.99,
        referral_rate=0.15,
        fba_fee=14.40,
        incoterm="FOB",
        inbound_freight=2.50,
        duty_rate=0.237,
        return_rate=0.08,
    )
    assert set(profit_mod.compute(_inputs_from(with_returns)).missing_inputs) == {
        "returns_handling_fee",
        "returns_goods_loss_ratio",
        "returns_inbound_loss_ratio",
    }


def test_explicit_zero_freight_is_still_warned_but_never_blocked():
    """显式声明「零运费」是合法的，只留一句告警。

    告警不等于拦住，这一点没变：调用方说了是 0，系统没有资格替他改，
    但必须让读报告的人看到这个 0 是他的声明而不是系统的默认值。
    """
    profile = CostProfileInput(
        quoted_price=18.0,
        selling_price=59.99,
        referral_rate=0.15,
        fba_fee=14.40,
        incoterm="FOB",
        inbound_freight=0.0,
        duty_rate=0.237,
        return_rate=0.0,
    )
    result = profit_mod.compute(_inputs_from(profile))
    assert result.missing_inputs == ()
    assert any("头程为 0" in w for w in result.warnings)
    assert result.gross_profit > 0  # 告警不等于拦住


def test_schema_keeps_no_default_for_the_six_optional_costs():
    """契约层的守卫：这六项一旦被改回 `default=0.0`，本用例立刻红。

    守住的是「形状」而不是某条业务结论——补默认值在任何单条结论上都看不出差别，
    只有看字段定义才看得出来。
    """
    fields = CostProfileInput.model_fields
    for name in OPTIONAL_FIELDS:
        assert fields[name].default is None, f"{name} 又有默认值了：漏填会重新变成 0"
