"""五台横向实算（`docs/04` §16）：把「哪台为什么不能做」的每个数钉进出门禁。

§16 第一次出现「五台全 NO_GO」这个结论，而它同时依赖三样会变的东西：
逐台的 HTS 税率、包装尺寸（现在有两套）、以及费率表。任何一样改了，
结论都可能翻转，所以这里不许写死单个百分比就完事——每条都要能从库里复算。
"""

from __future__ import annotations

from datetime import date

import pytest

from xborder_insight.db import fba as fba_db
from xborder_insight.domain import package as pkg
from xborder_insight.domain import profit as p

SHIP_DATE = date(2026, 11, 21)
REFERRAL = 0.15
FREIGHT = 6.00
RETURNS = 0.08

# A = 项目里已固化的实测包装（measured_skus.json）
# B = 11-21 17:20 询价单上给的那一套（`docs/07` H19 说两套不可能同时对）
SKU = {
    "air_purifier": dict(
        price=79.99, cogs=32.50, duty=0.125, lithium=False,
        a=((24.0, 15.0, 15.0), 18.5), b=((13.78, 7.48, 17.52), 9.26),
    ),
    "coffee_machine": dict(
        price=63.99, cogs=18.50, duty=0.237, lithium=False,
        a=((16.0, 13.0, 9.5), 14.5), b=((16.0, 13.0, 9.5), 15.98),
    ),
    "electric_kettle": dict(
        price=29.99, cogs=6.80, duty=0.152, lithium=False,
        a=((10.5, 9.2, 8.4), 4.8), b=((6.69, 6.69, 3.74), 3.09),
    ),
    "robot_vacuum": dict(
        price=199.99, cogs=118.00, duty=0.125, lithium=True,
        a=((22.0, 18.0, 14.0), 16.2), b=((17.9, 13.3, 23.2), 31.0),
    ),
    "pet_fountain": dict(
        price=45.99, cogs=14.50, duty=0.125, lithium=True,
        a=None, b=((9.0, 8.0, 10.0), 2.8),
    ),
}


def _fee(conn, dims, weight_lb, *, lithium, price):
    facts = pkg.PackageFacts(
        has_lithium_battery=lithium,
        unit_weight_oz=weight_lb * 16.0,
        dims_in=dims,
    )
    klass, env = fba_db.envelope_for_package(
        conn, "US", facts, selling_price=price, ship_date=SHIP_DATE, exact_weight=True
    )
    return klass, env


def _profit(cogs, price, fba, duty):
    return p.compute(
        p.CostInputs(
            quoted_price=cogs,
            selling_price=price,
            inbound_freight=FREIGHT,
            duty_rate=duty,
            referral_rate=REFERRAL,
            fba_fee=fba,
            return_rate=RETURNS,
            returns_handling_fee=3.0,
            returns_refund_admin_rate=0.20,
            returns_goods_loss_ratio=0.30,
            returns_inbound_loss_ratio=0.20,
            incoterm="FOB",
        )
    )


def _solve(target, cogs, fba, duty):
    low, high = 10.0, 800.0
    for _ in range(90):
        mid = (low + high) / 2
        if _profit(cogs, mid, fba, duty).gross_margin < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2


@pytest.fixture
def fees(conn):
    out = {}
    for key, spec in SKU.items():
        for label in ("a", "b"):
            spec_set = spec[label]
            if spec_set is None:
                continue
            dims, lb = spec_set
            klass, env = _fee(
                conn, dims, lb, lithium=spec["lithium"], price=spec["price"]
            )
            out[(key, label)] = (klass, env)
    return out


def test_every_sku_is_no_go_at_the_current_price(fees):
    """五台在现价上全部达不到 15%——这是 §16.3 的主结论。

    之所以要一条测试守住它，是因为这个结论**容易被一次参数改动悄悄翻转**：
    把关税按 3.7% 算、或者把 FBA 换成便宜的尺寸段，净化器就会从 −7.65%
    变成 +7.97%，而报告里还写着「全 NO_GO」。
    """
    for key, spec in SKU.items():
        _, env = fees[(key, "a" if spec["a"] else "b")]
        r = _profit(spec["cogs"], spec["price"], env.low_usd, spec["duty"])
        assert r.gross_margin < 0.15, f"{key} 竟然过了 15% 线：{r.gross_margin:.2%}"


def test_per_sku_duty_must_not_be_flattened_to_one_rate(fees):
    """把 23.7% 套到五台上，会把净化器的毛利从 −7.65% 压到 −12.20%。

    高估和低估一样有害：低估让人做不该做的生意，高估让人放弃本来能做的。
    所以税率必须逐台带（H18 定归类之前，用建议编码的口径）。
    """
    spec = SKU["air_purifier"]
    _, env = fees[("air_purifier", "a")]
    real = _profit(spec["cogs"], spec["price"], env.low_usd, 0.125)
    flat = _profit(spec["cogs"], spec["price"], env.low_usd, 0.237)
    assert real.gross_margin - flat.gross_margin == pytest.approx(0.0455, abs=5e-4)


def test_the_two_dimension_sets_disagree_on_the_size_tier(fees):
    """净化器与水壶在 B 那套里掉一个尺寸段（小号大件 → 大号标准件）。

    这不是「四舍五入」级别的差别：净化器 FBA 差 $12.48（19.7% 的售价），
    水壶差 $3.00（10% 的售价）。而扫地机的 A 是 16.2 lb、B 是 31 lb——
    包装只会比裸机重，所以两组不可能都是同一台的包装（`docs/07` H19）。
    """
    purifier_a, fee_a = fees[("air_purifier", "a")]
    purifier_b, fee_b = fees[("air_purifier", "b")]
    assert {t.size_tier for t in purifier_a.tiers} == {"small_bulky"}
    assert {t.size_tier for t in purifier_b.tiers} == {"large_standard"}
    assert fee_a.low_usd - fee_b.low_usd == pytest.approx(12.48, abs=0.02)

    kettle_a, fee_ka = fees[("electric_kettle", "a")]
    kettle_b, fee_kb = fees[("electric_kettle", "b")]
    assert {t.size_tier for t in kettle_a.tiers} == {"small_bulky"}
    assert {t.size_tier for t in kettle_b.tiers} == {"large_standard"}
    assert fee_ka.low_usd - fee_kb.low_usd == pytest.approx(3.00, abs=0.02)


def test_robot_vacuum_fee_is_insensitive_to_the_two_sets(fees):
    """扫地机两套尺寸差很多，FBA 却一模一样——因为计费重量都顶到 40 lb。

    这条是反例：不是所有 SKU 都受 H19 影响，所以 H19 不能一刀切说
    「先按 A 算，反正都要重算」——扫地机重算也是同一个数。
    """
    _, fee_a = fees[("robot_vacuum", "a")]
    _, fee_b = fees[("robot_vacuum", "b")]
    assert fee_a.low_usd == pytest.approx(fee_b.low_usd, abs=1e-9)
    assert fee_a.low_usd == pytest.approx(25.51, abs=0.01)


def test_cogs_ratio_is_what_kills_the_robot_vacuum(fees):
    """扫地机采购价占售价 59%，即使关税降到 0 也进不了 15% 线。

    这条防的是「以为关税是主要问题」——政策再怎么变，这台的结构性缺口
    都在采购价上，不是税率上。
    """
    _, env = fees[("robot_vacuum", "a")]
    zero_duty = _profit(SKU["robot_vacuum"]["cogs"], 199.99, env.low_usd, 0.0)
    assert zero_duty.gross_margin < 0.15


@pytest.mark.parametrize(
    ("key", "label", "target_price"),
    [
        ("air_purifier", "a", 109.32),
        ("air_purifier", "b", 89.10),
        ("coffee_machine", "a", 71.35),
        ("electric_kettle", "a", 40.79),
        ("robot_vacuum", "a", 270.69),
        ("pet_fountain", "b", 52.41),
    ],
)
def test_price_needed_for_15pct(fees, key, label, target_price):
    """15% 线所需售价——给定价用的锚，不是「市场最优价」的建议。

    项目纪律不爬竞品前台，所以这些数只能回答「卖这个价才够 15%」，
    回答不了「市场愿不愿意付这个价」。
    """
    spec = SKU[key]
    _, env = fees[(key, label)]
    got = _solve(0.15, spec["cogs"], env.low_usd, spec["duty"])
    assert got == pytest.approx(target_price, abs=0.05)
