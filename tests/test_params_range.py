"""适用区间（applies_min / applies_max）的行为测试。

这一层的存在理由只有一个：**费率是函数，不是常数**。
佣金 15% 只对前 $100 成立，退款管理费有 $5 上限，FBA 费随尺寸重量跳档。
把这些条件写在 note 的自然语言里，机器读不到——售价 $150 的商品会静默
套用「$100 以下」的费率，不报错、不警告，结论看起来完全正常。

所以这里测的不是「功能有没有」，而是**错的那条路是否被堵住**：
越界调用必须失败，区间写反必须进不了库，不传取值也必须失败。
"""

from __future__ import annotations

import sqlite3

import pytest

from xborder_insight.db import params
from xborder_insight.errors import ParamOutOfRange, ParamUnverified

MARKET = "US"
KEY = "amazon.referral_rate"


def _verify(conn, *, low=None, high=None, value=0.15):
    params.mark_verified(
        conn,
        KEY,
        MARKET,
        value=value,
        source_url="https://example.invalid/fee-schedule",
        effective_date="2026-01-01",
        verified_at="2026-11-21",
        applies_min=low,
        applies_max=high,
    )


# ---------- 迁移层：列存在且默认为空 ----------


def test_range_columns_default_to_null(conn):
    """种子参数不应凭空带上区间——NULL 表示「该参数与取值无关」。"""
    param = params.find(conn, KEY, MARKET)
    assert param is not None
    assert param.applies_min is None
    assert param.applies_max is None
    assert param.has_range is False
    assert param.range_label == "全区间"


# ---------- 应用层：区间内放行 ----------


def test_require_passes_inside_range(conn):
    _verify(conn, low=0, high=100)
    assert params.require(conn, KEY, MARKET, applied_to=29.99).value == pytest.approx(0.15)


def test_range_bounds_are_inclusive(conn):
    """区间是闭区间。$100 恰好是分段边界，边界值必须能算，
    否则「$100 以内 15%」在 $100 时反而报错，与官方表述不符。"""
    _verify(conn, low=0, high=100)
    assert params.require(conn, KEY, MARKET, applied_to=100).value == pytest.approx(0.15)
    assert params.require(conn, KEY, MARKET, applied_to=0).value == pytest.approx(0.15)


def test_open_ended_range_only_needs_one_side(conn):
    """只有下限的区间（如「$100 以上 8%」）应可用。"""
    _verify(conn, low=100, value=0.08)
    assert params.require(conn, KEY, MARKET, applied_to=500).value == pytest.approx(0.08)
    with pytest.raises(ParamOutOfRange):
        params.require(conn, KEY, MARKET, applied_to=50)


# ---------- 应用层：错的那条路必须失败 ----------


def test_require_rejects_value_above_range(conn):
    _verify(conn, low=0, high=100)
    with pytest.raises(ParamOutOfRange) as exc:
        params.require(conn, KEY, MARKET, applied_to=150)
    assert "0 – 100" in exc.value.message
    assert "150" in exc.value.message
    assert exc.value.code == "param_out_of_range"


def test_require_rejects_value_below_range(conn):
    _verify(conn, low=100, high=200)
    with pytest.raises(ParamOutOfRange):
        params.require(conn, KEY, MARKET, applied_to=99.99)


def test_require_refuses_when_value_not_given(conn):
    """有区间却不给取值，等于「不知道是否适用但照样用」。

    这条最容易被人情化地放行（「反正大部分情况都在区间内」），
    而它正是区间机制唯一要防的东西：静默用错值。
    """
    _verify(conn, low=0, high=100)
    with pytest.raises(ParamOutOfRange) as exc:
        params.require(conn, KEY, MARKET)
    assert "未提供被评估的取值" in exc.value.message


def test_require_keeps_working_without_range(conn):
    """没有区间的参数（如 returns.rate）不要求传 applied_to，保持向后兼容。"""
    _verify(conn, value=0.08)
    assert params.require(conn, KEY, MARKET).value == pytest.approx(0.08)


# ---------- 数据层：写反的区间进不了库 ----------


def test_db_rejects_inverted_range(conn):
    """区间写反必须当场失败，而不是等到调用时才发现所有取值都越界。"""
    with pytest.raises(sqlite3.IntegrityError):
        params.set_applies_range(conn, KEY, MARKET, applies_min=100, applies_max=0)


def test_db_rejects_negative_range(conn):
    with pytest.raises(sqlite3.IntegrityError):
        params.set_applies_range(conn, KEY, MARKET, applies_min=-1, applies_max=100)


def test_db_rejects_inverted_range_on_insert(conn):
    """绕过 set_applies_range 直接 UPDATE，同样被 CHECK 拦住。"""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE cost_params SET applies_min = 200, applies_max = 100 WHERE param_key = ?",
            (KEY,),
        )


# ---------- 写入语义 ----------


def test_mark_verified_preserves_existing_range(conn):
    """省略区间参数表示「保持原值」，不是「清空」。

    否则「先设区间、后补来源」这种自然顺序会把区间冲掉，
    而冲掉之后 require() 就不再校验——是静默降级。
    """
    params.set_applies_range(conn, KEY, MARKET, applies_min=0, applies_max=100)
    _verify(conn, value=0.15)
    param = params.find(conn, KEY, MARKET)
    assert param.applies_min == 0
    assert param.applies_max == 100


def test_set_applies_range_can_clear(conn):
    params.set_applies_range(conn, KEY, MARKET, applies_min=0, applies_max=100)
    params.set_applies_range(conn, KEY, MARKET, applies_min=None, applies_max=None)
    assert params.find(conn, KEY, MARKET).has_range is False


def test_mark_verified_on_missing_param_raises(conn):
    """写不存在的参数要报错，不能静默影响 0 行。"""
    with pytest.raises(ParamUnverified):
        params.mark_verified(
            conn,
            "no.such_key",
            MARKET,
            value=1.0,
            source_url="https://example.invalid/x",
            effective_date="2026-01-01",
            verified_at="2026-11-21",
        )
