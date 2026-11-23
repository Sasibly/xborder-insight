"""`product_returns`（m0014）：逐 SKU 两档退货率落库。

为什么这张表值得一组专门的用例：
    它是全项目唯一把「读来的数」和「猜的数」并排放在一张表里的地方。
    净化器 12.6% 与咖啡机 10% 在算毛利时地位完全相同，可信度差一档；
    如果表结构与测试不区分，报告就会把假设写成事实——而报告是要拿去签字的。

这组用例分三层：
    1. 结构层：CHECK 真的挡得住（倒置、假设升 verified、ASIN 乱填）
    2. 语义层：draft 行必须列得出「下一步去哪儿取数」，不能空着显得已完成
    3. 一致性：库里的两档喂给准入，必须复现 `docs/04` §28.4 的 14.35% / 9.18%
"""

from __future__ import annotations

import sqlite3

import pytest

from xborder_insight.db import product_returns as pr
from xborder_insight.domain import admission
from xborder_insight.errors import ReturnRateMissing

PURIFIER = "purifier_desk_1752"
COFFEE = "coffee_semi_1613"


# ---------------------------------------------------------------- 结构层


def test_seed_rows_carry_the_documented_rates(conn):
    """种子的四个数与 `docs/04` §25 / §28 一致。改了口径这里会红。"""
    purifier = pr.require(conn, PURIFIER)
    assert purifier.baseline_rate == pytest.approx(0.126)
    assert purifier.stress_rate == pytest.approx(0.17)
    assert purifier.rate_gap_pts == pytest.approx(4.4, abs=0.05)

    coffee = pr.require(conn, COFFEE)
    assert coffee.baseline_rate == pytest.approx(0.10)
    assert coffee.stress_rate == pytest.approx(0.14)
    assert coffee.rate_gap_pts == pytest.approx(4.0, abs=0.05)


def test_both_seed_rows_are_draft_for_different_reasons(conn):
    """一行借类目阈值、一行是假设——「有数」不等于「核实过」。"""
    purifier = pr.require(conn, PURIFIER)
    coffee = pr.require(conn, COFFEE)
    assert purifier.status == pr.DRAFT and coffee.status == pr.DRAFT
    assert purifier.evidence_kind == "category_threshold"
    assert coffee.evidence_kind == "estimate"


def test_database_rejects_swapped_rates(conn):
    """压力档写倒必须在库层失败：静默按「压力=基线」算会得到永远为正的结论。

    应用层也校验（见 `test_upsert_rejects_swapped_rates`），这里是绕过应用层
    直接改库——约束写在表上，任何写入路径都逃不掉。
    """
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE product_returns SET stress_rate = 0.10 WHERE sku_key = ?", (PURIFIER,)
        )


def test_estimate_can_never_be_verified(conn):
    """假设升 verified 必须在库层失败，即使把来源三项都补齐。"""
    conn.execute(
        "UPDATE product_returns SET source_note = '后台路径', "
        "effective_date = '2026-11-23', verified_at = '2026-11-23' "
        "WHERE sku_key = ?",
        (COFFEE,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE product_returns SET status = 'verified' WHERE sku_key = ?", (COFFEE,)
        )


def test_borrowed_category_threshold_can_never_be_verified(conn):
    """借用类目阈值不是这一台的数：三项齐全也不许 verified。

    这是 2026-11-23 建表时实测发现的漏洞：第一版把净化器标成 `backend_read`，
    结果一行 draft 在台账上「什么都不差」——待办列表空着，实际什么都没落实。
    """
    purifier = pr.require(conn, PURIFIER)
    # 来源三项本来就是齐的，说明这次失败只可能来自证据类型
    assert purifier.source_note and purifier.effective_date and purifier.verified_at
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE product_returns SET status = 'verified' WHERE sku_key = ?", (PURIFIER,)
        )


def test_verified_requires_source_date_and_verify_date(conn):
    """三闸缺一不可：只给来源、不给日期，不许 verified。"""
    pr.upsert(
        conn,
        sku_key="kettle_test",
        product_name="电热水壶",
        baseline_rate=0.10,
        stress_rate=0.14,
        evidence_kind="backend_read",
        source_note="Seller Central → FBA Returns → 该 ASIN",
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE product_returns SET status = 'verified' WHERE sku_key = 'kettle_test'"
        )


def test_verified_is_reachable_for_a_real_sku_reading(conn):
    """反向用例：本 SKU 读数 + 三项齐全 → 可以 verified。

    只测「挡得住」不测「放得行」，约束迟早会被写死成一个什么都进不来的表。
    """
    pr.upsert(
        conn,
        sku_key="kettle_real",
        product_name="电热水壶",
        baseline_rate=0.10,
        stress_rate=0.14,
        evidence_kind="backend_read",
        source_note="Reports → FBA Returns → return rate 报表，该 ASIN 行",
        effective_date="2026-11-24",
        verified_at="2026-11-24",
        status="verified",
    )
    record = pr.require(conn, "kettle_real")
    assert record.verified and record.usable
    assert record.missing_evidence == ()


def test_asin_must_be_ten_characters(conn):
    with pytest.raises(sqlite3.IntegrityError):
        pr.upsert(
            conn,
            sku_key="bad_asin",
            product_name="乱填",
            baseline_rate=0.10,
            stress_rate=0.14,
            asin="B0DCZ",
        )


# ---------------------------------------------------------------- 语义层


def test_draft_rows_list_an_actionable_next_step(conn):
    """draft 行必须说得出「下一步去哪儿取数」。空着 = 看起来已完成。"""
    purifier = pr.require(conn, PURIFIER)
    assert len(purifier.missing_evidence) == 1
    assert "该 ASIN 的实际退货率" in purifier.missing_evidence[0]

    coffee = pr.require(conn, COFFEE)
    assert len(coffee.missing_evidence) == 4
    assert any("假设" in item for item in coffee.missing_evidence)


def test_usable_only_for_this_skus_own_reading(conn):
    """`usable` 回答的是「这个数是不是这一台的」，不是「有没有填数」。"""
    assert not pr.require(conn, PURIFIER).usable
    assert not pr.require(conn, COFFEE).usable


def test_coverage_separates_borrowed_from_guessed(conn):
    cov = pr.coverage(conn, "US")
    assert cov["total"] == 2
    assert cov["usable"] == 0
    assert cov["verified"] == 0
    assert cov["threshold"] == 1
    assert cov["estimate"] == 1
    assert cov["pending_keys"] == sorted([PURIFIER, COFFEE])


def test_upsert_updates_instead_of_duplicating(conn):
    """同一个 sku_key 写两次是更新——否则「这台到底几档」要 join 才知道。"""
    before = len(pr.list_all(conn))
    pr.upsert(
        conn,
        sku_key=COFFEE,
        product_name="咖啡机（半自动意式 16×13×9.5）",
        baseline_rate=0.09,
        stress_rate=0.16,
        asin="B0DCZHWNDJ",
    )
    assert len(pr.list_all(conn)) == before
    updated = pr.require(conn, COFFEE)
    assert updated.baseline_rate == pytest.approx(0.09)
    assert updated.stress_rate == pytest.approx(0.16)


def test_upsert_rejects_swapped_rates(conn):
    with pytest.raises(ValueError, match="写倒"):
        pr.upsert(
            conn,
            sku_key="swapped",
            product_name="写倒",
            baseline_rate=0.17,
            stress_rate=0.12,
        )


def test_upsert_rejects_promoting_an_estimate(conn):
    with pytest.raises(ValueError, match="不能升 verified"):
        pr.upsert(
            conn,
            sku_key="wishful",
            product_name="想升",
            baseline_rate=0.10,
            stress_rate=0.14,
            status="verified",
        )


def test_missing_sku_says_what_is_registered(conn):
    """报错要能照着修：列出可用 key，而不是只说「找不到」。"""
    with pytest.raises(ReturnRateMissing) as excinfo:
        pr.require(conn, "not_registered")
    assert PURIFIER in (excinfo.value.detail or "")
    assert COFFEE in (excinfo.value.detail or "")


# ---------------------------------------------------------------- 一致性


def test_database_rates_reproduce_the_signed_margin(conn):
    """库里两档喂给准入，必须复现 §28.4 的 14.35% / 9.18%。

    这是这张表存在的唯一理由：让 `make check` 与 API 取到同一组数。
    对不上说明表里的数和文档分叉了，而那种分叉在报告里看不出来。
    """
    record = pr.require(conn, PURIFIER)
    candidate = admission.Candidate(
        name="空气净化器",
        selling_price=79.99,
        quoted_cost=24.30,
        duty_rate=0.125,
        fba_fee=11.35,
        inbound_freight=6.00,
        referral_rate=0.15,
        return_rate=record.baseline_rate,
    )
    result = admission.stress_test(candidate, record.stress_rate)

    assert result.baseline_margin == pytest.approx(0.1435, abs=5e-4)
    assert result.stress_margin == pytest.approx(0.0918, abs=5e-4)
    assert result.margin_drop_pts == pytest.approx(5.17, abs=0.05)
    assert result.worth_negotiating, "两档都为正才值得谈——这是 §25.1 的规则"


def test_coffee_gap_narrows_with_the_baseline_but_the_verdict_is_set_by_the_baseline(conn):
    """咖啡机两档 10% → 14%（差 4 个点）。表只负责给数，判定交给 `stress_test`。

    20:45 把基线从 8% 改成 10%、压力档从 15% 改成 14% 之后，两档差从 7 收窄到 4——
    14% 不是独立读数，是随基线平移的情景值。所以这一台的风险不在压测翻转，
    而在基线本身：$14.50 在 10% 下只有 13.45%，**还没走到压力档就已经出局**。
    """
    record = pr.require(conn, COFFEE)
    assert record.stress_rate - record.baseline_rate == pytest.approx(0.04, abs=1e-6)

    candidate = admission.Candidate(
        name="coffee", selling_price=63.99, quoted_cost=15.70, duty_rate=0.237,
        fba_fee=11.85, inbound_freight=6.00, referral_rate=0.15,
        return_rate=record.baseline_rate,
    )
    stressed = admission.stress_test(candidate, record.stress_rate)
    assert stressed.baseline_margin == pytest.approx(0.1506, abs=5e-4)
    assert stressed.stress_margin == pytest.approx(0.1039, abs=5e-4)
    assert stressed.margin_drop_pts == pytest.approx(4.68, abs=0.05)
    assert stressed.worth_negotiating, "两档都为正——风险可承受，问题在基线只剩 0.06 个点"
