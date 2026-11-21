"""五台横向实算（`docs/04` §16–§17）：把「哪台为什么不能做」的每个数钉进出门禁。

§16 第一次出现「五台全 NO_GO」这个结论，而它同时依赖三样会变的东西：
逐台的 HTS 税率、包装尺寸（现在有两套）、以及费率表。任何一样改了，
结论都可能翻转，所以这里不许写死单个百分比就完事——每条都要能从库里复算。

§17 是 §16 的反解：不问「现在多少」，问「要多低才能到 15%」。反解出来的数
比正算更容易被一次参数改动悄悄改变（它是解，不是观测值），所以同样必须钉住。
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


def _profit(cogs, price, fba, duty, *, freight=FREIGHT):
    return p.compute(
        p.CostInputs(
            quoted_price=cogs,
            selling_price=price,
            inbound_freight=freight,
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


def _solve_cogs(target, price, fba, duty, *, freight=FREIGHT, low=0.01, high=500.0):
    """采购价上限：毛利随采购价单调递减，所以二分是安全的。

    刻意不用闭式解：产品售价 ≥$166.67 时退款管理费会撞上 $5 上限
    （`profit.REFUND_ADMIN_CAP_USD`），闭式解会把上限当线性项，扫地机因此偏 $0.08。
    解要不依赖「目标 SKU 恰好没撞上限」这件事。
    """
    for _ in range(200):
        mid = (low + high) / 2
        if _profit(mid, price, fba, duty, freight=freight).gross_margin < target:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def _solve_freight(target, cogs, price, fba, duty):
    """头程上限。返回 None 表示「头程归零也到不了」——这比返回负数诚实。"""
    if _profit(cogs, price, fba, duty, freight=0.0).gross_margin < target:
        return None
    low, high = 0.0, 200.0
    for _ in range(200):
        mid = (low + high) / 2
        if _profit(cogs, price, fba, duty, freight=mid).gross_margin < target:
            high = mid
        else:
            low = mid
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


# ---------------------------------------------------------------------------
# §17 缺口反解：要多低才能做到 15%
#
# §16 只回答了「现在亏多少」。谈判桌上要的是另一件事：**采购价压到多少才够**。
# 反解出来的是「解」而不是「观测值」，它随任何一个入参变动而整体平移，
# 所以每个解都要在门禁里留一条——否则文档里的目标价会在参数改动后变成废纸，
# 而没有任何测试会失败。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "label", "cogs_ceiling"),
    [
        ("air_purifier", "a", 16.74),
        ("air_purifier", "b", 27.60),
        ("coffee_machine", "a", 14.90),
        ("electric_kettle", "a", 1.13),
        ("robot_vacuum", "a", 79.85),
        ("pet_fountain", "b", 11.05),
    ],
)
def test_cogs_ceiling_for_15pct(fees, key, label, cogs_ceiling):
    """采购价上限：高于这个数，这台在现价上就不可能到 15%。

    这个数比「毛利率」好用，理由是它是**对外的语言**：跟供应商谈的是单价，
    不是毛利率。把它算出来之后，「能不能做」就变成「他肯不肯降到这个价」，
    而不是一轮又一轮地重算模型。

    上限都远低于现价——最接近的咖啡机也要 −19.5%，最远的水壶要 −83%。
    所以「谈采购价」对四台都不成立，只有咖啡机勉强算得上一场真正的谈判。
    """
    spec = SKU[key]
    _, env = fees[(key, label)]
    got = _solve_cogs(0.15, spec["price"], env.low_usd, spec["duty"])
    assert got == pytest.approx(cogs_ceiling, abs=0.02)
    # 解的自我校验：把解代回去，毛利率必须正好落在 15%（不是「差不多 15%」）
    back = _profit(got, spec["price"], env.low_usd, spec["duty"]).gross_margin
    assert back == pytest.approx(0.15, abs=1e-6)


def test_kettle_cogs_ceiling_is_below_its_own_packaging(fees):
    """水壶要活下来，采购价必须降到 $1.13——比它的彩盒还便宜。

    这条测试的意义是**把「不现实」写成数字**：$6.80 → $1.13 是 −83%，
    不是「再谈谈」的量级。水壶真正的问题是 FBA 占售价 36%（小号大件），
    不是采购价高。所以它的出路只有涨价（$40.79，+36%）或换尺寸段，
    去压供应商是选错了杠杆。
    """
    spec = SKU["electric_kettle"]
    _, env = fees[("electric_kettle", "a")]
    ceiling = _solve_cogs(0.15, spec["price"], env.low_usd, spec["duty"])
    assert ceiling < spec["cogs"] * 0.2


@pytest.mark.parametrize(
    ("key", "label", "freight_ceiling"),
    [
        ("coffee_machine", "a", 1.53),
        ("pet_fountain", "b", 2.10),
    ],
)
def test_freight_alone_can_still_flip_two_skus(fees, key, label, freight_ceiling):
    """咖啡机与饮水机的缺口，单靠头程就能补上——但门槛低到不现实。

    咖啡机头程要 ≤ $1.53/件，饮水机 ≤ $2.10/件，而海运 LCL 单件分摊通常在
    $5–10。所以这条不是「有希望」，而是**把最后一条杠杆也排除掉**：
    排除之后，这两台剩下的路径与另外三台是同一件事——涨价或换供应链。
    """
    spec = SKU[key]
    _, env = fees[(key, label)]
    got = _solve_freight(0.15, spec["cogs"], spec["price"], env.low_usd, spec["duty"])
    assert got == pytest.approx(freight_ceiling, abs=0.02)


@pytest.mark.parametrize("key", ["air_purifier", "electric_kettle", "robot_vacuum"])
def test_freight_is_a_dead_lever_for_three_skus(fees, key):
    """三台的头程归零也到不了 15%——所以「等货代回价」对它们不是关键路径。

    净化器头程归零仍是 −0.02%（缺口 22.6 点 > 头程最大贡献 7.6 点）；
    水壶归零是 13.09%，差 1.9 点，最接近但仍不够；扫地机归零是 −3.87%。

    这条要守的是**优先级**：如果三台的结论已经不取决于头程，那把询价单
    当下一步就是排错了顺序。
    """
    spec = SKU[key]
    label = "a" if spec["a"] else "b"
    _, env = fees[(key, label)]
    got = _solve_freight(0.15, spec["cogs"], spec["price"], env.low_usd, spec["duty"])
    assert got is None
    zero = _profit(spec["cogs"], spec["price"], env.low_usd, spec["duty"], freight=0.0)
    assert zero.gross_margin < 0.15


def test_purchasing_leverage_differs_sevenfold_across_skus(fees):
    """采购价每降 $1，水壶补 3.92 点、扫地机只补 0.57 点——差 6.9 倍。

    所以「统一去谈 10% 折扣」这种策略是错的：同一个 10% 对水壶是补 2.7 点，
    对扫地机只补 6.7 点，而对扫地机需要补的缺口是 21.9 点。
    杠杆效率差这么多，是因为分母不同（毛利率的分母是售价），不是采购价高低。
    """
    pts = {}
    for key, spec in SKU.items():
        label = "a" if spec["a"] else "b"
        _, env = fees[(key, label)]
        base = _profit(spec["cogs"], spec["price"], env.low_usd, spec["duty"])
        one_less = _profit(spec["cogs"] - 1.0, spec["price"], env.low_usd, spec["duty"])
        pts[key] = one_less.gross_margin - base.gross_margin
    assert pts["electric_kettle"] == pytest.approx(0.0392, abs=5e-4)
    assert pts["robot_vacuum"] == pytest.approx(0.0057, abs=5e-4)
    assert pts["electric_kettle"] / pts["robot_vacuum"] > 6.5


@pytest.mark.parametrize(
    ("key", "label", "margin"),
    [
        ("air_purifier", "a", -0.076437),
        ("coffee_machine", "a", 0.079034),
        ("electric_kettle", "a", -0.072345),
        ("robot_vacuum", "a", -0.069196),
        ("pet_fountain", "b", 0.063898),
    ],
)
def test_reported_margins_are_not_double_rounded(fees, key, label, margin):
    """毛利率由「未舍入的毛利 ÷ 售价」得出，不能先把毛利舍到分再除。

    旧文档写的是 −7.65% / −7.24% / 6.38%，那是把 −$6.12 ÷ $79.99 算出来的——
    分母用了未舍入值、分子用了舍入值。误差只有 1 个基点，但后果是
    「文档里的数字」与「测试算出来的数字」对不上，而后者才是能复现的那个。
    四舍五入方向反了会让一次对账变成一场争论。
    """
    spec = SKU[key]
    _, env = fees[(key, label)]
    got = _profit(spec["cogs"], spec["price"], env.low_usd, spec["duty"]).gross_margin
    assert got == pytest.approx(margin, abs=1e-5)


def test_price_and_cogs_levers_trade_at_half(fees):
    """售价每涨 $1，采购价上限只上移 $0.49–0.54——不是 1:1。

    这条防的是「涨 $1 售价就能抵 $1 采购」的直觉：佣金 15% 与退款冲减 8%
    会把涨价的三成先拿走，剩下的才落到毛利里。做报价方案时按 1:1 换算是
    系统性乐观。
    """
    for key, spec in SKU.items():
        label = "a" if spec["a"] else "b"
        _, env = fees[(key, label)]
        at_now = _solve_cogs(0.15, spec["price"], env.low_usd, spec["duty"])
        at_plus5 = _solve_cogs(0.15, spec["price"] + 5.0, env.low_usd, spec["duty"])
        per_dollar = (at_plus5 - at_now) / 5.0
        assert 0.48 < per_dollar < 0.55, f"{key}: 售价 $1 换 {per_dollar:.2f} 采购上限"
