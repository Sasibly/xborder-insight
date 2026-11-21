"""护栏：为什么「补一个 profit CLI」不能先做。

11-21 定下的下一步是「先等售价再补 `profit` 命令」。这些测试把那条决定的
**技术依据**钉住，免得下次有人觉得「代码都在，加个 argparse 就完事」。

事实链条是这样的：

1. `profit.compute()` 是纯函数，API 侧已经接通；缺的从来不是代码。
2. `CostProfileInput` 里 `inbound_freight` / `duty_rate` / `return_rate` /
   `returns_handling_fee` / `returns_goods_loss_ratio` / `returns_inbound_loss_ratio`
   都有默认值 0（`returns_refund_admin_rate` 默认 0.20）。
   默认值本身没写错，错的是它让「漏填」和「这一项确实是 0」长得完全一样。
3. 于是只要有人写个 CLI 把命令行参数直接倒进 schema，第一个用它的人
   只会填售价和采购价，然后拿到一个「看起来正常」的毛利率——不报错、不告警。

下面两条测试分别锁「漏填 ≡ 零成本」和「差值有多大」。
差值那条不是精确值断言（改费率表会变），它断言的是**方向与量级**：
同一件货补上三个数就从 GO 翻到 NO_GO。
"""

from __future__ import annotations

from xborder_insight.domain import decision as decision_mod
from xborder_insight.domain import profit as profit_mod
from xborder_insight.schemas import CostProfileInput


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


def test_omitted_cost_fields_are_indistinguishable_from_declared_zero():
    """漏填三项 ≠ 这三项是 0，但模型分不出来——这就是不能急着做 CLI 的原因。

    若哪天有人把这几项改成必填（`Field(...)`），这条测试会失败。
    那时应该做的不是改测试，而是先确认 H17 拍过板。
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
    assert a.gross_profit == b.gross_profit
    assert a.gross_margin == b.gross_margin
    # 两条路径都不告警：漏填不会在输出里留下任何痕迹
    assert a.warnings == b.warnings


def test_three_missing_numbers_flip_the_verdict_from_go_to_no_go():
    """咖啡机实测 FBA $14.40：只填售价与采购价 → GO；补三个数 → NO_GO。

    三个数是头程 $2.50/件、关税 35%、退货率 8%（含处理费 $3/件、商品损失 30%、
    头程沉没 20%）。前两项属 H3/H8，第三项属 H3——都还没给。
    """
    thin = CostProfileInput(
        quoted_price=18.0, selling_price=59.99, referral_rate=0.15, fba_fee=14.40
    )
    thin_result = profit_mod.compute(_inputs_from(thin))

    full = CostProfileInput(
        quoted_price=18.0,
        selling_price=59.99,
        referral_rate=0.15,
        fba_fee=14.40,
        incoterm="FOB",
        inbound_freight=2.50,
        duty_rate=0.35,
        return_rate=0.08,
        returns_handling_fee=3.0,
        returns_goods_loss_ratio=0.30,
        returns_inbound_loss_ratio=0.20,
    )
    full_result = profit_mod.compute(_inputs_from(full))

    assert thin_result.gross_margin >= decision_mod.MIN_GROSS_MARGIN
    assert full_result.gross_margin < decision_mod.MIN_GROSS_MARGIN
    # 量级断言：不是「差一点点」，是差二十个点以上
    assert thin_result.gross_margin - full_result.gross_margin > 0.20


def test_zero_freight_under_fob_is_warned_but_never_blocked():
    """默认 0 至少会在 FOB 下留一句告警——这是现有护栏的全部。

    它挡不住「用了默认值」这件事本身，只说明「我记得提一句」。
    """
    profile = CostProfileInput(
        quoted_price=18.0,
        selling_price=59.99,
        referral_rate=0.15,
        fba_fee=14.40,
        incoterm="FOB",
    )
    result = profit_mod.compute(_inputs_from(profile))
    assert any("头程为 0" in w for w in result.warnings)
    assert result.gross_profit > 0  # 告警不等于拦住
