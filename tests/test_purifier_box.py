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
