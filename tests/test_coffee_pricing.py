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


def _profit(fba_fee, *, price=SELLING_PRICE, freight=6.00, duty=0.237, returns=0.08,
            cogs=QUOTED_PRICE):
    return p.compute(
        p.CostInputs(
            quoted_price=cogs,
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


def test_filling_the_box_to_the_ceiling_costs_eight_cents_more(conn):
    """18×14×8 顶满 → 体积重 232.1 oz 压过实重 232 oz，比 17×14×8 多 $0.08。

    上限不是「三边 ≤18/14/8」，而是「三边 ≤18/14/8 **且体积 ≤2015.5 in³**」：
    2016 比临界多 0.5 in³，够把体积重顶过实重，费率进一档续重。
    给供应商的约束若只写三边，他会合理地顶满，$2.55 的收益要掉 8 分钱——
    金额小，但它证明「贴着上限打」不是免费的，这条判断只能靠算不能靠猜。
    """
    c17, e17 = _fee(conn, (17.0, 14.0, 8.0), 232.0)
    c18, e18 = _fee(conn, (18.0, 14.0, 8.0), 232.0)

    assert c17.size_tiers == c18.size_tiers == ("large_standard",)
    assert e17.low_usd == pytest.approx(11.85, abs=0.005)
    assert e18.low_usd == pytest.approx(11.93, abs=0.005)
    assert e18.low_usd - e17.low_usd == pytest.approx(0.08, abs=0.005)

    # 差价的来源是计费重量，不是档位——档位两者相同
    assert e17.billable_weight_oz == pytest.approx(232.0, abs=0.05)
    assert e18.billable_weight_oz == pytest.approx(232.1, abs=0.05)
    assert e18.billable_weight_oz > 232.0 > 231.9


def test_branch_b_cannot_be_rescued_by_price_within_tolerance(conn):
    """B 档（$14.40）+ 现报价 $18.50：+5% 提价容忍内最高只有 8.83%，到 15% 要 $74.14。

    「券后价 $65.99 = 15.31%」这句曾被写进 `docs/04` §33.3，实测 **7.63%**——错在把
    「券后价」当成目录价又打了一次折，等于多算一次毛利。四个价位一起钉住：
    改退货率、改 FBA 档位、或再有人手算一遍，都会立刻红。
    """
    _, b = _fee(conn, (16.0, 13.0, 9.5), 232.0)
    assert b.low_usd == pytest.approx(14.40, abs=0.005)

    def margin(price: float) -> float:
        return _profit(b.low_usd, price=price, returns=0.10).gross_margin

    assert margin(63.99) == pytest.approx(0.05534, abs=5e-5)  # 他现在的报价
    assert margin(65.99) == pytest.approx(0.07630, abs=5e-5)  # 旧记录误称 15.31%
    assert margin(67.19) == pytest.approx(0.08828, abs=5e-5)  # 提价上限 +5%
    assert margin(74.99) == pytest.approx(0.15679, abs=5e-5)  # 到 15% 的实际价位

    # B 档不是「提个价就能做」：容忍带内没有任何价位到 15%
    assert margin(SELLING_PRICE * 1.05) < 0.15
    assert margin(74.99) >= 0.15  # 而能到的那个价超容忍（+17.2%）


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
    assert landed.gross_margin == pytest.approx(0.17357, abs=5e-4)

    full = _profit(before.low_usd)
    assert full.gross_margin == pytest.approx(0.07899, abs=5e-4)

    # 改包后同一口径：+3.99 个点
    assert _profit(after.low_usd).gross_margin == pytest.approx(0.11884, abs=5e-4)


@pytest.mark.parametrize(
    ("duty", "margin"),
    [
        (0.000, 0.18735),  # 有 FTA 或豁免时的上界
        (0.037, 0.17666),  # 若原产国非中国：只剩 MFN，两个 301 层都不适用
        (0.237, 0.11884),  # 原产中国（当前口径）：3.7% + 12.5% + 7.5%
        (0.375, 0.07894),  # 若 25% 旧清单也叠加（充电器的结构，不是咖啡机）
    ],
)
def test_duty_is_the_loudest_lever(conn, duty, margin):
    """关税 0→37.5% 掉 11 个点，比改包（+4pt）与换货代（约 +3pt）都大。

    所以 H8 不是「补一个参数」，它决定低价打法这条路是否存在。
    四档都钉住：23.7% 与 3.7% 是「原产国」这个二选一的两端（11-21 16:50 起，
    中间态 16.2% 已被排除——List 4A 那层从 Chapter 99 原文核实到 8516.71.00 在内）。
    """
    _, after = _fee(conn, (16.0, 13.0, 8.0), 232.0)
    assert _profit(after.low_usd, duty=duty).gross_margin == pytest.approx(margin, abs=5e-4)


def _solve_price(target_margin: float, fba_fee: float, *, duty: float = 0.237) -> float:
    low, high = 20.0, 400.0
    for _ in range(80):
        mid = (low + high) / 2
        if _profit(fba_fee, price=mid, duty=duty).gross_margin < target_margin:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def test_15pct_floor_is_6722_after_repack(conn):
    """券后 $63.99 只有 11.9% 的 ACOS 上限，打不起广告；15% 线在 $67.22。"""
    _, before = _fee(conn, (16.0, 13.0, 9.5), 232.0)
    _, after = _fee(conn, (16.0, 13.0, 8.0), 232.0)

    assert _solve_price(0.15, before.low_usd) == pytest.approx(71.34, abs=0.01)
    assert _solve_price(0.15, after.low_usd) == pytest.approx(67.22, abs=0.05)
    assert _solve_price(0.20, after.low_usd) == pytest.approx(73.14, abs=0.05)
    # 若原产国非中国（3.7%），15% 线降到 $61.23 —— 那时 $63.99 是 17.67%，反而成立
    assert _solve_price(0.15, after.low_usd, duty=0.037) == pytest.approx(61.23, abs=0.05)


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


@pytest.mark.parametrize(
    ("freight", "margin"),
    [(0.0, 0.21406), (3.0, 0.16649), (6.0, 0.11884), (9.0, 0.07119), (12.0, 0.02355)],
)
def test_freight_is_now_the_biggest_unknown(conn, freight, margin):
    """头程每 +$1 → 毛利率 −1.59 个点；$3 与 $12 之间差 14.3 个点。

    关税已收窄成二选一（23.7% / 3.7%，见 §15.4），而 inbound_freight 仍是纯假设 $6，
    所以现在最该先去要的是货代报价，不是继续调价。这条把弹性钉住：
    报价单一到手，改一个参数就能读出结论，不必重算整条链。
    """
    _, after = _fee(conn, (16.0, 13.0, 8.0), 232.0)
    assert _profit(after.low_usd, freight=freight).gross_margin == pytest.approx(margin, abs=5e-4)


def test_one_dollar_of_freight_costs_159_basis_points(conn):
    _, after = _fee(conn, (16.0, 13.0, 8.0), 232.0)
    step = _profit(after.low_usd, freight=7.0).gross_margin - _profit(
        after.low_usd, freight=6.0
    ).gross_margin
    assert step == pytest.approx(-0.0159, abs=1e-4)


def _ceiling(fba_fee, returns, target=0.15):
    """给定 FBA 档与退货率，反解 15% 线对应的采购上限。"""
    lo, hi = 0.0, 80.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _profit(fba_fee, returns=returns, cogs=mid).gross_margin > target:
            lo = mid
        else:
            hi = mid
    return mid


def _rate_for_his_quote(fba_fee, target=0.15):
    """反解：退货率低到多少，他的 $18.50 才能在 15% 线上成立。

    单调性成立（退货率越低毛利越高），所以二分可用。返回的既是「需要的退货率」，
    也是这台机器的**生死线**——低于它 $18.50 才成立。
    """
    lo, hi = 0.0, 0.40
    for _ in range(80):
        mid = (lo + hi) / 2
        if _profit(fba_fee, returns=mid).gross_margin > target:
            lo = mid
        else:
            hi = mid
    return mid


def test_no_single_lever_gets_his_quote_over_the_line(conn):
    """15% 裁决之后，四个变量逐个跑：没有一个能把 $18.50 推上 15% 线。

    **这条是「不做」这个结论的全部依据**，所以每个数都对得上一条实跑：

        退货率 10%（11-23 的旧口径，最好情况）  A 档上限 $15.732  → 差 $2.77
        退货率 12%                              A 档上限 $14.556  → 差 $3.94
        退货率 5.36%                            A 档上限 $18.50   → 刚好成立（不现实）
        箱规 ≤8 英寸 + 15% 退货                 $12.813           → 差 $5.69
        佣金 8% + 现箱 + 15% 退货               $14.423           → 差 $4.08

    写这条的直接原因是文档初稿里出现过一个凭印象的数（「12% 时上限约 $13.85」），
    实测是 $14.556——差一截，而且据此推出来的「低于 13% 就重开」这个门槛整个是错的
    （真门槛 5.36%）。估的数只要没被测试钉住，就会长回文档里。
    """
    # 四档上限（15% 退货率为基准）
    assert _ceiling(11.85, 0.15) == pytest.approx(12.813, abs=5e-4)
    assert _ceiling(14.40, 0.15) == pytest.approx(10.824, abs=5e-4)
    # 退货率回到旧口径也救不回来
    assert _ceiling(11.85, 0.10) == pytest.approx(15.732, abs=5e-4)
    assert _ceiling(11.85, 0.12) == pytest.approx(14.556, abs=5e-4)
    # 佣金 8%（-7 个点）+ 15% 退货：毛利只有 6.83%（12.67% 是 10% 退货率下的数），
    # 上限 $14.423，仍差 $4.08
    eight = p.compute(
        p.CostInputs(
            quoted_price=QUOTED_PRICE, selling_price=SELLING_PRICE,
            inbound_freight=6.00, duty_rate=0.237, referral_rate=0.08,
            fba_fee=14.40, return_rate=0.15,
            returns_handling_fee=3.00, returns_refund_admin_rate=0.20,
            returns_goods_loss_ratio=0.30, returns_inbound_loss_ratio=0.20,
            incoterm="FOB",
        )
    )
    assert eight.gross_margin == pytest.approx(0.0683, abs=5e-4)

    # 生死线：A 档要 5.36%、B 档要 1.99% —— 都不是一台真实机器会有的退货率
    assert _rate_for_his_quote(11.85) == pytest.approx(0.0536, abs=2e-4)
    assert _rate_for_his_quote(14.40) == pytest.approx(0.0199, abs=2e-4)
