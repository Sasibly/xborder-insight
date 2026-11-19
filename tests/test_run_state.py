"""运行台账与状态机回归测试。

两个断言层次：
- 应用层：状态机不允许回退（succeeded 不能再变 failed）
- 数据层：即使绕开状态机直接 UPDATE，库的 CHECK 也会拦住「终态没结束时间」

为什么两层都要测：应用层保证正常路径正确，数据层保证以后新增的写入方
（任务执行器、运维脚本、手改库）也逃不掉。只测一层等于把正确性寄托在自觉上。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from xborder_insight.db import runs
from xborder_insight.errors import InvalidStateTransition


def test_create_run_starts_in_running(conn):
    runs.create_run(conn, "run-1", "wireless_charger")
    row = runs.get_run(conn, "run-1")
    assert row["status"] == runs.RUNNING
    assert row["started_at"]
    assert row["finished_at"] is None


def test_finish_run_records_metrics(conn):
    runs.create_run(conn, "run-1", "wireless_charger")
    runs.finish_run(
        conn,
        "run-1",
        review_count=22,
        parsed_count=22,
        parse_success_rate=1.0,
        llm_calls=22,
        prompt_tokens=600,
        completion_tokens=900,
        latency_p95_ms=24.0,
        params_snapshot={"amazon.referral_rate": 0.15},
    )
    row = runs.get_run(conn, "run-1")
    assert row["status"] == runs.SUCCEEDED
    assert row["finished_at"] is not None
    assert row["review_count"] == 22
    # 快照必须落盘，否则历史 run 的账会跟着参数一起变
    assert json.loads(row["params_snapshot"]) == {"amazon.referral_rate": 0.15}


def test_fail_run_records_error_code(conn):
    runs.create_run(conn, "run-1", "wireless_charger")
    runs.fail_run(conn, "run-1", "upstream_error")
    row = runs.get_run(conn, "run-1")
    assert row["status"] == runs.FAILED
    assert row["error_code"] == "upstream_error"


def test_terminal_state_cannot_be_repeated(conn):
    runs.create_run(conn, "run-1", "wireless_charger")
    runs.finish_run(
        conn, "run-1", review_count=1, parsed_count=1, parse_success_rate=1.0,
        llm_calls=1, prompt_tokens=1, completion_tokens=1,
    )
    with pytest.raises(InvalidStateTransition, match="succeeded → succeeded"):
        runs.finish_run(
            conn, "run-1", review_count=2, parsed_count=2, parse_success_rate=1.0,
            llm_calls=2, prompt_tokens=2, completion_tokens=2,
        )


def test_cannot_fail_after_success(conn):
    runs.create_run(conn, "run-1", "wireless_charger")
    runs.finish_run(
        conn, "run-1", review_count=1, parsed_count=1, parse_success_rate=1.0,
        llm_calls=1, prompt_tokens=1, completion_tokens=1,
    )
    with pytest.raises(InvalidStateTransition):
        runs.fail_run(conn, "run-1", "late_failure")
    assert runs.get_run(conn, "run-1")["status"] == runs.SUCCEEDED


def test_cannot_succeed_after_failure(conn):
    runs.create_run(conn, "run-1", "wireless_charger")
    runs.fail_run(conn, "run-1", "boom")
    with pytest.raises(InvalidStateTransition):
        runs.finish_run(
            conn, "run-1", review_count=1, parsed_count=1, parse_success_rate=1.0,
            llm_calls=1, prompt_tokens=1, completion_tokens=1,
        )


def test_get_missing_run_raises(conn):
    with pytest.raises(KeyError):
        runs.get_run(conn, "nope")


# ---------- 数据层兜底 ----------


def test_db_rejects_terminal_status_without_finished_at(conn):
    """绕开状态机直接改 status，CHECK 仍然会拦住「成功了但没有结束时间」。"""
    runs.create_run(conn, "run-1", "wireless_charger")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE runs SET status = 'succeeded' WHERE run_id = 'run-1'")


def test_db_rejects_unknown_status(conn):
    runs.create_run(conn, "run-1", "wireless_charger")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE runs SET status = 'done' WHERE run_id = 'run-1'")


def test_db_rejects_impossible_parse_rate(conn):
    runs.create_run(conn, "run-1", "wireless_charger")
    runs.fail_run(conn, "run-1", "x")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE runs SET parse_success_rate = 1.5 WHERE run_id = 'run-1'"
        )


# ---------- 成本归因 ----------


def test_token_totals_only_counts_succeeded(conn):
    for run_id, status in (("ok-1", "ok"), ("ok-2", "ok"), ("bad-1", "bad")):
        runs.create_run(conn, run_id, "wireless_charger")
        if status == "ok":
            runs.finish_run(
                conn, run_id, review_count=10, parsed_count=10, parse_success_rate=1.0,
                llm_calls=10, prompt_tokens=100, completion_tokens=200,
            )
        else:
            runs.fail_run(conn, run_id, "boom")

    rows = runs.token_totals(conn)
    assert len(rows) == 1
    assert rows[0]["runs"] == 2  # 失败的 run 不进成本口径
    assert rows[0]["total_tokens"] == 600
    assert rows[0]["llm_calls"] == 20


def test_token_totals_empty_is_empty(conn):
    assert runs.token_totals(conn) == []
