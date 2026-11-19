"""任务层回归测试：幂等、租约、状态机、执行器。

这里的断言集中在三类「不会报错但会算错」的问题上：
1. 幂等键相同但请求体不同 → 必须报错，不能返回旧任务（否则跑的是老参数）
2. 租约过期的 running → 必须能回收（否则任务永久卡死，且看起来在跑）
3. 重试 → 必须记成新的一次尝试，而不是覆盖上一次的账
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from xborder_insight.errors import IdempotencyConflict, InvalidStateTransition, JobNotFound
from xborder_insight.jobs import models, store
from xborder_insight.jobs.handlers import JobContext, JobOutcome
from xborder_insight.jobs.runner import JobRunner

DECISION_PAYLOAD = {
    "asin": "B0TEST0001",
    "category": "wireless_charger",
    "marketplace": "US",
    "opportunity_score": 0.66,
    "cost": {"quoted_price": 8.0, "selling_price": 29.99, "referral_rate": 0.15},
}


def _submit(conn, job_id="job-1", **kwargs):
    return store.submit(
        conn,
        job_id=job_id,
        kind=kwargs.pop("kind", models.KIND_DECISION),
        payload=kwargs.pop("payload", DECISION_PAYLOAD),
        **kwargs,
    )


# ---------- 提交与幂等 ----------


def test_submit_creates_queued_job(conn):
    row, created = _submit(conn, idempotency_key="k1")
    assert created is True
    assert row["status"] == models.QUEUED
    assert row["attempts"] == 0
    assert row["started_at"] is None
    assert row["lease_expires_at"] is None


def test_submit_records_version_triple(conn):
    """版本三元组必须落库，否则「同一版本重跑结果一致」无法验证。"""
    row, _ = _submit(conn)
    assert row["prompt_version"].startswith("prompt-")
    assert row["taxonomy_version"].startswith("tax-")
    assert row["model"]


def test_same_key_same_payload_returns_same_job(conn):
    first, created_1 = _submit(conn, job_id="job-1", idempotency_key="k1")
    second, created_2 = _submit(conn, job_id="job-2", idempotency_key="k1")

    assert created_1 is True
    assert created_2 is False
    assert first["job_id"] == second["job_id"] == "job-1"
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1


def test_same_key_different_payload_is_conflict(conn):
    """键相同、内容不同必须 409。

    若返回旧任务，调用方会以为新参数已被接受，实际跑的是老参数——
    结论错了但全程没有报错。
    """
    _submit(conn, idempotency_key="k1")
    with pytest.raises(IdempotencyConflict):
        _submit(
            conn,
            job_id="job-2",
            idempotency_key="k1",
            payload={**DECISION_PAYLOAD, "opportunity_score": 0.9},
        )


def test_fingerprint_ignores_key_order(conn):
    """同样内容写成不同键顺序，指纹必须一致，否则正常重发会被误判成冲突。"""
    _submit(conn, job_id="job-1", idempotency_key="k1", payload={"a": 1, "b": 2})
    row, created = _submit(conn, job_id="job-2", idempotency_key="k1", payload={"b": 2, "a": 1})
    assert created is False
    assert row["job_id"] == "job-1"


def test_no_key_creates_separate_jobs(conn):
    _submit(conn, job_id="job-1")
    _submit(conn, job_id="job-2")
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 2


def test_same_key_different_tenant_does_not_collide(conn):
    """唯一索引建在 (tenant_id, key) 上：租户之间不该互相占键。"""
    _submit(conn, job_id="job-1", idempotency_key="shared", tenant_id="tenant-a")
    _, created = _submit(conn, job_id="job-2", idempotency_key="shared", tenant_id="tenant-b")
    assert created is True


def test_unknown_kind_rejected(conn):
    with pytest.raises(ValueError):
        _submit(conn, kind="teleport")


# ---------- 领取与租约 ----------


def test_claim_sets_running_and_lease(conn):
    _submit(conn)
    row = store.claim(conn, "w-1", lease_seconds=60)
    assert row is not None
    assert row["status"] == models.RUNNING
    assert row["attempts"] == 1
    assert row["worker_id"] == "w-1"
    assert row["started_at"] and row["lease_expires_at"]


def test_claim_empty_queue_returns_none(conn):
    assert store.claim(conn, "w-1", lease_seconds=60) is None


def test_claim_is_fifo(conn):
    _submit(conn, job_id="job-a")
    _submit(conn, job_id="job-b")
    first = store.claim(conn, "w-1", lease_seconds=60)
    second = store.claim(conn, "w-1", lease_seconds=60)
    assert (first["job_id"], second["job_id"]) == ("job-a", "job-b")


def test_claim_twice_does_not_double_claim(conn):
    """第二次领取必须拿不到：已 running 的任务不能被再领一次。"""
    _submit(conn)
    assert store.claim(conn, "w-1", lease_seconds=60) is not None
    assert store.claim(conn, "w-2", lease_seconds=60) is None


def test_extend_lease_only_for_owner(conn):
    _submit(conn)
    store.claim(conn, "w-1", lease_seconds=60)
    assert store.extend_lease(conn, "job-1", "w-2", lease_seconds=60) is False
    assert store.extend_lease(conn, "job-1", "w-1", lease_seconds=60) is True


def test_requeue_expired_recycles_stale_running(conn):
    """租约过期的 running 必须能回到队列——这是「重启后不永久卡死」的唯一保证。"""
    _submit(conn)
    store.claim(conn, "w-1", lease_seconds=60)
    result = store.requeue_expired(conn, now="2999-01-01T00:00:00.000")

    assert result["requeued"] == ["job-1"]
    row = store.get(conn, "job-1")
    assert row["status"] == models.QUEUED
    # 上一轮的开始时间必须清掉，否则耗时统计会把两段拼在一起
    assert row["started_at"] is None
    assert row["worker_id"] is None


def test_requeue_expired_keeps_fresh_lease(conn):
    _submit(conn)
    store.claim(conn, "w-1", lease_seconds=600)
    assert store.requeue_expired(conn)["requeued"] == []
    assert store.get(conn, "job-1")["status"] == models.RUNNING


def test_requeue_expired_fails_when_budget_spent(conn):
    """预算耗尽时判死而不是留在 running：留在 running 的任务永远不会被处理，
    却看起来「正在运行」，比明确失败更难发现。"""
    _submit(conn, max_attempts=1)
    store.claim(conn, "w-1", lease_seconds=60)
    result = store.requeue_expired(conn, now="2999-01-01T00:00:00.000")

    assert result["failed"] == ["job-1"]
    row = store.get(conn, "job-1")
    assert row["status"] == models.FAILED
    assert row["error_code"] == "lease_expired"


# ---------- 完成 / 失败 / 重试 ----------


def test_complete_records_result_and_usage(conn):
    _submit(conn)
    store.claim(conn, "w-1", lease_seconds=60)
    store.complete(
        conn, "job-1", result={"verdict": "NO_GO"}, llm_calls=3, prompt_tokens=10,
        completion_tokens=20,
    )
    row = store.get(conn, "job-1")
    assert row["status"] == models.SUCCEEDED
    assert row["finished_at"]
    assert row["worker_id"] is None  # 结束即释放租约，否则会被别人当成"还在跑"
    assert store.to_public(row)["usage"]["total_tokens"] == 30


def test_terminal_cannot_complete_twice(conn):
    _submit(conn)
    store.claim(conn, "w-1", lease_seconds=60)
    store.complete(conn, "job-1", result={})
    with pytest.raises(InvalidStateTransition, match="succeeded → succeeded"):
        store.complete(conn, "job-1", result={})


def test_fail_records_error_code(conn):
    _submit(conn)
    store.claim(conn, "w-1", lease_seconds=60)
    store.fail(conn, "job-1", "upstream_error")
    row = store.get(conn, "job-1")
    assert row["status"] == models.FAILED
    assert row["error_code"] == "upstream_error"


def test_retry_keeps_attempts_and_requeues(conn):
    _submit(conn)
    store.claim(conn, "w-1", lease_seconds=60)
    store.fail(conn, "job-1", "boom")

    row = store.retry(conn, "job-1")
    assert row["status"] == models.QUEUED
    assert row["attempts"] == 1  # 次数不重置：掩盖次数等于掩盖「它一直失败」
    assert row["error_code"] is None
    assert row["finished_at"] is None


def test_retry_succeeded_is_rejected(conn):
    _submit(conn)
    store.claim(conn, "w-1", lease_seconds=60)
    store.complete(conn, "job-1", result={})
    with pytest.raises(InvalidStateTransition):
        store.retry(conn, "job-1")


def test_retry_when_budget_spent_raises_budget(conn):
    """自动重试的次数上限是成本保护；人工重试是显式决定，因此抬高预算，
    而不是绕过库里的 `attempts <= max_attempts` 约束。"""
    _submit(conn, max_attempts=1)
    store.claim(conn, "w-1", lease_seconds=60)
    store.fail(conn, "job-1", "boom")

    row = store.retry(conn, "job-1")
    assert row["max_attempts"] == 2
    assert store.claim(conn, "w-1", lease_seconds=60) is not None


def test_get_missing_job_raises(conn):
    with pytest.raises(JobNotFound):
        store.get(conn, "nope")


# ---------- 数据层兜底 ----------


def test_db_rejects_running_without_lease(conn):
    """绕开 store 直接改 status，库也必须拦住「running 但没有租约」。"""
    _submit(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET status = 'running' WHERE job_id = 'job-1'")


def test_db_rejects_over_budget_attempts(conn):
    _submit(conn, max_attempts=2)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET attempts = 3 WHERE job_id = 'job-1'")


def test_db_rejects_result_on_queued_job(conn):
    _submit(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET result = '{}' WHERE job_id = 'job-1'")


def test_db_rejects_failed_without_error_code(conn):
    _submit(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE jobs SET status = 'running', started_at = 'x', "
                     "lease_expires_at = 'x' WHERE job_id = 'job-1'")
        conn.execute("UPDATE jobs SET status = 'failed', finished_at = 'x' WHERE job_id = 'job-1'")


# ---------- 成本归因 ----------


def test_usage_totals_only_counts_succeeded(conn):
    for job_id, ok in (("job-1", True), ("job-2", True), ("job-3", False)):
        _submit(conn, job_id=job_id)
        store.claim(conn, "w-1", lease_seconds=60)
        if ok:
            store.complete(conn, job_id, result={}, llm_calls=5, prompt_tokens=10,
                           completion_tokens=20)
        else:
            store.fail(conn, job_id, "boom")

    rows = store.usage_totals(conn)
    assert len(rows) == 1
    assert rows[0]["jobs"] == 2
    assert rows[0]["total_tokens"] == 60


# ---------- 执行器 ----------


class _FakeClient:
    name = "fake"


def _runner(conn, handlers):
    from contextlib import nullcontext

    from xborder_insight.config import settings

    return JobRunner(
        session=lambda: nullcontext(conn),
        client=_FakeClient(),  # type: ignore[arg-type]
        settings=settings,
        handlers=handlers,
        worker_id="w-test",
        lease_seconds=60,
    )


def test_runner_completes_job(conn):
    async def handler(payload, ctx: JobContext) -> JobOutcome:
        assert ctx.job_id == "job-1"
        return JobOutcome(result={"ok": True}, llm_calls=2, prompt_tokens=5, completion_tokens=7)

    _submit(conn)
    runner = _runner(conn, {models.KIND_DECISION: handler})
    assert asyncio.run(runner.drain()) == 1

    row = store.get(conn, "job-1")
    assert row["status"] == models.SUCCEEDED
    assert row["llm_calls"] == 2


def test_runner_marks_failure_and_retry_succeeds(conn):
    """失败要落 error_code；重试是**新的一次尝试**，attempts 递增而不是覆盖。"""
    calls = {"n": 0}

    async def flaky(payload, ctx: JobContext) -> JobOutcome:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一次必失败")
        return JobOutcome(result={"ok": True})

    _submit(conn)
    runner = _runner(conn, {models.KIND_DECISION: flaky})
    asyncio.run(runner.drain())

    failed = store.get(conn, "job-1")
    assert failed["status"] == models.FAILED
    assert failed["error_code"] == "internal_error"

    store.retry(conn, "job-1")
    asyncio.run(runner.drain())

    done = store.get(conn, "job-1")
    assert done["status"] == models.SUCCEEDED
    assert done["attempts"] == 2


def test_runner_unknown_kind_fails_fast(conn):
    """不认识的类型必须立刻判死，否则它会被反复领取、反复失败，占住队列。"""
    _submit(conn, kind=models.KIND_DECISION)
    runner = _runner(conn, {})  # 空注册表
    asyncio.run(runner.drain())
    row = store.get(conn, "job-1")
    assert row["status"] == models.FAILED
    assert row["error_code"] == "unknown_kind"
