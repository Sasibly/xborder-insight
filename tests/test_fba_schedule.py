"""FBA 四维费率维表的行为测试。

这组用例的重点不是「能不能算出个数」，而是**错的那条路是否被堵住**：

* 缺口不许外推——large_standard 的 16–48 oz 没录，就必须报缺，不能借用相邻档；
* draft 不许当值用——入库了但没核实，与没入库要报不同的话；
* 边界不许靠浮点运气——$10 / $50 / 12 oz / 10-15 这些点各钉一条；
* audit 必须能自己发现表坏了——人为制造重叠与缝隙，看它报不报。

如果这些用例全绿而线上仍算错，那说明错在「表里的数是错的」，
而不是「表的结构骗了人」——这是本项目能接受的错误边界。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from xborder_insight.db import fba as fba_db
from xborder_insight.domain import fba as dom
from xborder_insight.errors import InputInvalid, ParamUnverified

MARKET = "US"
SRC = "https://sellercentral.amazon.com/help/hub/reference/external/GABBX6GZPA8MSZGW"
OFF_PEAK_DAY = date(2026, 6, 1)   # 非旺季
PEAK_DAY = date(2026, 11, 21)     # 旺季


def _verify(conn, *, weight_min_oz, price_band="10_to_50", season="off_peak",
            size_tier="large_standard", base=None):
    fba_db.mark_verified(
        conn,
        marketplace=MARKET,
        size_tier=size_tier,
        weight_min_oz=weight_min_oz,
        price_band=price_band,
        season=season,
        source_url=SRC,
        effective_date="2026-01-15",
        verified_at="2026-11-21",
        base_fee_usd=base,
    )


def _insert(conn, **over):
    """直插一行，用于构造「表坏了」的场景。默认值是一条合法的 off_peak 行。"""
    values = {
        "marketplace": MARKET,
        "size_tier": "large_standard",
        "weight_min_oz": 100.0,
        "weight_max_oz": 110.0,
        "price_band": "10_to_50",
        "season": "off_peak",
        "base_fee_usd": 5.0,
        "per_increment_usd": 0.0,
        "increment_oz": None,
        "included_oz": None,
        "source_url": None,
        "effective_date": None,
        "verified_at": None,
        "status": "draft",
        "note": None,
        "created_at": "2026-11-21T00:00:00+08:00",
    }
    values.update(over)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO fba_fee_schedule ({cols}) VALUES ({marks})", tuple(values.values())
    )


# ---------------------------------------------------------------- 迁移层


def test_seed_is_all_draft(conn):
    """锚点值只搬结构、未逐档复核，因此一行都不许是 verified。"""
    rows = fba_db.list_fee_rows(conn, MARKET)
    assert len(rows) == 12
    assert all(not row.verified for row in rows)


def test_seed_covers_only_large_standard_off_peak(conn):
    rows = fba_db.list_fee_rows(conn, MARKET)
    assert {r.size_tier for r in rows} == {"large_standard"}
    assert {r.season for r in rows} == {"off_peak"}
    assert {r.price_band for r in rows} == {"under_10", "10_to_50", "over_50"}


def test_seed_records_the_known_gaps(conn):
    """4–8 oz、16–48 oz、320 oz 以上是「故意留空」，必须能被审计看见。"""
    audit = fba_db.audit(conn, MARKET)
    assert audit.gap_count == 9  # 3 个价格带 × 3 个缺口
    assert audit.verified_rows == 0
    assert audit.draft_rows == 12
    first = audit.groups[0]
    assert (4.0, 8.0) in first.gaps
    assert (16.0, 48.0) in first.gaps
    assert (320.0, None) in first.gaps
    assert first.overlaps == ()


def test_fuel_surcharge_registered_but_draft(conn):
    rows = conn.execute(
        "SELECT status, value FROM cost_params WHERE param_key = ?",
        (fba_db.FUEL_SURCHARGE_KEY,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "draft"
    assert rows[0]["value"] is None


# ---------------------------------------------------------------- 表约束


def test_verified_row_requires_source(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, status="verified")


def test_weight_range_must_be_increasing(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, weight_min_oz=20.0, weight_max_oz=20.0)


def test_increment_price_requires_increment_size(conn):
    """有「每 4 oz 加 $0.08」却不说每多少 oz，计价函数无法收敛。"""
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, per_increment_usd=0.08, increment_oz=None)


def test_increment_size_without_price_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, per_increment_usd=0.0, increment_oz=4.0)


def test_unknown_size_tier_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, size_tier="huge")


def test_duplicate_dimension_start_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, weight_min_oz=0.0)  # 与种子的 [0,4] 起点冲突


# ---------------------------------------------------------------- 维度归一


@pytest.mark.parametrize(
    ("price", "expected"),
    [(0.01, "under_10"), (9.99, "under_10"), (10.0, "10_to_50"),
     (29.99, "10_to_50"), (50.0, "10_to_50"), (50.01, "over_50")],
)
def test_price_band_boundaries(price, expected):
    """$10 与 $50 归中带，$50.01 归高带——差一分钱就跳档，必须钉住。"""
    assert dom.price_band_of(price) == expected


def test_price_band_rejects_non_positive():
    with pytest.raises(InputInvalid):
        dom.price_band_of(0)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 1, 14), "peak"),      # 旺季最后一天
        (date(2026, 1, 15), "off_peak"),  # 非旺季第一天
        (date(2026, 10, 14), "off_peak"),  # 非旺季最后一天
        (date(2026, 10, 15), "peak"),     # 旺季第一天
        (date(2026, 7, 1), "off_peak"),
    ],
)
def test_season_boundaries_cross_year(day, expected):
    """旺季跨年，写成单段比较就会把 1 月判成非旺季。"""
    assert dom.season_of(day) == expected


def test_weight_requires_exactly_one_unit():
    with pytest.raises(InputInvalid):
        dom.normalize_weight_oz()
    with pytest.raises(InputInvalid):
        dom.normalize_weight_oz(oz=4, lb=1)


def test_weight_lb_converts_to_oz():
    assert dom.normalize_weight_oz(lb=1) == pytest.approx(16.0)
    assert dom.normalize_weight_oz(oz=8) == pytest.approx(8.0)


def test_weight_rejects_zero():
    with pytest.raises(InputInvalid):
        dom.normalize_weight_oz(oz=0)


# ---------------------------------------------------------------- 匹配规则


def test_shared_endpoint_goes_to_finer_band(conn):
    """12 oz 同时属于 [8,12] 与 [12,16]，应命中起点更大的后者。"""
    _verify(conn, weight_min_oz=8.0, base=4.20)
    _verify(conn, weight_min_oz=12.0, base=4.60)
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", selling_price=29.99,
        ship_date=OFF_PEAK_DAY, weight_oz=12,
    )
    assert quote.base_fee_usd == pytest.approx(4.60)
    assert quote.row.weight_min_oz == 12.0


def test_missing_band_is_not_extrapolated(conn):
    """16–48 oz 是洞：必须报「缺这一档」，不能拿 [12,16] 或 [48,320] 顶上。"""
    for start in (0.0, 8.0, 12.0, 48.0):
        _verify(conn, weight_min_oz=start)
    with pytest.raises(ParamUnverified) as exc:
        fba_db.resolve(
            conn, MARKET, size_tier="large_standard", selling_price=29.99,
            ship_date=OFF_PEAK_DAY, weight_oz=30,
        )
    assert "缺少该组合" in exc.value.message
    assert "30 oz" in exc.value.message


def test_missing_size_tier_does_not_fall_back(conn):
    """small_standard 一行都没录，绝不能退到 large_standard 的费率。"""
    _verify(conn, weight_min_oz=0.0, base=3.73)
    with pytest.raises(ParamUnverified):
        fba_db.resolve(
            conn, MARKET, size_tier="small_standard", selling_price=29.99,
            ship_date=OFF_PEAK_DAY, weight_oz=3,
        )


def test_unknown_size_tier_is_input_error(conn):
    with pytest.raises(InputInvalid):
        fba_db.resolve(
            conn, MARKET, size_tier="huge", selling_price=29.99,
            ship_date=OFF_PEAK_DAY, weight_oz=3,
        )


# ---------------------------------------------------------------- 增量计费


def test_increment_starts_after_included_weight(conn):
    """首 3 lb（48 oz）收 base，48 oz 本身不加价。"""
    _verify(conn, weight_min_oz=48.0, base=6.97)
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", selling_price=29.99,
        ship_date=OFF_PEAK_DAY, weight_oz=48,
    )
    assert quote.base_fee_usd == pytest.approx(6.97)
    assert quote.extra_weight_fee_usd == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("weight_oz", "extra"),
    [(52.0, 0.08), (56.0, 0.16), (49.0, 0.08), (320.0, 5.44)],
)
def test_increment_rounds_up_per_step(conn, weight_oz, extra):
    """不足一格按一格：49 oz 已超 48，收一格；320 oz 收 68 格。"""
    _verify(conn, weight_min_oz=48.0, base=6.97)
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", selling_price=29.99,
        ship_date=OFF_PEAK_DAY, weight_oz=weight_oz,
    )
    assert quote.extra_weight_fee_usd == pytest.approx(extra)


# ---------------------------------------------------------------- 核实状态


def test_draft_row_is_rejected_with_its_own_wording(conn):
    """有行但 draft：报「未核实」，与「缺这一档」必须是两句不同的话。"""
    with pytest.raises(ParamUnverified) as exc:
        fba_db.resolve(
            conn, MARKET, size_tier="large_standard", selling_price=29.99,
            ship_date=OFF_PEAK_DAY, weight_oz=3,
        )
    assert "未核实" in exc.value.message
    assert "缺少该组合" not in exc.value.message


def test_mark_verified_on_absent_row_is_rejected(conn):
    with pytest.raises(ParamUnverified):
        _verify(conn, weight_min_oz=999.0)


# ---------------------------------------------------------------- 燃油附加费


def test_fuel_surcharge_not_applied_when_unverified(conn):
    _verify(conn, weight_min_oz=0.0, base=3.73)
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", selling_price=29.99,
        ship_date=OFF_PEAK_DAY, weight_oz=3,
    )
    assert quote.fuel_surcharge_rate == 0.0
    assert quote.total_fee_usd == pytest.approx(3.73)
    assert any("未施加" in line for line in quote.assumptions)
    # 未施加的原因也要写进去：未登记与登记未核实是两种补数据动作
    assert any("draft" in line for line in quote.assumptions)


def test_fuel_surcharge_multiplies_subtotal(conn):
    """附加费按配送费计提，不按售价：$3.73 × 3.5% = $0.13055。"""
    _verify(conn, weight_min_oz=0.0, base=3.73)
    from xborder_insight.db import params

    params.mark_verified(
        conn, fba_db.FUEL_SURCHARGE_KEY, MARKET,
        value=0.035,
        source_url="https://sellercentral.amazon.com/help/hub/reference/external/GABBX6GZPA8MSZGW",
        effective_date="2026-04-17",
        verified_at="2026-11-21",
    )
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", selling_price=29.99,
        ship_date=OFF_PEAK_DAY, weight_oz=3,
    )
    assert quote.fuel_surcharge_rate == pytest.approx(0.035)
    assert quote.fuel_surcharge_usd == pytest.approx(3.73 * 0.035)
    assert quote.total_fee_usd == pytest.approx(3.73 * 1.035)


# ---------------------------------------------------------------- 审计


def test_audit_detects_overlap(conn):
    """[8,12] 之间塞一行 [10,20]，起点不同所以过得去 INSERT，但必须被审计抓出。"""
    _insert(conn, weight_min_oz=10.0, weight_max_oz=20.0)
    audit = fba_db.audit(conn, MARKET)
    assert audit.overlap_count >= 1


def test_audit_does_not_flag_shared_endpoints(conn):
    """种子里 [8,12] 与 [12,16] 共享端点，这是官方写法，不算重叠。"""
    assert fba_db.audit(conn, MARKET).overlap_count == 0


def test_audit_marks_group_complete_only_when_no_gap_no_draft(conn):
    """把一组清空后补一条覆盖到 ∞ 的 verified 行，该组才应判为完整。"""
    conn.execute("DELETE FROM fba_fee_schedule WHERE price_band = 'over_50'")
    _insert(
        conn, weight_min_oz=0.0, weight_max_oz=None, price_band="over_50",
        status="verified", source_url=SRC, effective_date="2026-01-15",
        verified_at="2026-11-21",
    )
    audit = fba_db.audit(conn, MARKET)
    target = [g for g in audit.groups if g.price_band == "over_50"][0]
    assert target.gaps == ()
    assert target.draft == 0
    assert target.complete is True


def test_price_at_band_boundary_raises_warning(conn):
    """$10.00 是边界值：$9.99 就是另一档费率，必须让调用方看见。"""
    _verify(conn, weight_min_oz=0.0, base=3.73)
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", selling_price=10.0,
        ship_date=OFF_PEAK_DAY, weight_oz=3,
    )
    assert any("价格带分界" in line for line in quote.warnings)


def test_ordinary_price_has_no_band_warning(conn):
    _verify(conn, weight_min_oz=0.0, base=3.73)
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", selling_price=29.99,
        ship_date=OFF_PEAK_DAY, weight_oz=3,
    )
    assert quote.warnings == ()


def test_quote_serializes_surcharge_and_matched_range(conn):
    _verify(conn, weight_min_oz=8.0, base=4.20)
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", selling_price=29.99,
        ship_date=OFF_PEAK_DAY, weight_oz=9,
    )
    payload = quote.as_dict()
    assert payload["matched_range"] == "8–12 oz"
    assert payload["price_band"] == "10_to_50"
    assert payload["season"] == "off_peak"
    assert payload["total_fee_usd"] == pytest.approx(4.20)
