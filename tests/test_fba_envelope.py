"""FBA 费用包络：不知道尺寸与重量时，这个系统还能给什么结论。

这组用例守三件事：

1. **包络两端必须是真实费率**——不是插值、不是均值、不是经验值。
   做法是反过来验：任取一个真实格子（行内中点重量），用 `quote()` 查出来的
   费用必须落在包络内。这条能抓住「上下界算错」和「用了 draft 行」。
2. **收窄只会变小**——给尺寸段、给重量上界之后，区间必须是原区间的子集，
   否则说明过滤条件没生效，而调用方会误以为自己的已知条件被用上了。
3. **缺失不退化成 0**——`fba_fee` 默认按 0 计的那条老路必须彻底走不通：
   不给数、也不给算包络的条件时，入参层直接拒绝。

第 3 条是这轮的起因：把「不知道 FBA 费」写成 0，毛利率会比真实值高出
$2.4–13.4，而响应里没有任何痕迹——精确到小数点后两位，只是偏高。
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from xborder_insight.db import fba as fba_db
from xborder_insight.domain import decision as decision_mod
from xborder_insight.domain import fba as dom
from xborder_insight.domain import profit as profit_mod
from xborder_insight.domain.compliance import WARNING, RuleHit, evaluate
from xborder_insight.errors import InputInvalid, ParamUnverified
from xborder_insight.schemas import CostProfileInput

MARKET = "US"
PEAK_DAY = date(2026, 11, 21)   # 2026 旺季
PRICE = 29.99                   # 落在 $10–50 价格带

CLEAR = evaluate([RuleHit("x", "w", WARNING, "verified", "https://e.gov")], "c", "US")


@pytest.fixture
def bare(conn):
    """清空费率行，只留版本表——用于构造「未封顶档位」这类真实数据里没有的形态。"""
    conn.execute("DELETE FROM fba_fee_schedule")
    return conn


def _envelope(conn, **over):
    params = dict(
        product_class="non_apparel", selling_price=PRICE, ship_date=PEAK_DAY
    )
    params.update(over)
    return fba_db.envelope(conn, MARKET, **params)


# ---------------------------------------------------------------- 包络本身


def test_bounds_come_from_rows_that_match_the_request(conn):
    env = _envelope(conn)
    assert env.low_usd < env.high_usd
    for row in (env.low_row, env.high_row):
        assert row.product_class == "non_apparel"
        assert row.price_band == "10_to_50"
        assert row.version_key == env.version_key
        assert row.verified, "draft 行不能参与界定一个「可证明」的区间"
    assert set(env.size_tiers) == {"small_standard", "large_standard"}


def test_every_real_cell_falls_inside_the_envelope(conn):
    """不变式：表里任何一个格子的费用都不能跑到包络外面。

    取行内中点而不是端点，是为了避开「相邻档共享端点」——12 oz 同时属于
    8–12 与 12–16，pick_row 会取起点更大的那一行，用它验证端点会验错对象。
    """
    rows = list(fba_db.list_fee_rows(conn, MARKET))
    versions = fba_db.list_rate_versions(conn, MARKET)
    env = _envelope(conn)

    checked = 0
    for row in rows:
        if (
            row.version_key != env.version_key
            or row.product_class != "non_apparel"
            or row.price_band != "10_to_50"
            or row.weight_max_oz is None
        ):
            continue
        mid = (row.weight_min_oz + row.weight_max_oz) / 2
        quote = dom.quote(
            rows,
            versions,
            size_tier=row.size_tier,
            product_class=row.product_class,
            selling_price=PRICE,
            ship_date=PEAK_DAY,
            weight_oz=mid,
            fuel_surcharge_rate=env.fuel_surcharge_rate,
        )
        assert env.low_usd - 1e-6 <= quote.total_fee_usd <= env.high_usd + 1e-6
        checked += 1
    assert checked == env.row_count


def test_narrowing_only_shrinks_the_interval(conn):
    full = _envelope(conn)
    small = _envelope(conn, size_tiers=["small_standard"])
    large = _envelope(conn, size_tiers=["large_standard"])
    capped = _envelope(conn, size_tiers=["large_standard"], weight_range_oz=(0.1, 32))

    for narrowed in (small, large, capped):
        assert full.low_usd <= narrowed.low_usd + 1e-9
        assert narrowed.high_usd <= full.high_usd + 1e-9

    assert small.spread_usd < full.spread_usd
    assert capped.spread_usd < large.spread_usd
    assert capped.narrowed and capped.assumed_weight_oz == (0.1, 32.0)


def test_small_and_large_union_reproduces_the_full_interval(conn):
    """两个尺寸段各自的包络，并起来正好是全区间。

    如果不能，说明某一段里有一行没被扫到（或扫到了不该扫的）。
    """
    full = _envelope(conn)
    small = _envelope(conn, size_tiers=["small_standard"])
    large = _envelope(conn, size_tiers=["large_standard"])
    assert small.low_usd == pytest.approx(full.low_usd)
    assert large.high_usd == pytest.approx(full.high_usd)


def test_unconstrained_envelope_warns_that_it_is_wide(conn):
    env = _envelope(conn)
    assert env.warnings, "全区间必须提示「可以收窄」，否则会被当成精确结论用"
    assert env.spread_usd > 1.0


# ---------------------------------------------------------------- 拒绝路径


def test_unknown_size_tier_is_rejected(conn):
    with pytest.raises(InputInvalid):
        _envelope(conn, size_tiers=["huge"])


def test_apparel_has_no_rows_so_it_must_fail(conn):
    with pytest.raises(ParamUnverified):
        _envelope(conn, product_class="apparel")


def test_date_without_version_must_fail(conn):
    with pytest.raises(ParamUnverified):
        _envelope(conn, ship_date=date(2027, 12, 1))


def test_weight_range_outside_the_table_must_fail(conn):
    with pytest.raises(ParamUnverified):
        _envelope(conn, weight_range_oz=(400.0, 500.0))


def test_open_ended_row_cannot_bound_the_upper_edge(bare):
    """有 weight_max 为空的档位时，上界无从计算，必须报缺而不是取下界充当上界。"""
    bare.execute(
        "INSERT INTO fba_fee_schedule (version_key, marketplace, size_tier,"
        " product_class, weight_min_oz, weight_max_oz, price_band, base_fee_usd,"
        " per_increment_usd, increment_oz, included_oz, source_url, verified_at,"
        " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "us_2026_peak", MARKET, "large_standard", "non_apparel",
            0.0, None, "10_to_50", 5.0, 0.0, None, None,
            "https://e.gov", "2026-11-21", "verified", "2026-11-21T00:00:00+08:00",
        ),
    )
    with pytest.raises(ParamUnverified, match="未封顶"):
        _envelope(bare)


# ---------------------------------------------------------------- 利润层


PROFIT_BASE = dict(
    quoted_price=18.00,
    selling_price=59.99,
    inbound_freight=2.20,
    duty_rate=0.25,
    referral_rate=0.15,
    return_rate=0.08,
    returns_handling_fee=3.00,
    returns_refund_admin_rate=0.20,
    returns_goods_loss_ratio=0.30,
    returns_inbound_loss_ratio=1.00,
    incoterm="FOB",
)


def _compute(**over):
    return profit_mod.compute(profit_mod.CostInputs(**{**PROFIT_BASE, **over}))


def test_missing_fee_without_bounds_is_rejected_at_domain_level():
    with pytest.raises(ParamUnverified):
        _compute(fba_fee=None)


def test_point_estimate_takes_the_expensive_end():
    result = _compute(fba_fee=None, fba_bounds=(4.0, 9.0))
    assert result.fba_known is False
    assert result.fba_fee == pytest.approx(9.0), "点值必须取最贵档，低估成本比高估危险"
    assert result.gross_margin == pytest.approx(result.gross_margin_low)
    assert result.gross_margin_low < result.gross_margin_high


def test_bounds_must_be_ordered():
    with pytest.raises(InputInvalid):
        _compute(fba_fee=None, fba_bounds=(9.0, 4.0))


def test_known_fee_produces_a_degenerate_interval():
    result = _compute(fba_fee=5.0)
    assert result.fba_known is True
    assert result.gross_margin_low == pytest.approx(result.gross_margin_high)
    assert result.fba_low == result.fba_high == pytest.approx(5.0)


# ---------------------------------------------------------------- 决策层


def _decide(**over):
    return decision_mod.decide(_compute(**over), CLEAR, 0.7, data_provenance="test")


def test_interval_entirely_cheap_is_decidable_without_measuring():
    """最贵档都达标 → 直接 PASS。不需要真实的包装尺寸。"""
    result = _decide(fba_fee=None, fba_bounds=(4.0, 5.0))
    margin = next(r for r in result.reasons if r.factor == "单件毛利")
    assert margin.result == decision_mod.PASS
    assert "尺寸重量只会让结论更好" in margin.detail


def test_interval_entirely_expensive_is_decidable_as_fail():
    result = _decide(fba_fee=None, fba_bounds=(13.0, 15.0))
    assert result.verdict == decision_mod.NO_GO


def test_interval_straddling_the_threshold_needs_more_info():
    """真实包络跨阈值时，结论只能是「需要量包装」——既不能 GO 也不能 NO_GO。"""
    result = _decide(fba_fee=None, fba_bounds=(5.06, 14.52))
    assert result.verdict == decision_mod.NEED_MORE_INFO
    margin = next(r for r in result.reasons if r.factor == "单件毛利")
    assert margin.result == decision_mod.UNKNOWN
    assert "包装" in result.headline


def test_fba_unknown_does_not_fake_a_point_estimate():
    """回归：曾经 fba_fee 默认 0，毛利率会被静默抬高。现在必须有区间字段。"""
    result = _decide(fba_fee=None, fba_bounds=(5.06, 14.52))
    body = result.as_dict()["profit"]
    assert body["fba_known"] is False
    low, high = body["gross_margin_range"]
    assert low < high


# ---------------------------------------------------------------- 契约层


def test_cost_profile_without_fee_or_envelope_inputs_is_rejected():
    with pytest.raises(ValidationError) as exc:
        CostProfileInput(quoted_price=18.0, selling_price=59.99, referral_rate=0.15)
    assert "fba_fee" in str(exc.value)


def test_cost_profile_rejects_unknown_product_class():
    with pytest.raises(ValidationError):
        CostProfileInput(
            quoted_price=18.0,
            selling_price=59.99,
            referral_rate=0.15,
            product_class="toys",
            ship_date="2026-11-21",
        )


def test_cost_profile_accepts_an_explicit_fee_without_class_or_date():
    profile = CostProfileInput(
        quoted_price=18.0, selling_price=59.99, referral_rate=0.15, fba_fee=6.5
    )
    assert profile.fba_fee == 6.5
    assert profile.product_class is None
