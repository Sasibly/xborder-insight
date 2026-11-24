"""净化器箱规：一个被作废的尺寸，当了两天的「现状」。

2026-11-21 记下的外箱 24×15×15 / 18.5 lb 在 11-23 13:51 被用户补交的五项分解推翻
（箱体积 5400 in³ 是机器 1116 in³ 的 4.84 倍，逐边余 8.5/3/9 英寸；毛重 18.5
对不上净重 7.5 + 空箱 1.76 = 9.26）。`measured_skus.json` 里它已经进了 `superseded`。

但那套数推出来的两句话留在了文档里，并且都被当成事实用过：

1. 「净化器 5400 in³，标准尺寸上限 2016 in³，改包物理上不可能」——让人以为这台
   注定进大件，$11.35 是个够不着的乐观值；
2. 「箱规必须改到 17.5×13.7×7.5 才守得住 $11.35」——把**安全垫**说成了**档位线**，
   拿到谈判桌上去要求供应商缩 0.08 英寸，换回一句「改不了」。

真正的事实：大号标准件上限是 18×14×8，现行箱 17.52×13.78×7.48 **本来就在限内**，
FBA 一直是 $11.35。17.5×13.7×7.5 只是把余量从 0.48/0.22/0.52 挪到 0.5/0.3/0.52，
是给批量波动留的缓冲，不是过不过线的条件。

这组用例钉四件事：

- 上限是 18×14×8（常量），谈判目标（17.5×13.7×7.5）逐边更严——两者不可混用；
- 现行箱判 `large_standard`，次长边多 0.01 英寸就判 `small_bulky`（一分钱不差的悬崖）；
- 掉档的代价是 $2.65/件、3.3 个毛利点，签字线从 15.01% 掉到 11.69%；
- `measured_skus.json` 里 24×15×15 必须留在 `superseded`——谁把它提回 `dims_in`，
  整个净化器结论就翻成「做不成」，且不会有任何报错。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from xborder_insight.db import fba as fba_db
from xborder_insight.domain import package as pkg
from xborder_insight.domain import profit as p

SHIP_DATE = date(2026, 11, 21)      # 2026 旺季
MARKET = "US"
PRICE = 79.99
REFERRAL = 0.15
FREIGHT = 6.00
DUTY = 0.125
RETURNS = 0.12                      # 净化器基线档（H3 未核实前的口径）

#: 实测彩盒（`measured_skus.json`，2026-11-23 13:51 五项分解）
CURRENT_DIMS = (17.52, 13.78, 7.48)
CURRENT_WEIGHT_LB = 9.26

#: 13:51 谈判卡片里要求供应商改到的尺寸（安全垫，不是上限）
NEGOTIATION_TARGET_DIMS = (17.5, 13.7, 7.5)

PROFILE = Path(__file__).resolve().parents[1] / "data" / "profiles" / "measured_skus.json"


def _package(conn, dims, weight_lb=CURRENT_WEIGHT_LB):
    return fba_db.envelope_for_package(
        conn,
        MARKET,
        pkg.PackageFacts(
            has_lithium_battery=False,
            unit_weight_oz=weight_lb * 16,
            dims_in=dims,
        ),
        selling_price=PRICE,
        ship_date=SHIP_DATE,
        exact_weight=True,
    )


def _margin(fba_fee, cogs, *, returns=RETURNS):
    return p.compute(
        p.CostInputs(
            quoted_price=cogs,
            selling_price=PRICE,
            inbound_freight=FREIGHT,
            duty_rate=DUTY,
            referral_rate=REFERRAL,
            fba_fee=fba_fee,
            return_rate=returns,
            returns_handling_fee=3.00,
            returns_refund_admin_rate=0.20,
            returns_goods_loss_ratio=0.30,
            returns_inbound_loss_ratio=0.20,
            incoterm="FOB",
        )
    ).gross_margin


# ------------------------------------------------------- 上限 vs 安全垫


def test_the_ceiling_is_18x14x8_not_the_negotiation_target():
    """档位上限写在 `package.py` 里，是三个方向上的硬边界。

    谈判目标（17.5×13.7×7.5）逐边都比它小，所以「要求改到 17.5×13.7×7.5」
    不是「进标准尺寸」的必要条件，而是「多留一点余量」。这两句话对供应商
    的含义完全不同：前者他现在就满足，后者要换纸板规格。
    """
    assert pkg.STANDARD_MAX_DIMS_IN == (18.0, 14.0, 8.0)
    # 谈判目标逐边更严（按长度降序比较）
    target = sorted(NEGOTIATION_TARGET_DIMS, reverse=True)
    for got, limit in zip(target, pkg.STANDARD_MAX_DIMS_IN):
        assert got < limit, f"安全垫 {got} 应当小于上限 {limit}"


def test_current_box_already_fits_the_standard_tier():
    """现行箱三条边全在限内——这正是「箱规改不了」不改变任何数字的原因。

    `standard_edge_violation` 返回 None 表示三条都装得进；返回元组时第一项
    是第几长边越界，用来区分「哪条边最危险」。
    """
    assert pkg.standard_edge_violation(CURRENT_DIMS) is None
    # 谈判目标也满足（它更严），所以「要求改到 17.5×13.7×7.5」在合规上是多余的
    assert pkg.standard_edge_violation(NEGOTIATION_TARGET_DIMS) is None


def test_mistaking_the_cushion_for_the_ceiling_would_void_the_current_box():
    """把安全垫当上限，现行箱立刻「越界」——这就是那句「改不了」的来源。

    真实上限 18×14×8 时现行箱通过；换成 17.5×13.7×7.5 就前两条边都超。
    一次误用会让一个本来合规的箱子看起来必须改，于是去要一个供应商做不到的承诺。
    """
    assert pkg.edge_violation(CURRENT_DIMS, (18.0, 14.0, 8.0)) is None

    wrong = pkg.edge_violation(CURRENT_DIMS, NEGOTIATION_TARGET_DIMS)
    assert wrong is not None, "安全垫比现行箱还严，混用必然误判"
    edge_no, actual, limit = wrong
    assert edge_no == 1 and actual == 17.52 and limit == 17.5


def test_current_box_is_large_standard_and_costs_11_35(conn):
    klass, env = _package(conn, CURRENT_DIMS)
    assert klass.size_tiers == ("large_standard",)
    assert env.low_usd == pytest.approx(env.high_usd), "给了三边与实重就该是单点"
    assert env.high_usd == pytest.approx(11.35, abs=0.005)

    # 计费重量由体积重决定（1805.9 ÷ 139 = 12.99 → 13 lb），不是实重 9.26 lb
    assert env.dim_weight_oz == pytest.approx(1805.9 / 139 * 16, rel=1e-3)
    assert env.billable_weight_oz > CURRENT_WEIGHT_LB * 16


def test_one_hundredth_of_an_inch_drops_the_tier(conn):
    """次长边 13.78 → 14.01，费用从 $11.35 跳到 $14.00。

    这就是那 0.22 英寸余量的价钱：纸箱受潮、封箱带、气柱袋都会让它涨一点，
    所以「不用改箱」之后仍然要把 18×14×8 写进 PI，而不是当它自动成立。
    """
    over = (17.52, 14.01, 7.48)
    violation = pkg.standard_edge_violation(over)
    assert violation is not None
    edge_no, actual, limit = violation
    assert edge_no == 2, "越界的是次长边（排序后第二位）"
    assert actual == pytest.approx(14.01)
    assert limit == 14.0

    klass, env = _package(conn, over)
    assert klass.size_tiers == ("small_bulky",)
    assert env.high_usd == pytest.approx(14.00, abs=0.01)


# ------------------------------------------------------- 掉档的代价


def test_signing_line_survives_only_in_the_standard_tier():
    """$24.33 是签字线（15.01%）——这个结论只在 FBA $11.35 时成立。

    掉档到 $14.00 后同一个价只有 11.69%，差 3.31 个点。所以箱规条款不是
    「合同细节」，它决定这个价成不成立。
    """
    assert _margin(11.35, 24.33) == pytest.approx(0.1501, abs=5e-4)
    assert _margin(14.00, 24.33) == pytest.approx(0.1169, abs=5e-4)
    assert _margin(11.35, 24.33) - _margin(14.00, 24.33) > 0.03


def test_the_step_above_the_signing_line_is_already_under_it():
    """$24.50 在标准尺寸里也只有 14.76%——让步带只有 $0.33，没有第二档。"""
    assert _margin(11.35, 24.50) == pytest.approx(0.1476, abs=5e-4)
    assert _margin(11.35, 24.50) < 0.15 < _margin(11.35, 24.33)


# ------------------------------------------------------- 作废记录不许复活


def test_superseded_box_must_stay_in_the_superseded_block():
    """24×15×15 / 18.5 lb 只能待在 `superseded` 里。

    这条守的是一次真实事故的复现路径：把它提回 `dims_in`，FBA 变 $23.84，
    $24.00 从 15.49% 变成 −0.13%，结论从「可以谈」翻成「做不成」——
    而整个过程中没有任何异常、没有报错、没有告警，只有数字悄悄变了。
    """
    data = json.loads(PROFILE.read_text(encoding="utf-8"))
    purifier = next(
        c for c in data["categories"] if c["key"] == "air_purifier"
    )
    variant = purifier["variants"][0]

    assert tuple(variant["dims_in"]) == CURRENT_DIMS
    assert variant["weight_oz"] == pytest.approx(CURRENT_WEIGHT_LB * 16, abs=1e-6)

    superseded = variant.get("superseded")
    assert superseded is not None, "作废的 24×15×15 必须留档，否则无法复盘"
    assert tuple(superseded["dims_in"]) == (24.0, 15.0, 15.0)
    assert superseded["weight_oz"] == pytest.approx(18.5 * 16, abs=1e-6)
    # 现行尺寸不能等于作废尺寸
    assert tuple(variant["dims_in"]) != tuple(superseded["dims_in"])


def test_the_void_box_would_have_flipped_the_conclusion(conn):
    """把作废尺寸拿来重算会得到什么——写出来是为了让这个差距看得见。

    不作断言说「$23.84 是对的」，只断言它和现行口径的差距足以翻转结论：
    同一口价 $24.00，一个是 15.49%（过线），一个是 −0.13%（跌破成本）。
    """
    klass, env = _package(conn, (24.0, 15.0, 15.0), weight_lb=18.5)
    assert klass.size_tiers == ("small_bulky",)
    void_fee = env.high_usd
    assert void_fee == pytest.approx(23.84, abs=0.05)

    _, current = _package(conn, CURRENT_DIMS)
    assert _margin(current.high_usd, 24.00) == pytest.approx(0.1549, abs=5e-4)
    assert _margin(void_fee, 24.00) < 0.0
    assert _margin(current.high_usd, 24.00) - _margin(void_fee, 24.00) > 0.15


# ------------------------------------------------------- 208 oz 档顶：真正的硬线


def test_the_real_hard_line_is_1807_cubic_inches_not_the_three_edges(conn):
    """$11.35 的档顶是计费重量 208 oz，对应体积 **1807 in³**。

    样品 1805.9 in³，余量 **1.1 in³**——不是「三边各还有 0.48/0.22/0.52 英寸」那种余量，
    是「任何方向涨 0.005 英寸就到线」。所以 PI 里该锁的是体积，不是三边分列：
    三条边各写一个上限，读的人会以为三个方向各有各的余量，实际它们共享这 1.1。
    """
    _, sample = _package(conn, CURRENT_DIMS)
    assert sample.billable_weight_oz == pytest.approx(207.9, abs=0.2)
    assert sample.high_usd == pytest.approx(11.35, abs=0.005)

    # 高度只加 0.01 英寸（一个纸板厚度的量级）就跳档
    _, nudge = _package(conn, (17.52, 13.78, 7.49))
    assert nudge.billable_weight_oz / 16 > 13.0
    assert nudge.high_usd == pytest.approx(11.44, abs=0.005)

    # 208 oz 是那条线：体积 1807.0 = 208 ÷ 16 × 139
    assert 208 / 16 * 139 == pytest.approx(1807.0, abs=0.5)
    assert CURRENT_DIMS[0] * CURRENT_DIMS[1] * CURRENT_DIMS[2] < 208 / 16 * 139


def test_a_14_lb_dim_weight_clause_would_give_away_34_cents(conn):
    """「体积重不超过 14 磅」在 ① 成立时是死条款，单独生效时让掉 $0.34/件。

    ① 把三边锁在 17.5×13.7×7.5 时体积最大 1798.1 in³ = 12.94 lb，永远够不到 14 lb；
    可一旦三边放开、只留这条，14 lb = 1946 in³，比样品宽 140 in³，实测 $11.69。
    一条永远为真、单独为真时又亏钱的约束，写进合同只是让争议时有话说。
    """
    cap_volume = NEGOTIATION_TARGET_DIMS[0] * NEGOTIATION_TARGET_DIMS[1] * NEGOTIATION_TARGET_DIMS[2]
    assert cap_volume / 139 == pytest.approx(12.94, abs=0.01), "① 成立时到不了 14 lb"

    # 14 lb 体积重对应的实际形状：长边放到 18、高度放到 7.84（三边仍在 18×14×8 内）
    wide = (18.0, 13.78, 7.84)
    assert wide[0] * wide[1] * wide[2] / 139 == pytest.approx(13.99, abs=0.02)
    assert pkg.standard_edge_violation(wide) is None, "它仍在大号标准件里"

    klass, env = _package(conn, wide)
    assert klass.size_tiers == ("large_standard",)
    assert env.high_usd == pytest.approx(11.69, abs=0.005)
    assert env.high_usd - 11.35 == pytest.approx(0.34, abs=0.01)


def test_a_010_inch_tolerance_is_not_free_at_the_band_ceiling(conn):
    """「任一边超出 0.1 英寸以上才书面告知」——在档顶，0.1 英寸是 $0.17/件。

    而且在 208 oz 这条线上，0.01 英寸就已经跳档（上一条用例）。所以容差写 0.1
    不是「留一点余地」，是把 $0.17 白送出去；条款要么不给容差，要么写明由供方承担。
    """
    tol = (17.6, 13.88, 7.55)
    assert tol[0] * tol[1] * tol[2] == pytest.approx(1844.4, abs=0.5)

    _, sample = _package(conn, CURRENT_DIMS)
    klass, env = _package(conn, tol)
    assert klass.size_tiers == ("large_standard",)
    assert env.high_usd == pytest.approx(11.52, abs=0.005)
    assert env.high_usd - sample.high_usd == pytest.approx(0.17, abs=0.01)


def test_the_2_65_penalty_belongs_to_1401_not_to_a_01_inch_drift(conn):
    """$2.65 是「次长边超过 14.00」的价钱，不是「任一边超 0.1 英寸」的价钱。

    条款把两件事绑成一句（「超 0.1 英寸……差额费用（$2.65/件）由卖方承担」），
    后果是主张时被算据打回：0.1 英寸漂移实测 $0.17，只有 14.00→14.01 才是 $2.65。
    两种违约要分两条写，各自的金额才站得住。
    """
    _, drift = _package(conn, (17.6, 13.88, 7.55))          # 三边各 +0.1
    assert drift.high_usd == pytest.approx(11.52, abs=0.005)
    assert drift.high_usd - 11.35 == pytest.approx(0.17, abs=0.01)

    _, cliff = _package(conn, (17.52, 14.01, 7.48))          # 次长边过 14.00
    assert cliff.high_usd == pytest.approx(14.00, abs=0.01)
    assert cliff.high_usd - 11.35 == pytest.approx(2.65, abs=0.01)


# ------------------------------------------------------- 208 oz 档顶：真正的硬线


def test_the_real_hard_line_is_1807_cubic_inches_not_the_three_edges(conn):
    """$11.35 的档顶是计费重量 208 oz，对应体积 **1807 in³**。

    样品 1805.9 in³，余量 **1.1 in³**——不是「三边各还有 0.48/0.22/0.52 英寸」那种余量，
    是「任何方向涨 0.005 英寸就到线」。所以 PI 里该锁的是体积，不是三边分列：
    三条边各写一个上限，读的人会以为三个方向各有各的余量，实际它们共享这 1.1。
    """
    _, sample = _package(conn, CURRENT_DIMS)
    assert sample.billable_weight_oz == pytest.approx(207.9, abs=0.2)
    assert sample.high_usd == pytest.approx(11.35, abs=0.005)

    # 高度只加 0.01 英寸（一个纸板厚度的量级）就跳档
    _, nudge = _package(conn, (17.52, 13.78, 7.49))
    assert nudge.billable_weight_oz / 16 > 13.0
    assert nudge.high_usd == pytest.approx(11.44, abs=0.005)

    # 208 oz 是那条线：体积 1807.0 = 208 ÷ 16 × 139
    assert 208 / 16 * 139 == pytest.approx(1807.0, abs=0.5)
    assert CURRENT_DIMS[0] * CURRENT_DIMS[1] * CURRENT_DIMS[2] < 208 / 16 * 139


def test_a_14_lb_dim_weight_clause_would_give_away_34_cents(conn):
    """「体积重不超过 14 磅」在 ① 成立时是死条款，单独生效时让掉 $0.34/件。

    ① 把三边锁在 17.5×13.7×7.5 时体积最大 1798.1 in³ = 12.94 lb，永远够不到 14 lb；
    可一旦三边放开、只留这条，14 lb = 1946 in³，比样品宽 140 in³，实测 $11.69。
    一条永远为真、单独为真时又亏钱的约束，写进合同只是让争议时有话说。
    """
    cap_volume = NEGOTIATION_TARGET_DIMS[0] * NEGOTIATION_TARGET_DIMS[1] * NEGOTIATION_TARGET_DIMS[2]
    assert cap_volume / 139 == pytest.approx(12.94, abs=0.01), "① 成立时到不了 14 lb"

    # 14 lb 体积重对应的实际形状：长边放到 18、高度放到 7.84（三边仍在 18×14×8 内）
    wide = (18.0, 13.78, 7.84)
    assert wide[0] * wide[1] * wide[2] / 139 == pytest.approx(13.99, abs=0.02)
    assert pkg.standard_edge_violation(wide) is None, "它仍在大号标准件里"

    klass, env = _package(conn, wide)
    assert klass.size_tiers == ("large_standard",)
    assert env.high_usd == pytest.approx(11.69, abs=0.005)
    assert env.high_usd - 11.35 == pytest.approx(0.34, abs=0.01)


def test_a_010_inch_tolerance_is_not_free_at_the_band_ceiling(conn):
    """「任一边超出 0.1 英寸以上才书面告知」——在档顶，0.1 英寸是 $0.17/件。

    而且在 208 oz 这条线上，0.01 英寸就已经跳档（上一条用例）。所以容差写 0.1
    不是「留一点余地」，是把 $0.17 白送出去；条款要么不给容差，要么写明由供方承担。
    """
    tol = (17.6, 13.88, 7.55)
    assert tol[0] * tol[1] * tol[2] == pytest.approx(1844.4, abs=0.5)

    _, sample = _package(conn, CURRENT_DIMS)
    klass, env = _package(conn, tol)
    assert klass.size_tiers == ("large_standard",)
    assert env.high_usd == pytest.approx(11.52, abs=0.005)
    assert env.high_usd - sample.high_usd == pytest.approx(0.17, abs=0.01)


def test_the_2_65_penalty_belongs_to_1401_not_to_a_01_inch_drift(conn):
    """$2.65 是「次长边超过 14.00」的价钱，不是「任一边超 0.1 英寸」的价钱。

    条款把两件事绑成一句（「超 0.1 英寸……差额费用（$2.65/件）由卖方承担」），
    后果是主张时被算据打回：0.1 英寸漂移实测 $0.17，只有 14.00→14.01 才是 $2.65。
    两种违约要分两条写，各自的金额才站得住。
    """
    _, drift = _package(conn, (17.6, 13.88, 7.55))          # 三边各 +0.1
    assert drift.high_usd == pytest.approx(11.52, abs=0.005)
    assert drift.high_usd - 11.35 == pytest.approx(0.17, abs=0.01)

    _, cliff = _package(conn, (17.52, 14.01, 7.48))          # 次长边过 14.00
    assert cliff.high_usd == pytest.approx(14.00, abs=0.01)
    assert cliff.high_usd - 11.35 == pytest.approx(2.65, abs=0.01)
