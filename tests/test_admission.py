"""候选准入（`docs/04` §18.7）：把「这个品能不能做」变成一条命令。

门禁守两件事：

1. **算出来的数与文档表一致**。五台的采购上限（$16.74 / $14.90 / ...）是
   `docs/07` H21 谈判桌上要用的数，它们不能随代码重构悄悄漂。
2. **缺项不给默认值**。这是本模块存在的主要风险：候选往往缺税率、缺 FBA，
   一旦填个「大概 12.5%」放行，毛利率会静默高 1–11 个点，输出上没有任何痕迹。

FBA 值是固定的六位小数（来源：`python scripts/db.py fba-envelope --dims ... --exact-weight`），
不在这里重算——重算会把两个测试绑死在一起，改一处两边都红。
"""

from __future__ import annotations

import pytest

from xborder_insight.domain import admission as adm

# (name, price, cogs, duty, fba) —— 五台的已知量，与 tests/test_five_sku_margins.py 同源
FIVE = [
    ("air_purifier", 79.99, 32.50, 0.125, 23.83605),
    ("coffee_machine", 63.99, 18.50, 0.237, 14.39685),
    ("electric_kettle", 29.99, 6.80, 0.152, 10.85715),
    ("robot_vacuum", 199.99, 118.00, 0.125, 25.51275),
    ("pet_fountain", 45.99, 14.50, 0.125, 9.36675),
]


def _cand(name, price, cogs, duty, fba, **kw):
    return adm.Candidate(
        name=name,
        selling_price=price,
        quoted_cost=cogs,
        duty_rate=duty,
        fba_fee=fba,
        **kw,
    )


@pytest.mark.parametrize(
    ("name", "price", "cogs", "duty", "fba", "max_cogs"),
    [
        ("air_purifier", 79.99, 32.50, 0.125, 23.83605, 16.74),
        ("coffee_machine", 63.99, 18.50, 0.237, 14.39685, 14.90),
        ("electric_kettle", 29.99, 6.80, 0.152, 10.85715, 1.13),
        ("robot_vacuum", 199.99, 118.00, 0.125, 25.51275, 79.85),
        ("pet_fountain", 45.99, 14.50, 0.125, 9.36675, 11.05),
    ],
)
def test_max_cogs_matches_the_negotiation_table(name, price, cogs, duty, fba, max_cogs):
    """§17 / H21 那五张谈判目标价，必须与命令算出来的一致（±1 分）。

    这些数是拿到供应商桌上说的，差一分都会被对方抓住。
    """
    result = adm.evaluate(_cand(name, price, cogs, duty, fba))
    assert result.verdict == adm.VERDICT_FAIL
    assert result.max_cogs == pytest.approx(max_cogs, abs=0.01)


@pytest.mark.parametrize(("name", "price", "cogs", "duty", "fba"), FIVE)
def test_solution_put_back_gives_exactly_target(name, price, cogs, duty, fba):
    """把解代回去必须正好踩在 15%——不能是 15.3% 或者 14.8%。

    这条是解本身的正确性校验：二分只要区间或收敛条件写错，
    上面那条「与文档一致」可能一起错，这一条不会。
    """
    result = adm.evaluate(_cand(name, price, cogs, duty, fba))
    assert result.max_cogs is not None
    at_max = adm.evaluate(_cand(name, price, result.max_cogs, duty, fba))
    assert at_max.gross_margin == pytest.approx(adm.TARGET_MARGIN, abs=1e-6)


def test_referral_drop_flips_coffee_machine_only():
    """佣金 8% 时咖啡机 15.02% 转 pass——这正是 H1 的全部价值。

    其余四台在 8% 下仍然 fail，所以「唯一值得谈的那台」这个判断成立。
    """
    flipped = []
    for name, price, cogs, duty, fba in FIVE:
        result = adm.evaluate(
            _cand(name, price, cogs, duty, fba, referral_rate=0.08)
        )
        if result.verdict == adm.VERDICT_PASS:
            flipped.append(name)
            assert result.gross_margin == pytest.approx(0.1502, abs=1e-4)
    assert flipped == ["coffee_machine"]


def test_missing_duty_is_need_info_not_a_guess():
    """不给税率 → need_info，并明确说要先归类。

    这条防的是「顺手填 12.5%」：净化器 12.5% 与 15.9% 差 $1.11/件，
    而输出上只会显示一个正常的两层小数毛利率。
    """
    result = adm.evaluate(
        adm.Candidate(name="x", selling_price=59.99, quoted_cost=18.0, duty_rate=None,
                      fba_fee=10.0)
    )
    assert result.verdict == adm.VERDICT_NEED_INFO
    assert any("duty_rate" in item for item in result.missing)
    assert result.gross_margin is None and result.max_cogs is None


def test_missing_fba_is_need_info():
    """不给 FBA 也不给占比 → need_info。FBA 是每件 $2.4–8.6 的量级，不能默认 0。"""
    result = adm.evaluate(
        adm.Candidate(name="x", selling_price=59.99, quoted_cost=18.0, duty_rate=0.125)
    )
    assert result.verdict == adm.VERDICT_NEED_INFO
    assert any("FBA" in item for item in result.missing)


def test_fba_ratio_is_a_valid_shortcut():
    """只有「品类典型 FBA 占比」时也能算——报价阶段通常就是这个状态。

    $59.99 × 15% = $9.00 的 FBA，采购 $14 时毛利 24.4%：这一台是**能做**的。
    它同时说明闸一的 20% 比例段（$59.99 × 20% = $12.00）确实保守——
    真实上限是 $18.90（占售价 31.5%，正好是 §18.3 表「$60 × FBA 15%」那格），
    所以闸一只是粗筛，不是判据。
    """
    result = adm.evaluate(
        adm.Candidate(name="x", selling_price=59.99, quoted_cost=14.0,
                      duty_rate=0.125, fba_ratio=0.15)
    )
    assert result.verdict == adm.VERDICT_PASS
    assert result.gross_margin == pytest.approx(0.2438, abs=1e-3)


def test_all_missing_reported_at_once():
    """缺项一次报全，不要挤牙膏——候选通常一次缺三四样。

    四类：售价、采购、税率、FBA（后者可由 fba_ratio 顶替，但这里两者都没给）。
    """
    result = adm.evaluate(adm.Candidate(name="x", selling_price=None,
                                        quoted_cost=None, duty_rate=None))
    assert result.verdict == adm.VERDICT_NEED_INFO
    assert len(result.missing) == 4


@pytest.mark.parametrize(
    ("price", "cogs", "verdict", "band", "limit"),
    [
        (63.99, 18.50, adm.VERDICT_FAIL, "amount", 18.30),
        (63.99, 18.30, adm.VERDICT_PASS, "amount", 18.30),
        (59.00, 18.30, adm.VERDICT_PASS, "amount", 18.30),
        (58.99, 18.30, adm.VERDICT_FAIL, "ratio", 11.798),
        (40.00, 8.00, adm.VERDICT_PASS, "ratio", 8.00),
        (39.99, 5.00, adm.VERDICT_NEED_INFO, "size_first", None),
        (30.00, 1.20, adm.VERDICT_NEED_INFO, "size_first", None),
    ],
)
def test_gate_three_bands(price, cogs, verdict, band, limit):
    """闸一的三段与两个边界（$59 / $40）——`docs/04` §18.7 那张分段表。

    边界值本身也测：$59.00 落「金额段」，$58.99 落「比例段」。
    分界写错一位，就会有一整档候选被判错，而这种错不会报错。
    """
    gate = adm.screening_gate(price, cogs)
    assert gate.verdict == verdict
    assert gate.band == band
    if limit is None:
        assert gate.limit is None
    else:
        assert gate.limit == pytest.approx(limit, abs=0.01)


def test_gate_would_wrongly_kill_a_workable_candidate():
    """闸一判 fail、闸二判 pass 的样本：$80 售价 + $20 采购 + FBA $20.09。

    结论必须以闸二为准，并且 `notes` 要把这件事说出来——两道闸打架时，
    读者会挑自己爱听的那个，所以不能只留两个结论了事。

    这个样本的自纠史写在 `docs/04` §18.7 末尾：最初算成 FBA $8.09，
    是把「目标毛利额」在成本侧又扣了一次，实际 $8.09 时毛利是 **30%**，
    15% 对应的 FBA 上限是 **$20.09**。差的那 $12 正是 15% × $80。
    """
    result = adm.evaluate(_cand("sample", 80.0, 20.0, 0.125, 20.09))
    assert result.verdict == adm.VERDICT_PASS
    assert result.gross_margin == pytest.approx(0.15, abs=5e-4)
    assert result.gate is not None and result.gate.verdict == adm.VERDICT_FAIL
    assert any("误杀" in note for note in result.notes)


def test_gate_min_price_constant_is_derived_not_guessed():
    """$59 这个分界是从 $18.30 反解出来的，不是拍的。

    改了 `GATE_AMOUNT` 就必须同步重算它，否则金额段会放进做不成的候选。
    这条把两者的关系钉住。
    """
    needed = adm.min_price_for_cogs(
        adm.GATE_AMOUNT, 8.95, 0.125
    )
    assert needed == pytest.approx(adm.GATE_AMOUNT_MIN_PRICE, abs=0.25)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_zero_or_negative_input_is_invalid_not_need_info(bad):
    """0 与负数不是「还判不了」，是输入不成立——两者要分开，否则会被当成待补数据。"""
    with pytest.raises(adm.AdmissionInputInvalid):
        adm.evaluate(
            adm.Candidate(name="x", selling_price=bad, quoted_cost=10.0,
                          duty_rate=0.125, fba_fee=8.0)
        )


def test_overrun_equals_margin_gap():
    """fail 时「超预算点数」与「毛利缺口」是同一个数（§18.2 的恒等式）。"""
    for name, price, cogs, duty, fba in FIVE:
        result = adm.evaluate(_cand(name, price, cogs, duty, fba))
        assert result.overrun_pts == pytest.approx(
            (adm.TARGET_MARGIN - result.gross_margin) * 100, abs=1e-9
        )
