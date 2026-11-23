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


def _profit(cogs, price, fba, duty, *, freight=FREIGHT, referral=REFERRAL):
    return p.compute(
        p.CostInputs(
            quoted_price=cogs,
            selling_price=price,
            inbound_freight=freight,
            duty_rate=duty,
            referral_rate=referral,
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
        ("coffee_machine", "a", 71.34),
        ("electric_kettle", "a", 40.79),
        ("robot_vacuum", "a", 270.69),
        ("pet_fountain", "b", 52.40),
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
    assert got == pytest.approx(target_price, abs=0.01)


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


# ---------------------------------------------------------------------------
# §18 准入边界：把 15% 线化成一条不等式
#
# §17 逐台反解了采购上限，但那是「算出来」的。要拿它筛下一批候选，
# 得先确认三件事：①成本项分解是穷尽的（否则不等式漏项）；
# ②闭式解与数值解一致（否则文档里的表与代码算的不是一个东西）；
# ③「超预算」与「毛利缺口」是同一个数（否则读者要在两套单位之间换算）。
# ---------------------------------------------------------------------------

# 售价里不属于你的那部分：佣金 15% + 退款冲减 8% + 退款管理费 0.24% + 目标毛利 15%
ADMISSION_DENOMINATOR = 1 - 0.15 - 0.08 - 0.0024 - 0.15


def _sides(result, price):
    """把一张利润表拆成「进价侧 / 头程侧 / 平台侧」三段，用于检查分解是否穷尽。

    合并而不是逐项列，是因为它们各自带不同的系数：进价侧是 `1.024 + 税率`
    （关税按报价计、退货货损也是报价的 8%×30%），头程侧是 `1.016`
    （退货里 20% 的头程沉没）。合并之后每一项与售价之比才是可加的。
    """
    return {
        "cogs_side": (result.quoted_price + result.duty + result.returns.goods) / price,
        "freight_side": (result.freight + result.returns.inbound) / price,
        "referral": result.referral_fee / price,
        "reversal": result.returns.revenue_reversal / price,
        "admin": result.returns.refund_admin / price,
        "handling": result.returns.handling / price,
        "fba": result.fba_fee / price,
        "margin": result.gross_margin,
    }


def test_profit_decomposition_is_exhaustive(fees):
    """八段加起来必须正好是 100%——多一段就是重复计提，少一段就是漏计成本。

    这条与 §18 的不等式是一体两面：不等式只说「采购 + FBA + 头程 ≤ 61.76%」，
    而 61.76% 这个数只有在分解穷尽时才成立。若将来给模型加一项费用
    （比如仓储费、弃置费）却忘了改分母，这里会先失败，而不是等报价单上少算一笔。
    """
    for key, spec in SKU.items():
        label = "a" if spec["a"] else "b"
        _, env = fees[(key, label)]
        result = _profit(spec["cogs"], spec["price"], env.low_usd, spec["duty"])
        parts = _sides(result, spec["price"])
        assert sum(parts.values()) == pytest.approx(1.0, abs=1e-9), (
            f"{key}: 分解合计 {sum(parts.values()):.6f}，各项 {parts}"
        )


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("air_purifier", "a"),
        ("coffee_machine", "a"),
        ("electric_kettle", "a"),
        ("pet_fountain", "b"),
    ],
)
def test_admission_inequality_matches_the_solver(fees, key, label):
    """闭式解 `(采购×a + 头程×1.016 + $0.24 + FBA) ÷ 0.6176` 必须等于二分结果。

    这条守的是文档 §18.1 那条式子。它是这一节唯一可被外部复算的东西——
    读者拿计算器代进去，应当得到同一个售价。若模型将来新增一项费用而分母没改，
    闭式解会偏低，而二分仍是对的，两者的差就是这个漏项。
    """
    spec = SKU[key]
    _, env = fees[(key, label)]
    a = 1 + spec["duty"] + 0.08 * 0.30
    closed = (
        a * spec["cogs"] + 1.016 * FREIGHT + 0.08 * 3.0 + env.low_usd
    ) / ADMISSION_DENOMINATOR
    solved = _solve(0.15, spec["cogs"], env.low_usd, spec["duty"])
    assert closed == pytest.approx(solved, abs=0.02)


def test_refund_admin_cap_bends_the_closed_form(fees):
    """售价 ≥ $166.67 时闭式解偏高——退款管理费封顶 $5，不再随售价线性增长。

    扫地机是唯一撞线的（$199.99）：闭式解 $271.10，二分 $270.69，高 $0.41。
    这不影响结论，但它是一条必须写进文档的边界：拿这条式子去算高客单 SKU，
    得到的「所需售价」会偏高，方向是保守的（不会高估可行性），但不能说它精确。
    """
    spec = SKU["robot_vacuum"]
    _, env = fees[("robot_vacuum", "a")]
    a = 1 + spec["duty"] + 0.08 * 0.30
    closed = (
        a * spec["cogs"] + 1.016 * FREIGHT + 0.08 * 3.0 + env.low_usd
    ) / ADMISSION_DENOMINATOR
    solved = _solve(0.15, spec["cogs"], env.low_usd, spec["duty"])
    assert closed - solved == pytest.approx(0.41, abs=0.03)
    assert closed > solved  # 方向：封顶让闭式解偏高，不是偏低


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("air_purifier", "a"),
        ("coffee_machine", "a"),
        ("electric_kettle", "a"),
        ("robot_vacuum", "a"),
        ("pet_fountain", "b"),
    ],
)
def test_overrun_in_margin_points_equals_the_gap(fees, key, label):
    """「进价侧 + FBA 占售价」超出预算多少，毛利率就差多少点——同一个数。

    这条让 §18 的诊断表不必再给两套单位：超预算 22.64% 就是缺口 22.64 点。
    如果哪天两边的数对不上，说明分解里有项被算了两次或者漏了。

    扫地机是唯一例外：它的退款管理费撞了 $5 上限，而预算式里按「售价的 0.24%」
    近似，所以两边差 **0.04 点**（其余四台精确到 1e-6）。容差设 1e-3 是为了这一点，
    不是为了容错——若差距再大，就该改预算式而不是放宽容差。
    """
    spec = SKU[key]
    _, env = fees[(key, label)]
    price = spec["price"]
    result = _profit(spec["cogs"], price, env.low_usd, spec["duty"])
    parts = _sides(result, price)
    budget = ADMISSION_DENOMINATOR - (1.016 * FREIGHT + 0.08 * 3.0) / price
    overrun = (parts["cogs_side"] + parts["fba"]) - budget
    assert overrun == pytest.approx(0.15 - result.gross_margin, abs=1e-3)


@pytest.mark.parametrize(
    ("price", "fba_ratio", "cogs_cap_ratio"),
    [
        (30.0, 0.05, 0.3102),
        (30.0, 0.36, 0.0403),   # 水壶所在的那一格：采购只能占售价 4%
        (50.0, 0.25, 0.2096),   # 下一批候选的经验线
        (80.0, 0.25, 0.2510),
        (200.0, 0.36, 0.1966),
    ],
)
def test_cogs_cap_ratio_table(price, fba_ratio, cogs_cap_ratio):
    """§18.3 那张表的一格：给定售价与 FBA 占售价的比例，采购最多能占售价多少。

    这张表是给「还没量箱子的候选」用的——先按品类典型 FBA 估算，就能把
    采购报价直接筛掉。表里的数由公式算出，不是行业数据；用错这一步
    （比如拿品类均值代替单台）的代价见 `docs/04` §11.8。

    表固定按普货 12.5%（a=1.149）生成。逐台的税率不同时（咖啡机 23.7%），
    上限要再除以各自的 a——`docs/04` §18.3 第二张表给的就是那个数。
    """
    a = 1 + 0.125 + 0.08 * 0.30
    budget = ADMISSION_DENOMINATOR - (1.016 * FREIGHT + 0.08 * 3.0) / price
    cap = (budget - fba_ratio) / a
    assert cap == pytest.approx(cogs_cap_ratio, abs=5e-4)


@pytest.mark.parametrize(
    ("key", "label", "margin_at_8pct"),
    [
        ("coffee_machine", "a", 0.1502),
        ("pet_fountain", "b", 0.1350),
        ("electric_kettle", "a", -0.0012),
        ("air_purifier", "a", -0.0053),
        ("robot_vacuum", "a", 0.0015),
    ],
)
def test_referral_rate_swing_flips_only_the_coffee_machine(fees, key, label, margin_at_8pct):
    """佣金从 15% 换到 8%，五台里只有咖啡机越过 15%——而它正是唯一值得谈的那台。

    这条守的是 §18.6 的结论，也是下一步的排序依据：佣金差 7 个点，
    咖啡机的缺口正好 7.10 点，换成 8% 后是 15.02%。
    所以「查一次卖家后台的 fee category」（H1）与「向供应商还价 19.5%」（H21）
    是两条等价路径，而前者的成本低一个量级。

    15.02% 只比线高 0.02 个点，**不能算稳过**——它说明的是「这个参数的分量
    与一次成功议价相当」，不是「查一下就能做」。要下 GO 还得同时确认 H1 与 H20。
    """
    spec = SKU[key]
    _, env = fees[(key, label)]
    got = _profit(spec["cogs"], spec["price"], env.low_usd, spec["duty"], referral=0.08)
    assert got.gross_margin == pytest.approx(margin_at_8pct, abs=1e-4)


def test_only_one_sku_crosses_when_referral_drops_to_8pct(fees):
    """五台逐个过一遍：越过 15% 的必须恰好一台，否则 §18.6 的结论就错了。

    写成一条独立断言而不是藏在参数化里，是因为「只有一台」本身是结论——
    如果将来某次参数改动让两台同时越过，「唯一值得谈的那台」这个判断就失效了。
    """
    passed = []
    for key, spec in SKU.items():
        label = "a" if spec["a"] else "b"
        _, env = fees[(key, label)]
        margin = _profit(
            spec["cogs"], spec["price"], env.low_usd, spec["duty"], referral=0.08
        ).gross_margin
        if margin >= 0.15:
            passed.append(key)
    assert passed == ["coffee_machine"]


# --- §18.7 下一批候选的闸：$18.3 在哪些价位成立 -------------------------------

def _pmin_for_cogs(cogs, fba, duty, target=0.15):
    """采购价给定，解最低可行售价（闭式解，售价 < $166.67 时与模型逐分一致）。"""
    a = 1 + duty + RETURNS * 0.30
    fixed = 1.016 * FREIGHT + 0.24
    return (cogs * a + fixed + fba) / (1 - REFERRAL - RETURNS - 0.0024 - target)


@pytest.mark.parametrize(
    ("fba", "duty", "price_needed"),
    [
        (8.95, 0.125, 58.80),   # 小号大件下限 —— §18.7 的 $59 那道线就是它
        (10.86, 0.125, 61.89),  # 水壶档
        (14.40, 0.125, 67.62),  # 咖啡机档
        (23.84, 0.237, 86.22),  # 净化器 A + 23.7% 税
    ],
)
def test_18_3_needs_this_much_price(fba, duty, price_needed):
    """采购 $18.3 要卖到多少才够 15%——闸一的分段全由这张表推出来。

    这也是「$18.3 不等于便宜」的证据：它要求售价 $53–86。把 $18.3 当成
    「采购便宜的品」会放进一堆 $30–40 售价的候选，而那一档的上限只有 $1–12。
    """
    assert _pmin_for_cogs(18.30, fba, duty) == pytest.approx(price_needed, abs=0.02)


def test_18_3_is_loose_below_56_and_tight_above(fees):
    """$56 是 $18.3 的分界线：以下比数学上限宽 2.5 倍，以上只有它的 0.23 倍。

    这条守的是 §18.7 那张「宽严表」。若将来佣金或头程变了，分界点会移动，
    闸的分段也必须跟着移动——所以这里钉的是比值，不是「$56」这个字面值。
    """
    def cap_at(price):
        budget = ADMISSION_DENOMINATOR - (1.016 * FREIGHT + 0.24) / price
        return (budget - 0.125) / (1 + 0.125 + RETURNS * 0.30) * price

    assert 18.30 / cap_at(30.0) == pytest.approx(2.49, abs=0.02)
    assert 18.30 / cap_at(200.0) == pytest.approx(0.23, abs=0.01)
    # 分界点本身：$18.3 恰好等于该售价的上限
    assert cap_at(56.0) == pytest.approx(18.30, abs=0.35)

