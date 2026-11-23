"""退货率压测（`docs/04` §25）：两档都为正才值得谈价。

守两件事：

1. **压测表上的数不许漂**。§25 那张表是 16:37 用 `db.py screen --stress-returns`
   现算出来的，换成代码里另一条路径重算就会走样——所以这里钉死到 1e-4。
2. **判定规则本身**。「压力档转负 → 不值得谈」是决策规则，不是显示文案：
   写成文档只能读，写成代码改口径时这条会红。

两档退货率的取值来自用户 2026-11-23 的指定（净化器基线 12% / 压力 17%，
咖啡机基线 8% / 压力 15%），AI 设定、非核实。11-23 18:35 起已进库
（`product_returns`，v14），两行仍是 draft——表让「借来的还是猜的」可见，
没有让它们变成读到的。
"""

from __future__ import annotations

import pytest

from xborder_insight.domain import admission as adm

PURIFIER = ("净化器", 79.99, 0.125, 11.35)
COFFEE = ("咖啡机", 63.99, 0.237, 14.40)


def _cand(spec, cogs, returns, fba=None):
    """§25 那批数字的前提：头程按 `DEFAULT_FREIGHT`（H24 之后要显式给）。

    这些数就是「$6.00 头程 + 给两档退货率」下的毛利，头程不写就等于
    按 0 算上界，`stress_test` 会直接拒绝（两档差值会被低估）。
    """
    name, price, duty, default_fba = spec
    return adm.Candidate(
        name=name,
        selling_price=price,
        quoted_cost=cogs,
        duty_rate=duty,
        fba_fee=default_fba if fba is None else fba,
        inbound_freight=adm.DEFAULT_FREIGHT,
        return_rate=returns,
    )


# (spec, cogs, fba, baseline, stress, baseline_margin, stress_margin)
CASES = [
    # 净化器：现价压测就转负，公允价与开口价都为正
    (PURIFIER, 32.50, None, 0.12, 0.17, 0.0315, -0.0287),
    (PURIFIER, 28.00, None, 0.12, 0.17, 0.0968, 0.0374),
    (PURIFIER, 24.00, None, 0.12, 0.17, 0.1549, 0.0962),
    # 咖啡机：现价与公允价都在压力档转负，只有改箱规才转正
    (COFFEE, 18.50, None, 0.08, 0.15, 0.0790, -0.0038),
    (COFFEE, 20.00, None, 0.08, 0.15, 0.0494, -0.0338),
    (COFFEE, 18.50, 11.85, 0.08, 0.15, 0.1188, 0.0361),
]


@pytest.mark.parametrize(
    ("spec", "cogs", "fba", "baseline", "stress", "base_margin", "stress_margin"),
    CASES,
)
def test_stress_table_matches_the_documented_numbers(
    spec, cogs, fba, baseline, stress, base_margin, stress_margin
):
    """§25 表里的六个格子，必须与代码算出来的一致（±0.01 个点）。"""
    result = adm.stress_test(_cand(spec, cogs, baseline, fba), stress)
    assert result.baseline_margin == pytest.approx(base_margin, abs=1e-4)
    assert result.stress_margin == pytest.approx(stress_margin, abs=1e-4)


def test_positive_baseline_negative_stress_is_not_worth_negotiating():
    """咖啡机现价：基线 7.90% 好看，压力档 −0.38% —— 判定必须是否，不能被基线骗。

    这条是规则的边界样本：基线远在 0 以上、压力档只差 0.38 个点转负。
    如果判定写成「基线达标就谈」，这一台就会被送进谈判桌。
    """
    result = adm.stress_test(_cand(COFFEE, 18.50, 0.08), 0.15)
    assert result.baseline_ok is True
    assert result.stress_ok is False
    assert result.worth_negotiating is False
    assert result.label == "不值得谈"
    assert "换杠杆" in result.reason


def test_purifier_at_24_clears_both_rounds():
    """净化器 $24.00：12% 与 17% 都为正，且基线过 15% 线 —— 这是它可以谈的理由。"""
    result = adm.stress_test(_cand(PURIFIER, 24.00, 0.12), 0.17)
    assert result.worth_negotiating is True
    assert result.baseline_margin >= adm.TARGET_MARGIN
    assert result.cushion_pts == pytest.approx(9.62, abs=0.01)


def test_box_spec_flips_coffee_without_touching_price():
    """咖啡机同一口价 $18.50，只把 FBA 从 $14.40 降到 $11.85：压力档转正。

    这是 §25 的核心结论——咖啡机的杠杆在箱子，不在价格。
    """
    before = adm.stress_test(_cand(COFFEE, 18.50, 0.08), 0.15)
    after = adm.stress_test(_cand(COFFEE, 18.50, 0.08, fba=11.85), 0.15)
    assert before.worth_negotiating is False
    assert after.worth_negotiating is True
    assert (after.stress_margin - before.stress_margin) * 100 == pytest.approx(3.99, abs=0.02)


def test_stress_rate_must_exceed_baseline():
    """两个参数写倒时必须报错，不能静默算出一条永远为正的结论。"""
    with pytest.raises(adm.AdmissionInputInvalid):
        adm.stress_test(_cand(PURIFIER, 24.00, 0.17), 0.12)


def test_stress_refuses_to_run_on_incomplete_input():
    """缺税率就没有毛利，压测不许用默认值补——与 `evaluate()` 同一条纪律。"""
    broken = adm.Candidate(
        name="缺税率", selling_price=79.99, quoted_cost=24.00, duty_rate=None, fba_fee=11.35
    )
    with pytest.raises(adm.AdmissionInputInvalid):
        adm.stress_test(broken, 0.17)


# ---------- 判定规则只有一份（11-23 19:20：接口接表后新增） ----------
#
# 算毛利有两个入口：CLI 走 `stress_test()`（构造 Candidate 再 evaluate），
# 决策接口走 `profit.compute()` 两遍（它不构造 Candidate，因为 FBA 可能是
# 费率表算出来的区间）。两个入口都要用同一条「两档都为正才值得谈」，
# 所以规则被抽到 `StressResult.from_margins()`，`stress_test()` 只是它的
# 第一个调用方。下面两条锁住这件事。


def test_from_margins_reproduces_stress_test_verdict():
    """同一个数，两条路必须给出同一套判定。

    `from_margins` 若哪天自己算一遍（比如把门槛从 0 改成 15%），
    CLI 与接口就会各说一套，而两边都「有测试」。
    """
    via_candidate = adm.stress_test(_cand(PURIFIER, 24.00, 0.12), 0.17)
    via_margins = adm.StressResult.from_margins(
        baseline_returns=via_candidate.baseline_returns,
        stress_returns=via_candidate.stress_returns,
        baseline_margin=via_candidate.baseline_margin,
        stress_margin=via_candidate.stress_margin,
    )
    assert via_margins.as_dict() == via_candidate.as_dict()
    assert via_margins.label == "值得谈"


def test_from_margins_rejects_inverted_tiers():
    """接口那条路也必须挡住「两档写倒」——挡在 service 之前就晚了。"""
    with pytest.raises(adm.AdmissionInputInvalid):
        adm.StressResult.from_margins(
            baseline_returns=0.17,
            stress_returns=0.12,
            baseline_margin=0.09,
            stress_margin=0.15,
        )
