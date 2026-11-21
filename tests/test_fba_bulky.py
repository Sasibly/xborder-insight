"""大件（small_bulky / large_bulky）费率：数值、结构、与包装层的衔接。

这组用例的存在理由：大件不是「标准尺寸的放大版」，它在三处与标准尺寸不同，
每一处都足以让费用算错一个档而界面上看不出来：

1. **计费结构**：base 含首 1 lb，其后每满 1 lb 加 $0.38（标准尺寸是一格一个价）。
2. **体积重的宽高下限**：bulky / oversize 的体积重把宽、高各按至少 2 英寸算。
3. **档位判定同时看重量与「长 + 围」**：59×33×33 三边都没超上限，长+围 191
   却远超 130，只看三边会把它错判成大件。

`OFFICIAL_EXAMPLE` 是官方页自带的算例（baby cot，7.90 lb，>$50，Small bulky，
2026 非旺季 $10.21）。它同时钉住第 1 条的两个细节：base 含首 1 lb、不足 1 lb
向上取整——所以拿它当锚点，而不是拿我们自己算出来的数当锚点。
"""

from __future__ import annotations

from datetime import date

import pytest

from xborder_insight.db import fba as fba_db
from xborder_insight.db.migrations import fba_us_bulky_rates as seeds
from xborder_insight.domain import fba as dom

MARKET = "US"
OFF_PEAK_DAY = date(2026, 5, 1)   # us_2026_off_peak
PEAK_DAY = date(2026, 11, 21)     # us_2026_peak
FUTURE_2027 = date(2027, 5, 1)    # us_2027_off_peak：bulky 未录


def _quote(conn, *, tier, weight_lb, day, product_class="non_apparel", price=59.99):
    return fba_db.resolve(
        conn,
        MARKET,
        size_tier=tier,
        product_class=product_class,
        selling_price=price,
        ship_date=day,
        weight_lb=weight_lb,
    )


def test_official_baby_cot_example_reproduces_10_21(conn):
    """官方算例：7.90 lb、>$50、Small bulky、2026 非旺季 → $10.21（未含附加费）。"""
    quote = _quote(
        conn,
        tier="small_bulky",
        weight_lb=seeds.OFFICIAL_EXAMPLE["lb"],
        day=OFF_PEAK_DAY,
        price=99.0,
    )
    assert quote.price_band == seeds.OFFICIAL_EXAMPLE["price_band"]
    # $7.55 含首 1 lb + 7 个 1 lb 区间 × $0.38 = $10.21
    assert quote.subtotal_usd == pytest.approx(seeds.OFFICIAL_EXAMPLE["fee_usd"], abs=0.005)
    assert quote.extra_weight_fee_usd == pytest.approx(7 * 0.38, abs=1e-9)


def test_base_fee_covers_first_pound_only(conn):
    """恰好 1 lb 时不该产生任何超重增量；1.1 lb 就要多算一格。"""
    at_one = _quote(conn, tier="small_bulky", weight_lb=1.0, day=OFF_PEAK_DAY)
    assert at_one.extra_weight_fee_usd == pytest.approx(0.0)
    over = _quote(conn, tier="small_bulky", weight_lb=1.1, day=OFF_PEAK_DAY)
    assert over.extra_weight_fee_usd == pytest.approx(0.38)


def test_dangerous_goods_sits_above_non_apparel_by_a_fixed_delta(conn):
    """危险品与普货的差额在同一期内是常数（$0.72 非旺季 / $1.24 旺季）。

    这条是抓取自洽性的守门员：抄错任何一格，差值就不再是常数。
    """
    for day, delta in ((OFF_PEAK_DAY, 0.72), (PEAK_DAY, 1.24)):
        for tier in ("small_bulky", "large_bulky"):
            plain = _quote(conn, tier=tier, weight_lb=5.0, day=day)
            hazmat = _quote(conn, tier=tier, weight_lb=5.0, day=day,
                            product_class="dangerous_goods")
            assert hazmat.subtotal_usd - plain.subtotal_usd == pytest.approx(delta, abs=1e-9)


def test_peak_sits_above_off_peak_by_a_fixed_delta(conn):
    """同一档位，旺季比非旺季固定贵 $1.04（普货）/ $1.56（危险品）。"""
    for product_class, delta in (("non_apparel", 1.04), ("dangerous_goods", 1.56)):
        for tier in ("small_bulky", "large_bulky"):
            off = _quote(conn, tier=tier, weight_lb=5.0, day=OFF_PEAK_DAY,
                         product_class=product_class)
            peak = _quote(conn, tier=tier, weight_lb=5.0, day=PEAK_DAY,
                          product_class=product_class)
            assert peak.subtotal_usd - off.subtotal_usd == pytest.approx(delta, abs=1e-9)


def test_2027_bulky_is_missing_not_extrapolated(conn):
    """2027 非旺季的 bulky 被官方重构为十档，本期没有对应格子 → 必须报缺。"""
    from xborder_insight.errors import ParamUnverified

    with pytest.raises(ParamUnverified):
        _quote(conn, tier="small_bulky", weight_lb=5.0, day=FUTURE_2027)


def test_bulky_rows_carry_included_weight_and_increment(conn):
    """base 含首 1 lb、每 1 lb 一格：这两列缺任一个都会把续重算成 0。"""
    rows = [
        row for row in fba_db.list_fee_rows(conn, MARKET)
        if row.size_tier in ("small_bulky", "large_bulky")
    ]
    assert len(rows) == 24  # 2 期 × 2 类别 × 2 段 × 3 价格带
    for row in rows:
        assert row.included_oz == pytest.approx(16.0)
        assert row.increment_oz == pytest.approx(16.0)
        assert row.per_increment_usd == pytest.approx(0.38)
        assert row.weight_max_oz == pytest.approx(800.0)
        assert row.verified and row.source_url


def test_desk_purifier_gets_a_quote_from_its_box(conn):
    """15.4×10.2×10.2 / 141.1 oz：这条链路上一轮是 503，现在必须给出单一尺寸段。"""
    from xborder_insight.domain import package as pkg

    facts = pkg.PackageFacts(False, 141.1, (15.4, 10.2, 10.2))
    klass = pkg.classify(facts)
    assert klass.size_tiers == ("small_bulky",)

    _, envelope = fba_db.envelope_for_package(
        conn, MARKET, facts, selling_price=59.99, ship_date=PEAK_DAY,
    )
    assert envelope.size_tiers == ("small_bulky",)
    assert envelope.low_usd < envelope.high_usd
    # 计费重量由体积重决定（141.1 oz 实重 < 184.4 oz 体积重）
    assert envelope.high_row is not None
    assert envelope.high_usd > 10.0


def test_fuel_surcharge_applies_to_bulky_too(conn):
    """3.5% 附加费作用于配送费，与尺寸段无关；大件漏计等于成本低 3.5%。"""
    quote = _quote(conn, tier="large_bulky", weight_lb=10.0, day=PEAK_DAY, price=99.0)
    assert quote.fuel_surcharge_usd == pytest.approx(quote.subtotal_usd * 0.035, abs=1e-6)
    assert quote.total_fee_usd == pytest.approx(
        quote.subtotal_usd + quote.fuel_surcharge_usd, abs=1e-6
    )


def test_billable_weight_reports_the_larger_of_unit_and_dim(conn):
    """24×15×15 / 18.5 lb：体积重 38.85 lb 才是计费重量。

    这条钉的是一个显示缺陷：早先按 "large_standard" 段取计费上界，
    大件包里没有这一段，于是 fallback 到实重，输出「计费重量 18.5 lb；
    其中体积重 38.85 lb」——自相矛盾，且恰好发生在最需要看对的大件档。
    """
    from xborder_insight.domain import package as pkg

    facts = pkg.PackageFacts(False, 18.5 * 16, (24.0, 15.0, 15.0))
    _, envelope = fba_db.envelope_for_package(
        conn, MARKET, facts, selling_price=59.99, ship_date=PEAK_DAY,
    )
    assert envelope.dim_weight_oz is not None
    assert envelope.billable_weight_oz == pytest.approx(envelope.dim_weight_oz)
    assert envelope.billable_weight_oz > facts.unit_weight_oz


def test_exact_weight_collapses_the_envelope_to_a_single_number(conn):
    """实测实重 + 实测三边 → 单点；上界仍按「不足 1 lb 向上取整」计费。"""
    from xborder_insight.domain import package as pkg

    facts = pkg.PackageFacts(False, 18.5 * 16, (24.0, 15.0, 15.0))
    loose, env_loose = fba_db.envelope_for_package(
        conn, MARKET, facts, selling_price=59.99, ship_date=PEAK_DAY,
    )
    _, env_exact = fba_db.envelope_for_package(
        conn, MARKET, facts, selling_price=59.99, ship_date=PEAK_DAY, exact_weight=True,
    )
    # 默认口径：实重被当作上界，区间下端仍从最轻档起算（1 lb 的 base 费）
    assert env_loose.low_usd < env_loose.high_usd
    assert env_loose.low_row is not None
    assert env_loose.low_usd == pytest.approx(
        env_loose.low_row.base_fee_usd * (1 + 0.035), abs=0.01
    )
    # 实测口径：收成单点，且上界一分不差
    assert env_exact.low_usd == pytest.approx(env_exact.high_usd)
    assert env_exact.high_usd == pytest.approx(env_loose.high_usd)
    assert env_exact.spread_usd == pytest.approx(0.0)
    assert loose.size_tiers == env_exact.size_tiers


def test_weight_ranges_by_tier_exact_uses_billable_on_both_ends(conn):
    """exact=True 时每段上下端都取计费上界；exact=False 时下端敞着。"""
    from xborder_insight.domain import package as pkg

    klass = pkg.classify(pkg.PackageFacts(False, 4.8 * 16, (10.5, 9.2, 8.4)))
    loose = klass.weight_ranges_by_tier()
    tight = klass.weight_ranges_by_tier(exact=True)
    for tier, (lo, hi) in loose.items():
        assert lo == pytest.approx(pkg.MIN_WEIGHT_OZ)
        tlo, thi = tight[tier]
        assert tlo == pytest.approx(hi)
        assert thi == pytest.approx(hi)
