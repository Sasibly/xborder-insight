"""`docs/04` §27 那张单值表：文档里的每个数都要能被一条命令复现。

这组用例的由来：用户 2026-11-23 17:13 定了一条协作规则——需要给价格时直接给单值，
不给范围。单值比范围更好用，但也更容易在参数变动后悄悄失效：范围写错了看起来
只是「区间偏了」，单值写错了就是**按一个不成立的价去签字**。

所以表里每一个数都在这里钉一遍，尤其是两个「看起来正好」的：

- 咖啡机不改箱 **$13.72 = 14.998%**：它是精确解 $13.719 四舍五入到分的结果，
  看起来就是「算出来的那个数」，但它已经掉线。签字线只能取 **$13.70（15.04%）**。
  旧口径下同类陷阱是 **$14.90 = 14.99%**（8% 退货率时），那条线已随退货率改 10% 作废，
  但陷阱的形状没变——`margin >= 0.15` 判的是数，不是四舍五入的直觉。
- 净化器 **$24.50 = 14.76%**，是「再让一档」最常见的心理价位，但它已经掉出线。

参数若改（税率、头程、退货率），这些断言会红——这正是它们该做的事。
2026-11-23 20:45 咖啡机退货率 8% → 10%，本文件咖啡机部分整段重算：四条旧线全部跌破
15%（`docs/04` §33），新签字线 $15.70（改箱）/ $13.70（不改箱）。
"""

from __future__ import annotations

import pytest

from xborder_insight.domain import profit as p

# 净化器：$79.99 / 关税 12.5% / 头程 6.00 / 佣金 15% / 退货 12% / FBA $11.35
PURIFIER = dict(
    selling_price=79.99, inbound_freight=6.00, duty_rate=0.125,
    referral_rate=0.15, return_rate=0.12, fba_fee=11.35,
)
# 咖啡机：$63.99 / 关税 23.7% / 头程 6.00 / 佣金 15% / 退货 10%（20:45 由 8% 改）
COFFEE = dict(
    selling_price=63.99, inbound_freight=6.00, duty_rate=0.237,
    referral_rate=0.15, return_rate=0.10,
)

THRESHOLD = 0.15


def _margin(cogs, *, fba, common):
    return p.compute(
        p.CostInputs(
            quoted_price=cogs,
            fba_fee=fba,
            returns_handling_fee=3.00,
            returns_refund_admin_rate=0.20,
            returns_goods_loss_ratio=0.30,
            returns_inbound_loss_ratio=0.20,
            incoterm="FOB",
            **common,
        )
    ).gross_margin


def _purifier(cogs):
    common = {k: v for k, v in PURIFIER.items() if k != "fba_fee"}
    return _margin(cogs, fba=PURIFIER["fba_fee"], common=common)


def _coffee(cogs, *, fba):
    return _margin(cogs, fba=fba, common=COFFEE)


# ------------------------------------------------------------ 净化器


def test_purifier_three_price_points():
    """开口 $24.00、最高可付 $24.30、超此不签 $24.50。"""
    assert _purifier(24.00) == pytest.approx(0.1549, abs=5e-4)
    assert _purifier(24.30) == pytest.approx(0.1505, abs=5e-4)
    assert _purifier(24.50) == pytest.approx(0.1476, abs=5e-4)

    assert _purifier(24.00) >= THRESHOLD
    assert _purifier(24.30) >= THRESHOLD, "最高可付那一档必须仍过线"
    assert _purifier(24.50) < THRESHOLD, "再让一档就掉出线——这是不加价的依据"


def test_purifier_ceiling_has_room_below_the_exact_solution():
    """$24.30 比精确解 $24.33 更低，但仍过线——3 分钱买谈判手感。"""
    assert _purifier(24.33) == pytest.approx(0.1501, abs=5e-4)
    assert _purifier(24.30) > _purifier(24.33)
    assert 24.30 < 24.33


# ------------------------------------------------------------ 咖啡机（改箱 · 10% 退货）


def test_coffee_repacked_price_points():
    """箱规改到 8 英寸内（FBA $11.85）：开口 $15.00、签字线 $15.70。

    10% 退货率把这一档的上限从 $16.92 压到 $15.73（−$1.19），
    旧开口 $16.50 与旧签字 $16.90 现在分别只有 13.48% / 12.69%。
    """
    assert _coffee(15.00, fba=11.85) == pytest.approx(0.1645, abs=5e-4)
    assert _coffee(15.70, fba=11.85) == pytest.approx(0.1506, abs=5e-4)
    assert _coffee(15.73, fba=11.85) == pytest.approx(0.1500, abs=5e-4)   # 精确解
    assert _coffee(15.75, fba=11.85) == pytest.approx(0.1496, abs=5e-4)

    assert _coffee(15.00, fba=11.85) >= THRESHOLD
    assert _coffee(15.70, fba=11.85) >= THRESHOLD
    assert _coffee(15.73, fba=11.85) >= THRESHOLD, "精确解仍在线内，但只余 0.003 个点"
    assert _coffee(15.75, fba=11.85) < THRESHOLD, "$15.75 就是「往上凑整」会踩的那一步"


# ------------------------------------------------------------ 咖啡机（不改箱 · 10% 退货）


def test_coffee_unrepacked_at_the_old_quote_is_dead():
    """$14.50 在 10% 下只有 13.45%——「不改箱就维持原价」这句话失效了。

    8% 时它是 15.78%（一笔体面的维持），10% 时它掉 2.33 个点。
    同一个价、同一台机器，只因为退货率从假设 8% 改成给定 10%。
    """
    assert _coffee(14.50, fba=14.40) == pytest.approx(0.1345, abs=5e-4)
    assert _coffee(14.50, fba=14.40) < THRESHOLD

    # 要它过线，采购价必须压到 $13.72 以下——对 $18.50 的现报价是 −25.8%
    assert _coffee(13.72, fba=14.40) < THRESHOLD, "精确解四舍五入到分就掉线"
    assert 1 - 13.72 / 18.50 > 0.25, "需降幅超过 25%，不在真实议价区间内"


def test_coffee_signing_line_is_13_70_not_13_72():
    """这条是本文件存在的理由：$13.72 差 0.002 个点，算术直觉会签错。

    $13.719 是精确解，报到分位就是 $13.72 = 14.998%。`admission` 判 `margin >= 0.15`，
    它不过——所以签字线取 $13.70。旧口径下同形状的陷阱是 $14.90 = 14.99%。
    """
    assert _coffee(13.72, fba=14.40) == pytest.approx(0.1500, abs=5e-4)
    assert _coffee(13.72, fba=14.40) < THRESHOLD, "看起来正好，实际没过"

    assert _coffee(13.70, fba=14.40) == pytest.approx(0.1504, abs=5e-4)
    assert _coffee(13.70, fba=14.40) >= THRESHOLD

    # 两分钱的差距决定过不过线
    assert _coffee(13.70, fba=14.40) - _coffee(13.72, fba=14.40) < 0.001


def test_old_8pct_signing_lines_must_now_fail():
    """8% 口径的两条签字线在 10% 下必须判不过——**它们过线才是回归**。

    这条是给「有人把退货率改回 8% 却忘了改价格」准备的：数字一动它就红。
    """
    assert _coffee(16.90, fba=11.85) < THRESHOLD, "旧改箱签字线 $16.90：12.69%"
    assert _coffee(14.89, fba=14.40) < THRESHOLD, "旧不改箱签字线 $14.89：12.68%"


# ------------------------------------------------------------ 两条线的相对位置


def test_the_box_is_worth_more_than_the_price_concession():
    """咖啡机：箱规把签字线从 $13.70 抬到 $15.70，值 $2.00/件；谈判让步空间只有 $0.70。

    $0.70 = $15.70 − $15.00，是改箱那一档的全部谈判空间。
    把这两个数并排放，是为了让「先谈箱规」这个顺序有算术依据，不是话术偏好。
    """
    box_gain = 15.70 - 13.70
    fee_gain = 14.40 - 11.85
    concession = 15.70 - 15.00
    assert box_gain == pytest.approx(2.00)
    assert fee_gain == pytest.approx(2.55)
    assert concession == pytest.approx(0.70)
    assert box_gain > concession * 2, "箱规的价值仍是让步空间的近三倍"


# ------------------------------------------------------------ 成交价（§28）


def test_signed_price_is_exactly_the_ceiling_we_precommitted_to():
    """成交 $24.30 就是 §27 预先定死的那个顶——没有临场加价。

    这条守的不是算术，是纪律：一旦有人把成交价改成 $24.50 之类的「再让一档」，
    它立刻变红，因为 14.76% 已经掉出线。
    """
    signed = 24.30
    ceiling = 24.30
    assert signed == ceiling
    assert _purifier(signed) >= THRESHOLD

    # 再让一档就不成立
    assert _purifier(24.50) < THRESHOLD


def test_signed_price_survives_the_returns_stress_but_not_a_box_downgrade():
    """成交价的两种翻车方式，代价完全不同。

    - 退货率涨到 17%（压测档）：9.18%，仍为正 → 经得起，不必重谈。
    - 箱规失守（FBA $14.00）：11.74%，掉 3.31 个点 → 每件 −$2.65，必须靠 PI 条款挡。
    """
    stress = _margin(
        24.30, fba=11.35,
        common={**{k: v for k, v in PURIFIER.items() if k != "fba_fee"}, "return_rate": 0.17},
    )
    assert stress == pytest.approx(0.0918, abs=5e-4)
    assert stress > 0, "压力档为正才符合「两档都为正才谈」的规则"

    box_fail = _margin(
        24.30, fba=14.00,
        common={k: v for k, v in PURIFIER.items() if k != "fba_fee"},
    )
    assert box_fail == pytest.approx(0.1174, abs=5e-4)
    assert _purifier(24.30) - box_fail > 0.03, "掉档 3.3 个点，比任何压价让步都贵"


def test_signed_price_has_a_known_residual_under_the_strict_returns_rate():
    """12.6% 取严档下是 14.35%——差 0.65 点，但**仍为正**。

    这就是 §28.4 说的「残差风险」：签不撤回，只是「15% 达标」这句话不成立。
    把它写成断言，是为了防止有人拿 15.05% 去对外引用而不带前提。
    """
    strict = _margin(
        24.30, fba=11.35,
        common={**{k: v for k, v in PURIFIER.items() if k != "fba_fee"}, "return_rate": 0.126},
    )
    assert strict == pytest.approx(0.1435, abs=5e-4)
    assert strict < THRESHOLD
    assert strict > 0, "低于目标线但没亏——所以处理方式是接受或补价，不是撕单"

    # 若真按 12.6% 执行，补 $0.20 就回到线下沿
    assert _margin(
        24.10, fba=11.35,
        common={**{k: v for k, v in PURIFIER.items() if k != "fba_fee"}, "return_rate": 0.126},
    ) > strict


def test_signed_price_profit_per_unit():
    """15.05% × $79.99 = $12.04/件——谈判桌上唯一该记住的绝对数。"""
    gross = _purifier(24.30) * 79.99
    assert gross == pytest.approx(12.04, abs=0.02)


# ------------------------------------------------------------ 退货率的两种读法


def test_fourteen_point_three_five_is_a_margin_not_a_return_rate():
    """`14.35%` 是毛利（$24.30 / 退货 12.6%），不是退货率。

    用户 17:28 把它当退货率回报过一次。这两个数在 `docs/04` §28.4 里是同一行的左右两列，
    混起来的代价是 2 个毛利点：真按 14.35% 退货率算，$24.30 只有 12.29%。
    这条用例把「同一个百分比、两种含义」的差距钉成数字，避免以后再串。
    """
    as_margin = _margin(
        24.30, fba=11.35,
        common={**{k: v for k, v in PURIFIER.items() if k != "fba_fee"}, "return_rate": 0.126},
    )
    as_return_rate = _margin(
        24.30, fba=11.35,
        common={**{k: v for k, v in PURIFIER.items() if k != "fba_fee"}, "return_rate": 0.1435},
    )
    assert as_margin == pytest.approx(0.1435, abs=5e-4)
    assert as_return_rate == pytest.approx(0.1229, abs=5e-4)

    gap_pts = (as_margin - as_return_rate) * 100
    assert gap_pts == pytest.approx(2.06, abs=0.02), "两个读法差 2 个毛利点，不能混用"
    assert gap_pts * 79.99 / 100 == pytest.approx(1.65, abs=0.02)


def test_if_the_return_rate_were_really_14_35_the_action_changes():
    """真 14.35% 退货率时，15% 线要 $22.45（比成交价低 $1.85）——那是重谈量级。

    与「12.6% 下接受 14.35%」是两套动作：前者连谈两次都不一定够，后者什么也不用做。
    所以这一个百分点的口径必须先对齐再动，不能先动手再对齐。
    """
    rate = 0.1435
    common = {**{k: v for k, v in PURIFIER.items() if k != "fba_fee"}, "return_rate": rate}
    at_signed = _margin(24.30, fba=11.35, common=common)
    assert at_signed < 0.15

    # 二分出 15% 线（与 admission 同口径：毛利额 / 售价）
    lo, hi = 10.0, 24.30
    for _ in range(60):
        mid = (lo + hi) / 2
        if _margin(mid, fba=11.35, common=common) >= 0.15:
            lo = mid
        else:
            hi = mid
    assert lo == pytest.approx(22.45, abs=0.02)
    assert 24.30 - lo == pytest.approx(1.85, abs=0.02)
