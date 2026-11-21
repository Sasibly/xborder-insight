"""FBA 费率维表的行为测试。

这组用例的重点不是「能不能算出个数」，而是**错的那条路是否被堵住**：

* 缺口不许外推——既不能跨重量档、跨尺寸段，也不能跨费率期、跨产品类别；
* 版本不许借——2027 旺季官方未发布，就必须报缺，不能命中 2026 旺季；
* draft 不许当值用——入库了但没核实，与没入库要报不同的话；
* 边界不许靠浮点运气——$10 / $50 / 12 oz / 10-15 / 1-14 这些点各钉一条；
* audit 必须能自己发现表坏了——人为制造重叠与缝隙，看它报不报；
* 官方数值本身要么自洽、要么被测出来——单调性、跨期、类别三组不变式。

最后一条是这轮新增的：表填满之后，「结构对了但数字抄错」成了主要风险。
两个方向都覆盖：结构由行为用例守，数字由不变式守。
如果全绿而线上仍算错，那说明错在「不变式管不到的那一格」，
而不是「表的结构骗了人」——这是本项目能接受的错误边界。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from xborder_insight.db import fba as fba_db
from xborder_insight.domain import fba as dom
from xborder_insight.errors import InputInvalid, ParamUnverified

MARKET = "US"
SRC = "https://sellercentral.amazon.com/help/hub/reference/external/GABBX6GZPA8MSZGW"
OFF_PEAK_DAY = date(2026, 6, 1)     # 2026 非旺季
PEAK_DAY = date(2026, 11, 21)       # 2026 旺季
NEXT_OFF_PEAK_DAY = date(2027, 6, 1)  # 2027 非旺季
CREATED_AT = "2026-11-21T00:00:00+08:00"


@pytest.fixture
def bare(conn):
    """清掉全部费率行，只留版本表。

    行为用例（缺口、重叠、匹配规则）必须自己造场景：官方 378 行落库后，
    「16–48 oz 是洞」这类前提已经不成立，靠真实数据写断言只会写出
    「今天恰好如此」的测试。
    """
    conn.execute("DELETE FROM fba_fee_schedule")
    return conn


def _insert(conn, **over):
    """直插一行，默认值是一条合法的 draft 行（2026 非旺季 / 非服装 / 大号标准件）。"""
    values = {
        "version_key": "us_2026_off_peak",
        "marketplace": MARKET,
        "size_tier": "large_standard",
        "product_class": "non_apparel",
        "weight_min_oz": 100.0,
        "weight_max_oz": 110.0,
        "price_band": "10_to_50",
        "base_fee_usd": 5.0,
        "per_increment_usd": 0.0,
        "increment_oz": None,
        "included_oz": None,
        "source_url": None,
        "verified_at": None,
        "status": "draft",
        "note": None,
        "created_at": CREATED_AT,
    }
    values.update(over)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO fba_fee_schedule ({cols}) VALUES ({marks})", tuple(values.values())
    )


def _verify(conn, *, weight_min_oz, weight_max_oz=110.0, price_band="10_to_50",
            version_key="us_2026_off_peak", size_tier="large_standard",
            product_class="non_apparel", base=None):
    _insert(
        conn,
        weight_min_oz=weight_min_oz,
        weight_max_oz=weight_max_oz,
        price_band=price_band,
        version_key=version_key,
        size_tier=size_tier,
        product_class=product_class,
    )
    fba_db.mark_verified(
        conn,
        marketplace=MARKET,
        version_key=version_key,
        size_tier=size_tier,
        product_class=product_class,
        weight_min_oz=weight_min_oz,
        price_band=price_band,
        source_url=SRC,
        verified_at="2026-11-21",
        base_fee_usd=base,
    )


# ---------------------------------------------------------------- 迁移与数据


def test_three_rate_versions_are_registered(conn):
    """三期：2026 非旺季、2026 旺季、2027 非旺季。2027 旺季官方未发布，不许登记。"""
    versions = fba_db.list_rate_versions(conn, MARKET)
    assert [v.version_key for v in versions] == [
        "us_2026_off_peak", "us_2026_peak", "us_2027_off_peak",
    ]
    assert all(v.verified for v in versions)
    assert all(v.source_url for v in versions)


def test_version_ranges_are_contiguous_and_non_overlapping(conn):
    """相邻两期必须首尾相接：差一天就是「某天报价不了」，重叠一天就是口径不唯一。"""
    versions = fba_db.list_rate_versions(conn, MARKET)
    for earlier, later in zip(versions, versions[1:]):
        assert later.effective_from == earlier.effective_to + timedelta(days=1)
    assert dom.version_overlaps(versions) == ()


def test_official_rows_are_all_verified_with_source(conn):
    """378 行（3 期 × 2 类别 × 2 尺寸段 × 档位 × 3 价格带），一行都不许是 draft。"""
    rows = fba_db.list_fee_rows(conn, MARKET)
    assert len(rows) == 378
    assert all(row.verified for row in rows)
    assert all(row.source_url == SRC for row in rows)


def test_official_rows_cover_only_non_apparel_and_dangerous_goods(conn):
    """服装与大件/超大件整段未录：这是缺口，不是「没有这类商品」。"""
    rows = fba_db.list_fee_rows(conn, MARKET)
    assert {r.product_class for r in rows} == {"non_apparel", "dangerous_goods"}
    assert {r.size_tier for r in rows} == {"small_standard", "large_standard"}


def test_official_table_has_no_gap_within_tier_bounds(conn):
    """尺寸段的有效重量范围内没有断档，也没有重叠。"""
    audit = fba_db.audit(conn, MARKET)
    assert audit.gap_count == 0
    assert audit.overlap_count == 0
    assert audit.version_overlaps == ()
    assert audit.banded_dimensions == 36  # 3 期 × 2 类别 × 2 尺寸段 × 3 价格带


def test_fuel_surcharge_is_verified(conn):
    rows = conn.execute(
        "SELECT status, value, effective_date FROM cost_params WHERE param_key = ?",
        (fba_db.FUEL_SURCHARGE_KEY,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "verified"
    assert rows[0]["value"] == pytest.approx(0.035)
    assert rows[0]["effective_date"] == "2026-04-17"


def test_2027_peak_is_not_registered(conn):
    """官方未发布 2027 旺季：版本表里不该出现，查询也必须报缺。"""
    versions = fba_db.list_rate_versions(conn, MARKET)
    assert dom.pick_version(versions, date(2027, 12, 1)) is None


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


def test_unknown_product_class_is_rejected(conn):
    """写错类别（如 'apparel_standard'）必须进不了库，否则查表时会静默查不到。"""
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, product_class="fragile")


def test_unknown_version_is_rejected_by_foreign_key(conn):
    """行不能挂在不存在的版本上：那会让「哪一期」这个问题无解。"""
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, version_key="us_2099_peak")


def test_duplicate_dimension_start_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, weight_min_oz=0.0)  # 同一版本/类别/价格带下已有 0 起点


def test_same_weight_start_in_another_class_is_allowed(bare):
    """类别在唯一键里：同一个重量起点、两个类别必须能共存。"""
    _insert(bare, weight_min_oz=200.0, weight_max_oz=210.0, product_class="non_apparel")
    _insert(bare, weight_min_oz=200.0, weight_max_oz=210.0, product_class="dangerous_goods")
    assert len(fba_db.list_fee_rows(bare, MARKET)) == 2


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


# ---------------------------------------------------------------- 版本解析


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 1, 15), "us_2026_off_peak"),  # 非旺季首日
        (date(2026, 10, 14), "us_2026_off_peak"),  # 非旺季末日
        (date(2026, 10, 15), "us_2026_peak"),      # 旺季首日
        (date(2027, 1, 14), "us_2026_peak"),       # 旺季末日，已跨年
        (date(2027, 1, 15), "us_2027_off_peak"),   # 2027 非旺季首日
        (date(2027, 10, 14), "us_2027_off_peak"),
    ],
)
def test_ship_date_selects_version(conn, day, expected):
    """旺季跨年：2027-01-14 仍属于「2026 旺季」。按年份切版本会在这里错。"""
    versions = fba_db.list_rate_versions(conn, MARKET)
    assert dom.pick_version(versions, day).version_key == expected


@pytest.mark.parametrize("day", [date(2025, 12, 1), date(2027, 10, 15), date(2027, 12, 1)])
def test_date_outside_all_versions_reports_missing_version(bare, day):
    """没有对应期次时必须说「缺版本」，而不是「缺档位」。"""
    with pytest.raises(ParamUnverified) as exc:
        fba_db.resolve(
            bare, MARKET, size_tier="large_standard", product_class="non_apparel",
            selling_price=29.99, ship_date=day, weight_oz=9,
        )
    assert "没有对应的费率版本" in exc.value.message
    assert "缺少该组合" not in exc.value.message


def test_next_year_rate_is_not_borrowed(bare, conn):
    """2027-12-01 只有 2026 旺季表可用，但它已过期——必须报缺，不能顶上。"""
    _verify(bare, weight_min_oz=8.0, weight_max_oz=12.0, base=4.48)
    with pytest.raises(ParamUnverified):
        fba_db.resolve(
            bare, MARKET, size_tier="large_standard", product_class="non_apparel",
            selling_price=29.99, ship_date=date(2027, 12, 1), weight_oz=9,
        )


def test_draft_version_is_rejected(bare, conn):
    """版本没核实，行再全也不能用：否则「有值」被当成「有依据」。"""
    _verify(bare, weight_min_oz=8.0, weight_max_oz=12.0, base=4.48)
    conn.execute(
        "UPDATE fba_rate_version SET status = 'draft' WHERE version_key = 'us_2026_off_peak'"
    )
    with pytest.raises(ParamUnverified) as exc:
        fba_db.resolve(
            bare, MARKET, size_tier="large_standard", product_class="non_apparel",
            selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=9,
        )
    assert "费率版本未核实" in exc.value.message


def test_overlapping_versions_stop_the_query(bare, conn):
    """两期区间重叠时，无论挑哪一期都是猜的，必须停。"""
    _verify(bare, weight_min_oz=8.0, weight_max_oz=12.0, base=4.48)
    conn.execute(
        "INSERT INTO fba_rate_version "
        "(version_key, marketplace, season, effective_from, effective_to, status, created_at) "
        "VALUES ('us_test_overlap', 'US', 'off_peak', '2026-06-01', '2026-06-30', 'draft', ?)",
        (CREATED_AT,),
    )
    with pytest.raises(ParamUnverified) as exc:
        fba_db.resolve(
            bare, MARKET, size_tier="large_standard", product_class="non_apparel",
            selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=9,
        )
    assert "同时落在" in exc.value.message
    assert fba_db.audit(bare, MARKET).version_overlaps == (
        ("us_2026_off_peak", "us_test_overlap"),
    )


def test_version_season_must_match_its_range(bare, conn):
    """版本的时段标注与区间不自洽时，同一行会被两套口径引用，必须报错。"""
    conn.execute(
        "INSERT INTO fba_rate_version "
        "(version_key, marketplace, season, effective_from, effective_to, status, created_at) "
        "VALUES ('us_test_bad_season', 'US', 'peak', '2025-01-01', '2025-12-31', 'draft', ?)",
        (CREATED_AT,),
    )
    with pytest.raises(ParamUnverified) as exc:
        fba_db.resolve(
            bare, MARKET, size_tier="large_standard", product_class="non_apparel",
            selling_price=29.99, ship_date=date(2025, 6, 1), weight_oz=9,
        )
    assert "不自洽" in exc.value.message


# ---------------------------------------------------------------- 匹配规则


def test_shared_endpoint_goes_to_finer_band(bare):
    """12 oz 同时属于 [8,12] 与 [12,16]，应命中起点更大的后者。"""
    _verify(bare, weight_min_oz=8.0, weight_max_oz=12.0, base=4.20)
    _verify(bare, weight_min_oz=12.0, weight_max_oz=16.0, base=4.60)
    quote = fba_db.resolve(
        bare, MARKET, size_tier="large_standard", product_class="non_apparel",
        selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=12,
    )
    assert quote.base_fee_usd == pytest.approx(4.60)
    assert quote.row.weight_min_oz == 12.0


def test_missing_band_is_not_extrapolated(bare):
    """中间断档必须报「缺这一档」，不能拿前一段或后一段顶上。"""
    for start in (0.0, 8.0, 12.0, 48.0):
        _verify(bare, weight_min_oz=start, weight_max_oz=None if start == 48.0 else 16.0)
    with pytest.raises(ParamUnverified) as exc:
        fba_db.resolve(
            bare, MARKET, size_tier="large_standard", product_class="non_apparel",
            selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=30,
        )
    assert "缺少该组合" in exc.value.message
    assert "30 oz" in exc.value.message


def test_missing_size_tier_does_not_fall_back(bare):
    """small_standard 一行都没录，绝不能退到 large_standard 的费率。"""
    _verify(bare, weight_min_oz=0.0, weight_max_oz=4.0, base=3.73)
    with pytest.raises(ParamUnverified):
        fba_db.resolve(
            bare, MARKET, size_tier="small_standard", product_class="non_apparel",
            selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=3,
        )


def test_missing_product_class_does_not_fall_back(bare):
    """服装没录，不能拿非服装的费率顶上——三套表之间差 10–25%。"""
    _verify(bare, weight_min_oz=8.0, weight_max_oz=12.0, base=4.48)
    with pytest.raises(ParamUnverified) as exc:
        fba_db.resolve(
            bare, MARKET, size_tier="large_standard", product_class="apparel",
            selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=9,
        )
    assert "服装" in exc.value.message


def test_product_class_is_required_and_validated(bare):
    """product_class 没有默认值：猜「非服装」会让含锂电池的商品每件少算约 $0.8。"""
    with pytest.raises(TypeError):
        fba_db.resolve(  # type: ignore[call-arg]
            bare, MARKET, size_tier="large_standard",
            selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=9,
        )
    with pytest.raises(InputInvalid):
        fba_db.resolve(
            bare, MARKET, size_tier="large_standard", product_class="fragile",
            selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=9,
        )


def test_unknown_size_tier_is_input_error(bare):
    with pytest.raises(InputInvalid):
        fba_db.resolve(
            bare, MARKET, size_tier="huge", product_class="non_apparel",
            selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=3,
        )


def test_dangerous_goods_is_pricier_than_non_apparel(conn):
    """同一档位，危险品的费率必须更高：这是官方三套表的核心差别。"""
    common = dict(size_tier="large_standard", selling_price=29.99,
                  ship_date=PEAK_DAY, weight_oz=9)
    plain = fba_db.resolve(conn, MARKET, product_class="non_apparel", **common)
    hazmat = fba_db.resolve(conn, MARKET, product_class="dangerous_goods", **common)
    assert hazmat.base_fee_usd > plain.base_fee_usd


# ---------------------------------------------------------------- 增量计费


def test_increment_starts_after_included_weight(bare):
    """首 3 lb（48 oz）收 base，48 oz 本身不加价。"""
    _insert(bare, weight_min_oz=48.0, weight_max_oz=320.0, per_increment_usd=0.08,
            increment_oz=4.0)
    bare.execute(
        "UPDATE fba_fee_schedule SET status = 'verified', source_url = ?, verified_at = ?",
        (SRC, "2026-11-21"),
    )
    quote = fba_db.resolve(
        bare, MARKET, size_tier="large_standard", product_class="non_apparel",
        selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=48,
    )
    assert quote.base_fee_usd == pytest.approx(5.0)
    assert quote.extra_weight_fee_usd == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("weight_oz", "extra"),
    [(52.0, 0.08), (56.0, 0.16), (49.0, 0.08), (320.0, 5.44)],
)
def test_increment_rounds_up_per_step(bare, weight_oz, extra):
    """不足一格按一格：49 oz 已超 48，收一格；320 oz 收 68 格。"""
    _insert(bare, weight_min_oz=48.0, weight_max_oz=320.0, per_increment_usd=0.08,
            increment_oz=4.0, status="verified", source_url=SRC, verified_at="2026-11-21")
    quote = fba_db.resolve(
        bare, MARKET, size_tier="large_standard", product_class="non_apparel",
        selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=weight_oz,
    )
    assert quote.extra_weight_fee_usd == pytest.approx(extra)


def test_official_increment_band_is_the_3_to_20_lb_row(conn):
    """只有 3–20 lb 那一档带增量，其余档位的增量字段必须为空。"""
    rows = [
        r for r in fba_db.list_fee_rows(conn, MARKET)
        if r.size_tier == "large_standard"
    ]
    with_increment = [r for r in rows if r.per_increment_usd > 0]
    assert {r.weight_min_oz for r in with_increment} == {48.0}
    assert {r.weight_max_oz for r in with_increment} == {320.0}
    assert {r.increment_oz for r in with_increment} == {4.0}


# ---------------------------------------------------------------- 核实状态


def test_draft_row_is_rejected_with_its_own_wording(bare):
    """有行但 draft：报「未核实」，与「缺这一档」必须是两句不同的话。"""
    _insert(bare, weight_min_oz=0.0, weight_max_oz=4.0)
    with pytest.raises(ParamUnverified) as exc:
        fba_db.resolve(
            bare, MARKET, size_tier="large_standard", product_class="non_apparel",
            selling_price=29.99, ship_date=OFF_PEAK_DAY, weight_oz=3,
        )
    assert "未核实" in exc.value.message
    assert "缺少该组合" not in exc.value.message


def test_mark_verified_on_absent_row_is_rejected(bare):
    """「标记为已核实」不能凭空造出一行：缺口只能靠补录，不能靠盖章。"""
    _insert(bare, weight_min_oz=0.0, weight_max_oz=4.0)
    with pytest.raises(ParamUnverified):
        fba_db.mark_verified(
            bare, marketplace=MARKET, version_key="us_2026_off_peak",
            size_tier="large_standard", product_class="non_apparel",
            weight_min_oz=999.0, price_band="10_to_50",
            source_url=SRC, verified_at="2026-11-21",
        )


def test_mark_version_verified_on_absent_version_is_rejected(bare):
    with pytest.raises(ParamUnverified):
        fba_db.mark_version_verified(
            bare, version_key="us_2099_peak", source_url=SRC, verified_at="2026-11-21"
        )


# ---------------------------------------------------------------- 燃油附加费


def test_fuel_surcharge_multiplies_subtotal(conn):
    """附加费按配送费计提，不按售价：$4.48 × 3.5% = $0.1568。"""
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", product_class="non_apparel",
        selling_price=29.99, ship_date=PEAK_DAY, weight_oz=9,
    )
    assert quote.fuel_surcharge_rate == pytest.approx(0.035)
    assert quote.fuel_surcharge_usd == pytest.approx(4.48 * 0.035)
    assert quote.total_fee_usd == pytest.approx(4.48 * 1.035)


def test_fuel_surcharge_not_applied_when_unverified(conn):
    """参数退回 draft 时，报价要明说「未施加」，且给出原因。"""
    conn.execute(
        "UPDATE cost_params SET status = 'draft', value = NULL WHERE param_key = ?",
        (fba_db.FUEL_SURCHARGE_KEY,),
    )
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", product_class="non_apparel",
        selling_price=29.99, ship_date=PEAK_DAY, weight_oz=9,
    )
    assert quote.fuel_surcharge_rate == 0.0
    assert quote.total_fee_usd == pytest.approx(4.48)
    assert any("未施加" in line for line in quote.assumptions)
    # 未施加的原因也要写进去：未登记与登记未核实是两种补数据动作
    assert any("draft" in line for line in quote.assumptions)


# ---------------------------------------------------------------- 审计


def test_audit_detects_overlap(bare):
    """[8,12] 之间塞一行 [10,20]，起点不同所以过得去 INSERT，但必须被审计抓出。"""
    _insert(bare, weight_min_oz=8.0, weight_max_oz=12.0)
    _insert(bare, weight_min_oz=10.0, weight_max_oz=20.0)
    assert fba_db.audit(bare, MARKET).overlap_count >= 1


def test_audit_does_not_flag_shared_endpoints(conn):
    """官方档位 [8,12] 与 [12,16] 共享端点，这是写法本身，不算重叠。"""
    assert fba_db.audit(conn, MARKET).overlap_count == 0


def test_audit_reports_gap_inside_tier_but_not_beyond_ceiling(bare):
    """小号标准件只录到 12 oz：12–16 是真缺口，16 oz 以上不属于这一段。"""
    for start, end in ((0.0, 2.0), (2.0, 4.0), (8.0, 12.0)):
        _insert(bare, size_tier="small_standard", weight_min_oz=start, weight_max_oz=end)
    groups = [g for g in fba_db.audit(bare, MARKET).groups
              if g.size_tier == "small_standard"]
    gaps = {g for group in groups for g in group.gaps}
    assert (4.0, 8.0) in gaps
    assert (12.0, 16.0) in gaps
    assert (16.0, None) not in gaps


def test_audit_marks_group_complete_only_when_no_gap_no_draft(bare):
    """把一组补成 0→∞ 的 verified 行，该组才应判为完整。"""
    _insert(bare, weight_min_oz=0.0, weight_max_oz=None,
            status="verified", source_url=SRC, verified_at="2026-11-21")
    group = fba_db.audit(bare, MARKET).groups[0]
    assert group.gaps == ()
    assert group.draft == 0
    assert group.complete is True


# ---------------------------------------------------------------- 报价输出


def test_price_at_band_boundary_raises_warning(conn):
    """$10.00 是边界值：$9.99 就是另一档费率，必须让调用方看见。"""
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", product_class="non_apparel",
        selling_price=10.0, ship_date=PEAK_DAY, weight_oz=9,
    )
    assert any("价格带分界" in line for line in quote.warnings)


def test_ordinary_price_has_no_band_warning(conn):
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", product_class="non_apparel",
        selling_price=29.99, ship_date=PEAK_DAY, weight_oz=9,
    )
    assert quote.warnings == ()


def test_low_price_band_warns_about_2027_eligibility(conn):
    """2027 起紧凑型家电不再享低价 FBA：<$10 档必须提示，否则成本被低估。"""
    quote = fba_db.resolve(
        conn, MARKET, size_tier="small_standard", product_class="non_apparel",
        selling_price=8.99, ship_date=NEXT_OFF_PEAK_DAY, weight_oz=5,
    )
    assert quote.version_key == "us_2027_off_peak"
    assert any("低价 FBA 资格收窄" in line for line in quote.warnings)


def test_quote_serializes_version_class_and_matched_range(conn):
    quote = fba_db.resolve(
        conn, MARKET, size_tier="large_standard", product_class="dangerous_goods",
        selling_price=29.99, ship_date=PEAK_DAY, weight_oz=9,
    )
    payload = quote.as_dict()
    assert payload["version_key"] == "us_2026_peak"
    assert payload["rate_period"] == "2026-10-15 至 2027-01-14"
    assert payload["product_class"] == "dangerous_goods"
    assert payload["price_band"] == "10_to_50"
    assert payload["season"] == "peak"
    assert payload["matched_range"] == "8–12 oz"
    assert payload["total_fee_usd"] == pytest.approx(5.64 * 1.035)


# ---------------------------------------------------------------- 数值不变式
#
# 这一节守的是「数字抄错」，不是结构。结构对了、数字错一位时，
# 上面所有用例都会绿——这正是本项目最怕的形态（结论看起来正常）。


def _rows_by_key(conn):
    keyed = {}
    for row in fba_db.list_fee_rows(conn, MARKET):
        keyed[(row.version_key, row.product_class, row.size_tier, row.price_band)] = row
    return keyed


def test_fee_increases_with_weight(conn):
    """同一组内，重量越重费率不得下降。抄错一位通常在这里破。"""
    grouped = {}
    for row in fba_db.list_fee_rows(conn, MARKET):
        grouped.setdefault(
            (row.version_key, row.product_class, row.size_tier, row.price_band), []
        ).append(row)
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda r: r.weight_min_oz)
        fees = [r.base_fee_usd for r in ordered]
        assert fees == sorted(fees), f"{key} 的费率不随重量单调：{fees}"


def test_low_price_band_is_never_more_expensive(conn):
    """<$10 档 <= $10–50 档 <= >$50 档，逐档成立。"""
    grouped = {}
    for row in fba_db.list_fee_rows(conn, MARKET):
        grouped.setdefault(
            (row.version_key, row.product_class, row.size_tier, row.weight_min_oz), {}
        )[row.price_band] = row.base_fee_usd
    for key, bands in grouped.items():
        assert bands["under_10"] <= bands["10_to_50"] <= bands["over_50"], key


def test_peak_is_never_cheaper_than_off_peak(conn):
    """同一档位，2026 旺季不得低于 2026 非旺季。"""
    rows = _rows_by_key(conn)
    for (version, cls, tier, band), off in rows.items():
        if version != "us_2026_off_peak":
            continue
        peak = rows[("us_2026_peak", cls, tier, band)]
        assert peak.base_fee_usd >= off.base_fee_usd, (tier, cls, band)


def test_2027_is_never_cheaper_than_2026(conn):
    """2027 非旺季全线不低于 2026 非旺季（官方说明：标准尺寸普涨）。"""
    rows = _rows_by_key(conn)
    for (version, cls, tier, band), old in rows.items():
        if version != "us_2026_off_peak":
            continue
        new = rows[("us_2027_off_peak", cls, tier, band)]
        assert new.base_fee_usd >= old.base_fee_usd, (tier, cls, band)


def test_dangerous_goods_is_never_cheaper_than_non_apparel(conn):
    """危险品三套表里最贵的一套；出现反例说明类别或数值抄串行了。"""
    rows = _rows_by_key(conn)
    for (version, cls, tier, band), plain in rows.items():
        if cls != "non_apparel":
            continue
        hazmat = rows[(version, "dangerous_goods", tier, band)]
        assert hazmat.base_fee_usd >= plain.base_fee_usd, (version, tier, band)
