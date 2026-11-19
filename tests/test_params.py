"""参数表回归测试：这里是「不许编数」这条原则的代码化。

三个层次各拦一道：
1. 应用层 —— `require()` 遇到 draft 就抛 ParamUnverified，不做默认值回落
2. 数据层 —— CHECK 约束让「verified 但没来源」的写入直接被数据库拒绝
3. 交付层 —— 种子数据必须全是 draft，防止有人为了演示好看偷偷塞数字

第 3 条尤其重要：种子数据一旦带 verified 值，整个项目的可信度就没了，
因为那些数字没有出处，而页面上看不出来。
"""

from __future__ import annotations

import sqlite3

import pytest

from xborder_insight.db import params
from xborder_insight.errors import ParamUnverified

MARKET = "US"


# ---------- 交付层：种子数据必须诚实 ----------


def test_seeded_params_are_all_draft(conn):
    """预置参数一律 draft。要让它们变成 verified，必须人工补官方来源。"""
    seeded = params.list_params(conn, MARKET)
    assert seeded, "种子数据不应为空，否则等于没声明要核实什么"
    assert all(p.status == params.DRAFT for p in seeded)
    assert all(p.value is None for p in seeded)
    assert all(p.source_url is None for p in seeded)


def test_seeded_params_explain_what_to_verify(conn):
    """每个 draft 参数都要有 note，说明该去查什么，否则下一个人不知道从哪下手。"""
    for param in params.list_params(conn, MARKET):
        assert param.note, f"{param.param_key} 缺少核实说明"


# ---------- 应用层：未核实不许用 ----------


def test_require_raises_on_draft(conn):
    with pytest.raises(ParamUnverified) as exc:
        params.require(conn, "amazon.referral_rate", MARKET)
    assert "未核实" in exc.value.message


def test_require_raises_on_missing(conn):
    """连记录都没有时同样报错，不能返回 None 让上层去猜。"""
    conn.execute("DELETE FROM cost_params WHERE param_key = 'amazon.referral_rate'")
    with pytest.raises(ParamUnverified, match="缺少参数"):
        params.require(conn, "amazon.referral_rate", MARKET)


def test_require_returns_value_after_verification(conn):
    params.mark_verified(
        conn,
        "amazon.referral_rate",
        MARKET,
        value=0.15,
        source_url="https://example.invalid/fee-schedule",
        effective_date="2026-01-01",
        verified_at="2026-11-17",
    )
    param = params.require(conn, "amazon.referral_rate", MARKET)
    assert param.value == pytest.approx(0.15)
    assert param.usable is True
    assert param.source_url


# ---------- 数据层：让约束不可绕过 ----------


def test_db_rejects_verified_without_source(conn):
    """绕开应用层直接 UPDATE，也必须被 CHECK 拦住。"""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE cost_params SET status = 'verified', value = 0.15 "
            "WHERE param_key = 'amazon.referral_rate'"
        )


def test_db_rejects_verified_with_partial_provenance(conn):
    """来源有了但缺生效日期，同样不算核实。三项缺一不可。"""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE cost_params SET status = 'verified', value = 0.15, "
            "source_url = 'https://example.invalid/x', verified_at = '2026-11-17' "
            "WHERE param_key = 'amazon.referral_rate'"
        )


def test_db_rejects_verified_with_null_value(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE cost_params SET status = 'verified', "
            "source_url = 'https://example.invalid/x', "
            "effective_date = '2026-01-01', verified_at = '2026-11-17' "
            "WHERE param_key = 'amazon.referral_rate'"
        )


def test_db_rejects_unknown_unit(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cost_params (param_key, marketplace, unit, status, created_at) "
            "VALUES ('x.y', 'US', 'percent', 'draft', '2026-11-17T00:00:00')"
        )


def test_compliance_rule_also_requires_source(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE compliance_rules SET status = 'verified' "
            "WHERE rule_key = 'us.radio.equipment_authorization'"
        )


# ---------- 快照与进度 ----------


def test_snapshot_excludes_unverified(conn):
    assert params.snapshot(conn, MARKET) == {}
    params.mark_verified(
        conn,
        "amazon.referral_rate",
        MARKET,
        value=0.15,
        source_url="https://example.invalid/fee-schedule",
        effective_date="2026-01-01",
        verified_at="2026-11-17",
    )
    assert params.snapshot(conn, MARKET) == {"amazon.referral_rate": 0.15}


def test_coverage_reports_honest_numbers(conn):
    info = params.coverage(conn, MARKET)
    assert info["total"] == info["verified"] + info["draft"]
    assert info["verified"] == 0
    assert "amazon.referral_rate" in info["missing_keys"]


def test_compliance_rules_hide_draft_by_default(conn):
    assert params.compliance_rules(conn, "wireless_charger", MARKET) == []
    drafts = params.compliance_rules(conn, "wireless_charger", MARKET, include_draft=True)
    assert len(drafts) >= 1
    assert all(row["status"] == params.DRAFT for row in drafts)
