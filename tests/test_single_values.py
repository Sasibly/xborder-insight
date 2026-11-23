"""`docs/04` §27 那张单值表：文档里的每个数都要能被一条命令复现。

这组用例的由来：用户 2026-11-23 17:13 定了一条协作规则——需要给价格时直接给单值，
不给范围。单值比范围更好用，但也更容易在参数变动后悄悄失效：范围写错了看起来
只是「区间偏了」，单值写错了就是**按一个不成立的价去签字**。

所以表里每一个数都在这里钉一遍，尤其是两个「看起来正好」的：

- 咖啡机不改箱 **$14.90 = 14.99%**，差 0.01 个点，`margin >= 0.15` 判不过。
  签字线只能取 **$14.89（15.01%）**。四舍五入的直觉在这里会签错。
- 净化器 **$24.50 = 14.76%**，是「再让一档」最常见的心理价位，但它已经掉出线。

参数若改（税率、头程、退货率），这些断言会红——这正是它们该做的事。
"""

from __future__ import annotations

import pytest

from xborder_insight.domain import profit as p

# 净化器：$79.99 / 关税 12.5% / 头程 6.00 / 佣金 15% / 退货 12% / FBA $11.35
PURIFIER = dict(
    selling_price=79.99, inbound_freight=6.00, duty_rate=0.125,
    referral_rate=0.15, return_rate=0.12, fba_fee=11.35,
)
# 咖啡机：$63.99 / 关税 23.7% / 头程 6.00 / 佣金 15% / 退货 8%
COFFEE = dict(
    selling_price=63.99, inbound_freight=6.00, duty_rate=0.237,
    referral_rate=0.15, return_rate=0.08,
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


# ------------------------------------------------------------ 咖啡机（改箱）


def test_coffee_repacked_price_points():
    """箱规改到 8 英寸内（FBA $11.85）：开口 $16.50、签字线 $16.90。"""
    assert _coffee(16.50, fba=11.85) == pytest.approx(0.1582, abs=5e-4)
    assert _coffee(16.90, fba=11.85) == pytest.approx(0.1504, abs=5e-4)
    assert _coffee(17.00, fba=11.85) == pytest.approx(0.1484, abs=5e-4)

    assert _coffee(16.50, fba=11.85) >= THRESHOLD
    assert _coffee(16.90, fba=11.85) >= THRESHOLD
    assert _coffee(17.00, fba=11.85) < THRESHOLD, "$17.00 是整数心理价，但它不过线"


# ------------------------------------------------------------ 咖啡机（不改箱）


def test_coffee_unrepacked_opening_is_simply_the_current_quote():
    """不改箱时开口 $14.50 = 现报价，含义是「维持原价，不再压」。"""
    assert _coffee(14.50, fba=14.40) == pytest.approx(0.1578, abs=5e-4)
    assert _coffee(14.50, fba=14.40) >= THRESHOLD


def test_coffee_signing_line_is_14_89_not_14_90():
    """这条是本文件存在的理由：$14.90 差 0.01 个点，四舍五入的直觉会签错。

    `admission` 判的是 `margin >= 0.15`，14.99% 就是不过。签字线只能取 $14.89。
    """
    assert _coffee(14.90, fba=14.40) == pytest.approx(0.1499, abs=5e-4)
    assert _coffee(14.90, fba=14.40) < THRESHOLD, "看起来正好，实际没过"

    assert _coffee(14.89, fba=14.40) == pytest.approx(0.1501, abs=5e-4)
    assert _coffee(14.89, fba=14.40) >= THRESHOLD

    # 一分钱的差距决定过不过线
    assert _coffee(14.89, fba=14.40) - _coffee(14.90, fba=14.40) < 0.0005


# ------------------------------------------------------------ 两条线的相对位置


def test_the_box_is_worth_more_than_the_price_concession():
    """咖啡机：箱规值 $2.55/件，压价最多值 $0.40——所以筹码全押在箱规上。

    $0.40 = $16.90 − $16.50，是改箱那一档的全部谈判空间。
    把这两个数并排放，是为了让「先谈箱规」这个顺序有算术依据，不是话术偏好。
    """
    box_gain = 16.90 - 16.50
    fee_gain = 14.40 - 11.85
    assert box_gain == pytest.approx(0.40)
    assert fee_gain == pytest.approx(2.55)
    assert fee_gain > box_gain * 6
