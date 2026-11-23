"""两套「三边 + 重量」读数互校（`package.compare_packaging`）。

这一层守的是一个很具体的坑：同一个 SKU 常有一套量箱子的数、一套供应商规格书
或商品详情页抄的数。两套都塞进费率表不报错，费用能差 19%（净化器 $12.48/件）。

不需要知道哪套是真的，就能判出哪套**不可能是彩盒**——因为箱子包着机器，
只能更大、更重。判定只有四种，每种都对应一个明确动作：

    box_vs_unit              体积大那套同时更重 → 它就是箱，另一套是裸机
    contradiction_same_box   三边几乎相同但重量差得多 → 至少一套不是这台
    contradiction_weight     体积大反而更轻 → 箱子比机器还轻，至少一套错
    indistinguishable        两套在噪声内相同 → 不构成分歧

关键约束写在测试里：**判不出时不挑「更保守」的那个顶上**。这里没有保守方向——
更小的箱意味着更低的费，而错的低费正是这类错误最贵的形态。
"""

from __future__ import annotations

import pytest

from xborder_insight.domain import package as pkg


def _r(label, dims, lb, source=""):
    return pkg.SpecReading(label=label, dims_in=dims, weight_lb=lb, source=source)


def test_bigger_and_heavier_wins_the_carton():
    """箱 = 体积大的那套，且它必须同时更重。这是唯一能放心用的一种结论。"""
    relation = pkg.compare_packaging(
        _r("A", (24.0, 15.0, 15.0), 18.5), _r("B", (13.78, 7.48, 17.52), 9.26)
    )
    assert relation.verdict == pkg.SPEC_BOX_VS_UNIT
    assert relation.box == "A"
    assert relation.unit == "B"
    assert relation.volume_ratio == pytest.approx(1805.9 / 5400.0, abs=1e-3)
    assert relation.weight_delta_lb == pytest.approx(9.24)


def test_same_edges_but_different_weight_is_a_contradiction():
    """三边一模一样、重量差 1.48 lb：同一外箱不可能一套含箱一套不含箱。

    咖啡机就是这个形态。它证明的不是「哪套错」，而是「必有一套错」——
    这时候说「取平均」或「取重的那个保守点」都没有依据。
    """
    relation = pkg.compare_packaging(
        _r("A", (16.0, 13.0, 9.5), 14.5), _r("B", (16.0, 13.0, 9.5), 15.98)
    )
    assert relation.verdict == pkg.SPEC_CONTRADICTION_SAME_BOX
    assert relation.box is None and relation.unit is None
    assert relation.weight_delta_lb == pytest.approx(1.48, abs=0.01)


def test_bigger_but_lighter_is_also_a_contradiction():
    """体积大反而轻：箱子比它装的机器还轻，物理上不成立。"""
    relation = pkg.compare_packaging(
        _r("箱?", (20.0, 20.0, 20.0), 10.0), _r("机?", (15.0, 15.0, 15.0), 12.0)
    )
    assert relation.verdict == pkg.SPEC_CONTRADICTION_WEIGHT
    assert relation.box is None
    assert relation.weight_delta_lb == pytest.approx(2.0)


def test_two_identical_readings_are_not_a_disagreement():
    """两套一模一样时不能说成「矛盾」——那是同一个数，写报告时要合并成一条。"""
    relation = pkg.compare_packaging(
        _r("A", (10.0, 8.0, 6.0), 3.0), _r("B", (10.0, 8.0, 6.0), 3.05)
    )
    assert relation.verdict == pkg.SPEC_INDISTINGUISHABLE


def test_tolerance_is_volume_ratio_not_per_edge():
    """容差按体积比，不按单边差：三条边各差 1% 时体积差 3%，已经超出 2% 容差。

    这一对是正常的一台机器（箱 20³ / 20 lb，机 19.8³ / 12 lb）。若容差按单边算，
    它会被并成「同一外箱」，然后在重量差 8 lb 上判成矛盾——把一台数据齐全的
    机器判成数据错误，这类假警报一旦出现过，真警报就没人信了。
    """
    relation = pkg.compare_packaging(
        _r("A", (20.0, 20.0, 20.0), 20.0), _r("B", (19.8, 19.8, 19.8), 12.0)
    )
    assert relation.verdict == pkg.SPEC_BOX_VS_UNIT
    assert relation.box == "A"
    assert relation.volume_ratio == pytest.approx(0.970, abs=1e-3)


def test_usable_as_box_refuses_to_pick_between_contradictions():
    """判不出时返回 None。挑「更保守」的那个会得到一个既不等于箱也不等于机的数。"""
    assert (
        pkg.usable_as_box(
            _r("A", (16.0, 13.0, 9.5), 14.5), _r("B", (16.0, 13.0, 9.5), 15.98)
        )
        is None
    )


def test_single_reading_is_returned_as_is():
    """只有一套读数时直接返回它——但调用方必须把它标成未核实（饮水机就是这样）。"""
    only = _r("B", (9.0, 8.0, 10.0), 2.8)
    assert pkg.usable_as_box(only) is only


def test_zero_or_negative_reading_is_rejected():
    """0 不能被当成「没数据」混过去：体积 0 会让体积重变 0，费用掉到最轻档。"""
    with pytest.raises(Exception):
        pkg.compare_packaging(_r("A", (0.0, 10.0, 10.0), 5.0), _r("B", (9.0, 9.0, 9.0), 4.0))


def test_reordered_but_identical_numbers_are_the_same_data_not_a_new_measurement():
    """11-23 13:42 用户报的「实测」：17.52×13.78×7.48 / 9.26 lb。

    与供应商规格书（13.78×7.48×17.52 / 9.26 lb）逐位相同，只是三边顺序换了。
    卷尺量不出 13.78 与 17.52 这种两位小数、电子秤也称不出 9.26 这种第二位——
    逐位相同只能说明这次「实测」是同一份数据的第二次出现。

    这一档必须单独存在：若判成 box_vs_unit（「B 是裸机」），读者会以为已经查清；
    若判成矛盾，读者会去改数。它真正要说的是「这次没有产生新信息」。
    """
    relation = pkg.compare_packaging(
        _r("实测", (17.52, 13.78, 7.48), 9.26),
        _r("规格书", (13.78, 7.48, 17.52), 9.26),
    )
    assert relation.verdict == pkg.SPEC_SAME_VALUES
    assert relation.box is None and relation.unit is None
    assert "同一份数据" in relation.reason


def test_same_values_is_detected_for_the_kettle_too():
    """水壶同样逐位相同（6.69×6.69×3.74 / 3.09 lb）——两台都中，不是巧合。"""
    relation = pkg.compare_packaging(
        _r("实测", (6.69, 6.69, 3.74), 3.09),
        _r("规格书", (6.69, 6.69, 3.74), 3.09),
    )
    assert relation.verdict == pkg.SPEC_SAME_VALUES


def test_third_decimal_breaks_the_same_values_verdict():
    """差 0.01 lb 就不算逐位相同——判据按两位小数卡死，否则「差不多」会把真读数也吞掉。

    这一对落在 box_vs_unit（箱 20³ / 20 lb，机 19.8³ / 12 lb），说明同源判定
    不会顺手把正常的一对也判成「同一份数据」。
    """
    relation = pkg.compare_packaging(
        _r("A", (20.0, 20.0, 20.0), 20.00),
        _r("B", (19.8, 19.8, 19.8), 12.00),
    )
    assert relation.verdict == pkg.SPEC_BOX_VS_UNIT


def test_usable_as_box_rejects_same_source_data():
    """同一份数据不能当「已量过」。判不出箱就返回 None，调用方必须去要新读数。"""
    assert (
        pkg.usable_as_box(
            _r("实测", (17.52, 13.78, 7.48), 9.26),
            _r("规格书", (13.78, 7.48, 17.52), 9.26),
        )
        is None
    )
