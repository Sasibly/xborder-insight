"""咖啡机首单（H16 第一台）：把 `docs/04` §15 的每个数字钉成门禁。

这些数已经在对话里出现过三次（$14.40 / $11.85 / $2.55 / 7.52% / 11.51%），
而它们全是「某天某次手算」的产物。手算会记错、会沿用旧口径、会在费率表
更新后悄悄失效。这里让它们要么复现，要么报错。
"""

from __future__ import annotations

from datetime import date

import pytest

from xborder_insight.db import fba as fba_db
from xborder_insight.domain import package as pkg
from xborder_insight.domain import profit as p

# 2026-11-21 落在 us_2026_peak（2026-10-15 至 2027-01-14）
SHIP_DATE = date(2026, 11, 21)
SELLING_PRICE = 63.99
QUOTED_PRICE = 18.50
REFERRAL = 0.15


def _fee(conn, dims, weight_oz, *, lithium=False, price=SELLING_PRICE):
    facts = pkg.PackageFacts(
        has_lithium_battery=lithium,
        unit_weight_oz=weight_oz,
        dims_in=dims,
    )
    klass, env = fba_db.envelope_for_package(
        conn, "US", facts,
        selling_price=price, ship_date=SHIP_DATE, exact_weight=True,
    )
    return klass, env


def _profit(fba_fee, *, price=SELLING_PRICE, freight=6.00, duty=0.25, returns=0.08):
    return p.compute(
        p.CostInputs(
            quoted_price=QUOTED_PRICE,
            selling_price=price,
            inbound_freight=freight,
            duty_rate=duty,
            referral_rate=REFERRAL,
            fba_fee=fba_fee,
            return_rate=returns,
            returns_handling_fee=3.00,
            returns_refund_admin_rate=0.20,
            returns_goods_loss_ratio=0.30,
            returns_inbound_loss_ratio=0.20,
            incoterm="FOB",
        )
    )


def test_repack_moves_the_sku_back_to_standard_tier(conn):
    """9.5 → 8.0 压的不是重量，是尺寸段：小号大件 → 大号标准件。

    两次的计费重量都是 232 oz（14.5 lb），所以「改包省 $2.55」不是「降一档续重」，
    而是换了一张费率表。归因错会导致改包方案被套用到不成立的 SKU 上。
    """
    before_class, before = _fee(conn, (16.0, 13.0, 9.5), 232.0)
    after_class, after = _fee(conn, (16.0, 13.0, 8.0), 232.0)

    assert before_class.size_tiers == ("small_bulky",)
    assert after_class.size_tiers == ("large_standard",)

    assert before.low_usd == pytest.approx(14.40, abs=0.005)
    assert after.low_usd == pytest.approx(11.85, abs=0.005)
    assert before.high_usd == pytest.approx(before.low_usd)  # exact_weight 下收成单点
    assert after.high_usd == pytest.approx(after.low_usd)
    assert before.low_usd - after.low_usd == pytest.approx(2.55, abs=0.005)

    # 计费重量不变，是这条结论的支撑证据，不是巧合
    assert before.billable_weight_oz == after.billable_weight_oz == pytest.approx(232.0)


def test_price_band_is_over_50_for_the_actual_price(conn):
    _, env = _fee(conn, (16.0, 13.0, 9.5), 232.0)
    assert env.price_band == "over_50"
    assert env.version_key == "us_2026_peak"


def test_same_sku_three_readings(conn):
    """同一件货：33.6% / 17.0% / 7.5%。中间不报错，所以每一档都要钉住。"""
    _, before = _fee(conn, (16.0, 13.0, 9.5), 232.0)
    _, after = _fee(conn, (16.0, 13.0, 8.0), 232.0)

    known = _profit(before.low_usd, freight=0.0, duty=0.0, returns=0.0)
    assert known.gross_margin == pytest.approx(0.33589, abs=5e-4)

    landed = _profit(before.low_usd, returns=0.0)
    assert landed.gross_margin == pytest.approx(0.16982, abs=5e-4)

    full = _profit(before.low_usd)
    assert full.gross_margin == pytest.approx(0.07523, abs=5e-4)

    # 改包后同一口径：+3.99 个点
    assert _profit(after.low_usd).gross_margin == pytest.approx(0.11510, abs=5e-4)


@pytest.mark.parametrize(
    ("duty", "margin"),
    [(0.00, 0.18742), (0.10, 0.15844), (0.25, 0.11510), (0.35, 0.08617)],
)
def test_duty_is_the_loudest_lever(conn, duty, margin):
    """关税 0→35% 掉 10 个点，比改包（+4pt）与换货代（约 +3pt）都大。

    所以 H8 不是「补一个参数」，它决定低价打法这条路是否存在。
    """
    _, after = _fee(conn, (16.0, 13.0, 8.0), 232.0)
    assert _profit(after.low_usd, duty=duty).gross_margin == pytest.approx(margin, abs=5e-4)


def _solve_price(target_margin: float, fba_fee: float) -> float:
    low, high = 20.0, 400.0
    for _ in range(80):
        mid = (low + high) / 2
        if _profit(fba_fee, price=mid).gross_margin < target_margin:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def test_15pct_floor_is_6761_after_repack(conn):
    """券后 $63.99 只有 11.5% 的 ACOS 上限，打不起广告；15% 线在 $67.61。"""
    _, before = _fee(conn, (16.0, 13.0, 9.5), 232.0)
    _, after = _fee(conn, (16.0, 13.0, 8.0), 232.0)

    assert _solve_price(0.15, before.low_usd) == pytest.approx(71.74, abs=0.05)
    assert _solve_price(0.15, after.low_usd) == pytest.approx(67.61, abs=0.05)
    assert _solve_price(0.20, after.low_usd) == pytest.approx(73.56, abs=0.05)


def _dg_gap(conn, size_tier: str, weight_min: float, price_band: str, version: str) -> float:
    rows = [
        row for row in fba_db.list_fee_rows(conn, "US")
        if row.version_key == version
        and row.size_tier == size_tier
        and row.weight_min_oz == weight_min
        and row.price_band == price_band
    ]
    by_class = {row.product_class: row.base_fee_usd for row in rows}
    return by_class["dangerous_goods"] - by_class["non_apparel"]


def test_lithium_gap_is_not_a_constant(conn):
    """$0.97 与 $1.16 都对，只是各对应一格。

    文档里曾把这个差写成「每件约 $1.16」，等于把一个具体档位的数当成通例。
    实测 201 个 DG 档的差额分布在 $0.46–$1.24，用常数兜必然在某些档上错。
    """
    small = _dg_gap(conn, "small_standard", 0.0, "10_to_50", "us_2026_off_peak")
    large = _dg_gap(conn, "large_standard", 8.0, "10_to_50", "us_2026_peak")
    bulky = _dg_gap(conn, "large_bulky", 0.0, "10_to_50", "us_2026_peak")

    assert small == pytest.approx(0.97, abs=0.005)
    assert large == pytest.approx(1.16, abs=0.005)
    assert bulky == pytest.approx(1.24, abs=0.005)
    assert len({small, large, bulky}) == 3, "三个档位的差价应当互不相同——这正是不能用常数的证据"
