"""包装事实 → 查表条件：含不含锂电池、多重、装不装得进 18×14×8。

这组用例守的是三条**会静默算错**的边界，每一条都对应一个具体的错法：

1. **含锂电池按普货算**。官方两套表同格差 18.8–25.9%，错在类别上不报错。
   所以 `has_lithium_battery` 没有默认值：漏填是 TypeError，不是「默认普货」。
2. **把实重当计费重量**。大号标准件取「实重与体积重较大者」。
   实重 2 lb、包装 18×14×8 的商品，体积重 14.5 lb，按 2 lb 报会少算 $5 上下。
   这条是上一轮的真实缺陷：`--size-tier large_standard --max-lb 2` 报 $7.67，
   实际 $12.78。用例 `test_billable_weight_never_falls_back_to_unit_weight` 钉它。
3. **三边判定的顺序**。18×8×14 这种写法不排序会把次长边拿去和最长边上限比，
   判成「通过」。用例 `test_edge_check_sorts_before_comparing` 钉它。
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from xborder_insight.db import fba as fba_db
from xborder_insight.domain import package as pkg
from xborder_insight.errors import InputInvalid, ParamUnverified
from xborder_insight.schemas import CostProfileInput

MARKET = "US"
PEAK_DAY = date(2026, 11, 21)
PRICE = 59.99  # 落在 >$50 价格带，命中危险品表里最贵的一档


# ---------------------------------------------------------------- 体积重


def test_dimensional_weight_uses_divisor_139():
    """12×10×6 = 720 in³ → 720/139 lb = 5.1799 lb → 82.87 oz。

    除数写错（166 / 5000）的方向是**低估**费用，所以这个数字要钉死。
    """
    assert pkg.dimensional_weight_oz((12, 10, 6)) == pytest.approx(720 / 139 * 16)


def test_max_dim_weight_under_standard_box_is_14_5_lb():
    """18×14×8 装满时的体积重就是大号标准件在标准尺寸内的体积重上界。"""
    assert pkg.dimensional_weight_oz((18, 14, 8)) == pytest.approx(232.0575, abs=1e-3)
    assert pkg.dimensional_weight_oz((18, 14, 8)) / 16 == pytest.approx(14.5036, abs=1e-4)


# ---------------------------------------------------------------- 三边判定


@pytest.mark.parametrize(
    "dims",
    [(18, 14, 8), (10, 8, 4), (8, 14, 18), (18, 8, 14), (17.9, 13.9, 7.9)],
)
def test_standard_box_within_all_three_edges(dims):
    assert pkg.fits_standard(dims) is True


@pytest.mark.parametrize(
    "dims, position, limit",
    [
        ((18.1, 14, 8), 1, 18),
        ((18, 14.1, 8), 2, 14),
        ((18, 14, 8.1), 3, 8),
        ((20, 14, 8), 1, 18),
    ],
)
def test_edge_violation_names_the_offending_edge(dims, position, limit):
    hit = pkg.standard_edge_violation(dims)
    assert hit is not None
    assert hit[0] == position and hit[2] == limit


def test_edge_check_sorts_before_comparing():
    """18×8×14 必须通过。不排序会把 14 拿去比 14、8 拿去比 18，也「碰巧」通过；
    但 20×8×14 不排序时，20 会和 18 比出局——排序后才对。
    真正被这条钉住的是 14×18×8：不排序会把 14 当最长边（≤18 通过）、
    18 当次长边（>14 出局），误判为超尺寸。
    """
    assert pkg.standard_edge_violation((14, 18, 8)) is None
    assert pkg.standard_edge_violation((8, 14, 18)) is None


# ---------------------------------------------------------------- 类别


def test_lithium_battery_routes_to_dangerous_goods():
    """"是否含锂离子/锂金属/锂聚合物电池" → 危险品表。两套表不可混用。"""
    assert pkg.classify(pkg.PackageFacts(True, 32.0, (12, 10, 6))).product_class == "dangerous_goods"


def test_no_battery_routes_to_non_apparel():
    assert pkg.classify(pkg.PackageFacts(False, 32.0, (12, 10, 6))).product_class == "non_apparel"


def test_battery_flag_has_no_default():
    """漏填必须是 TypeError：默认成 False 等于默认普货，差 $1/件且看不出来。"""
    with pytest.raises(TypeError):
        pkg.PackageFacts()  # type: ignore[call-arg]


# ---------------------------------------------------------------- 尺寸段与计费重量


def test_unit_weight_over_16oz_is_large_standard_only():
    result = pkg.classify(pkg.PackageFacts(True, 32.0, (12, 10, 6)))
    assert result.size_tiers == ("large_standard",)


def test_dims_over_small_standard_short_side_drops_small_tier():
    """实重 10 oz 但最短边 6 英寸 > 0.75 英寸的小号标准上限 → 只能是大号标准件。

    这条在 H13 关闭前是「两段都保留」：当时没有可引用的三边上限，
    宁可把区间放宽。拿到官方原文后（15×12×0.75），这一段可以直接排掉。
    """
    result = pkg.classify(pkg.PackageFacts(True, 10.0, (12, 10, 6)))
    assert result.size_tiers == ("large_standard",)


def test_dims_within_small_standard_keep_small_tier_only():
    """官方示例：13.8 × 9 × 0.7 英寸 / 2.88 oz 判为 Small standard。"""
    result = pkg.classify(pkg.PackageFacts(True, 2.88, (13.8, 9.0, 0.7)))
    assert result.size_tiers == ("small_standard",)


def test_small_standard_edge_limits_are_official_values():
    """三边上限必须就是官方表里的 15×12×0.75，别改成「差不多」的数。"""
    assert pkg.SMALL_STANDARD_MAX_DIMS_IN == (15.0, 12.0, 0.75)
    assert pkg.small_standard_edge_violation((15.0, 12.0, 0.75)) is None
    assert pkg.small_standard_edge_violation((15.1, 12.0, 0.75)) == (1, 15.1, 15.0)
    assert pkg.small_standard_edge_violation((15.0, 12.0, 0.76)) == (3, 0.76, 0.75)


def test_unit_weight_under_16oz_without_dims_keeps_both_tiers():
    """没给三边就判不了 15×12×0.75，两段都保留（方向安全，宁宽不窄）。"""
    result = pkg.classify(pkg.PackageFacts(True, 10.0, None, True))
    assert result.size_tiers == ("small_standard", "large_standard")
    assert any("0.75" in w for w in result.warnings)


def test_large_standard_billable_weight_is_max_of_unit_and_dim():
    result = pkg.classify(pkg.PackageFacts(True, 32.0, (12, 10, 6)))
    large = next(t for t in result.tiers if t.size_tier == "large_standard")
    assert large.max_oz == pytest.approx(82.87, abs=1e-2)
    assert large.max_oz > 32.0  # 体积重压过实重


def test_billable_weight_never_falls_back_to_unit_weight(conn):
    """上一轮的真实缺陷：只给「大号标准件 + ≤2 lb」时把 2 lb 当计费重量。

    实际包装若接近 18×14×8，体积重 232 oz，费用上界是 $12.78 而不是 $7.67。
    这条用例比较两条路径，确保包装事实路径**不会**比裸裁剪更便宜。
    """
    naive = fba_db.envelope(
        conn,
        MARKET,
        product_class="dangerous_goods",
        selling_price=PRICE,
        ship_date=PEAK_DAY,
        size_tiers=["large_standard"],
        weight_range_oz=(0.1, 32.0),
    )
    _, aware = fba_db.envelope_for_package(
        conn,
        MARKET,
        pkg.PackageFacts(True, 32.0, None, True),
        selling_price=PRICE,
        ship_date=PEAK_DAY,
    )
    assert aware.high_usd > naive.high_usd
    assert naive.high_usd == pytest.approx(7.67, abs=0.01)
    assert aware.high_usd == pytest.approx(12.78, abs=0.01)


def test_dimensions_narrow_the_envelope_compared_to_declaration_only(conn):
    """给了三边就该比只说「装得进」更窄——否则等于尺寸没被用上。"""
    _, declared = fba_db.envelope_for_package(
        conn,
        MARKET,
        pkg.PackageFacts(True, 32.0, None, True),
        selling_price=PRICE,
        ship_date=PEAK_DAY,
    )
    _, measured = fba_db.envelope_for_package(
        conn,
        MARKET,
        pkg.PackageFacts(True, 32.0, (12, 10, 6)),
        selling_price=PRICE,
        ship_date=PEAK_DAY,
    )
    assert measured.high_usd < declared.high_usd
    assert measured.low_usd >= declared.low_usd


# ---------------------------------------------------------------- 越界与缺失


def test_oversize_box_is_refused_not_downgraded():
    """三边任一条超出即不属于标准尺寸；大件费未录，必须报缺而不是按标准件算。"""
    with pytest.raises(ParamUnverified) as info:
        pkg.classify(pkg.PackageFacts(True, 32.0, (20, 14, 8)))
    assert "不属于标准尺寸" in info.value.message
    assert "大件" in (info.value.detail or "")


def test_heavy_item_is_refused():
    with pytest.raises(ParamUnverified):
        pkg.classify(pkg.PackageFacts(True, 21 * 16, (12, 10, 6)))


def test_declared_oversize_is_refused():
    with pytest.raises(ParamUnverified):
        pkg.classify(pkg.PackageFacts(True, 32.0, None, False))


def test_no_dims_and_no_declaration_is_rejected():
    """不知道三边、也没说装不装得进 → 拒。这是判断题，不是估算题。"""
    with pytest.raises(InputInvalid) as info:
        pkg.classify(pkg.PackageFacts(True, 32.0))
    assert "18×14×8" in info.value.message


def test_contradictory_declaration_is_rejected():
    with pytest.raises(InputInvalid):
        pkg.classify(pkg.PackageFacts(True, 32.0, (12, 10, 6), False))


def test_zero_dimension_is_rejected():
    """填 0 会让体积重变成 0，等于把费用算成最低档。"""
    with pytest.raises(InputInvalid):
        pkg.classify(pkg.PackageFacts(True, 32.0, (12, 0, 6)))


def test_zero_weight_is_rejected():
    with pytest.raises(InputInvalid):
        pkg.classify(pkg.PackageFacts(True, 0.0, (12, 10, 6)))


# ---------------------------------------------------------------- 与包络/入参的衔接


def test_envelope_reports_which_weight_it_capped_at(conn):
    """区间上界对应的是计费重量，不是那一行的 20 lb 上限。"""
    _, env = fba_db.envelope_for_package(
        conn,
        MARKET,
        pkg.PackageFacts(True, 32.0, (12, 10, 6)),
        selling_price=PRICE,
        ship_date=PEAK_DAY,
    )
    assert env.billable_weight_oz == pytest.approx(82.87, abs=1e-2)
    assert env.dim_weight_oz == pytest.approx(82.87, abs=1e-2)


def test_unknown_unit_weight_keeps_the_full_large_standard_range(conn):
    """实重也不知道时，大号标准件不能按体积重封顶——实重可能更大。"""
    _, env = fba_db.envelope_for_package(
        conn,
        MARKET,
        pkg.PackageFacts(True, None, (12, 10, 6)),
        selling_price=PRICE,
        ship_date=PEAK_DAY,
    )
    assert env.high_usd > 12.78


def test_cost_profile_accepts_package_facts_without_product_class():
    profile = CostProfileInput(
        quoted_price=18,
        selling_price=PRICE,
        referral_rate=0.15,
        has_lithium_battery=True,
        unit_weight_oz=32.0,
        package_dims_in=(12, 10, 6),
        ship_date="2026-11-21",
    )
    assert profile.uses_package_facts is True
    assert profile.product_class is None


def test_cost_profile_rejects_two_sources_of_truth():
    """既给包装事实又给 product_class：两处不一致时无法知道该信哪个。"""
    with pytest.raises(ValidationError) as info:
        CostProfileInput(
            quoted_price=18,
            selling_price=PRICE,
            referral_rate=0.15,
            has_lithium_battery=True,
            product_class="non_apparel",
            unit_weight_oz=32.0,
            package_dims_in=(12, 10, 6),
            ship_date="2026-11-21",
        )
    assert "两个" in str(info.value) or "不能同时" in str(info.value)


def test_cost_profile_rejects_package_facts_without_size_evidence():
    with pytest.raises(ValidationError):
        CostProfileInput(
            quoted_price=18,
            selling_price=PRICE,
            referral_rate=0.15,
            has_lithium_battery=True,
            ship_date="2026-11-21",
        )


# ---------------------------------------------------------------- 体积上界


def test_volume_ceiling_bounds_the_dim_weight():
    """只知道「不超过 400 in³」时，体积重上界就是 400/139×16 = 46.0 oz。"""
    result = pkg.classify(pkg.PackageFacts(True, 32.0, None, True, 400))
    assert result.dim_weight_oz == pytest.approx(400 / 139 * 16)


def test_volume_ceiling_is_what_actually_narrows_the_envelope(conn):
    """「装得进 18×14×8」是二元判断，不约束体积重；体积上界才约束。

    这条用例的意义：上一版把「装得进」当成收窄条件写进文档，实际它一点都不窄
    （体积重仍按 14.5 lb 算）。加体积上界后费用上界才真的降下来。
    """
    _, declared = fba_db.envelope_for_package(
        conn,
        MARKET,
        pkg.PackageFacts(True, 32.0, None, True),
        selling_price=PRICE,
        ship_date=PEAK_DAY,
    )
    _, bounded = fba_db.envelope_for_package(
        conn,
        MARKET,
        pkg.PackageFacts(True, 32.0, None, True, 400),
        selling_price=PRICE,
        ship_date=PEAK_DAY,
    )
    assert bounded.high_usd < declared.high_usd
    assert bounded.low_usd == pytest.approx(declared.low_usd)


def test_volume_smaller_than_unit_weight_does_not_lower_the_floor(conn):
    """体积上界小于实重时，计费重量仍是实重，不能被体积重覆盖成更小的值。"""
    _, env = fba_db.envelope_for_package(
        conn,
        MARKET,
        pkg.PackageFacts(True, 32.0, None, True, 200),
        selling_price=PRICE,
        ship_date=PEAK_DAY,
    )
    assert env.billable_weight_oz == pytest.approx(32.0, abs=0.5)


def test_volume_ceiling_contradicting_dims_is_rejected():
    with pytest.raises(InputInvalid):
        pkg.classify(pkg.PackageFacts(True, 32.0, (12, 10, 6), None, 100))


def test_zero_volume_is_rejected():
    with pytest.raises(InputInvalid):
        pkg.classify(pkg.PackageFacts(True, 32.0, None, True, 0.0))


def test_explicit_fee_and_package_facts_cannot_coexist():
    """两条路都能给出费用时，同时给等于两套费用，谁赢都不对。"""
    with pytest.raises(ValidationError):
        CostProfileInput(
            quoted_price=18,
            selling_price=PRICE,
            referral_rate=0.15,
            fba_fee=6.5,
            has_lithium_battery=True,
            unit_weight_oz=32.0,
            package_dims_in=(12, 10, 6),
            ship_date="2026-11-21",
        )
