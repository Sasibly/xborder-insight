"""结果归档的回归测试。

这一组测试守的是一条容易在重构中被「优化掉」的性质：

    归档只清 `result`，不删行。删行会让幂等键一起消失，
    同一个 `Idempotency-Key` 重放会变成一条新任务——重新花一次钱，
    而调用方看到的重放响应和正常重放一模一样，发现不了。

所以第一个用例断言的不是「归档了多少行」，而是「归档之后重放仍然返回同一个 job_id」。
"""

from __future__ import annotations

import json

import pytest

from xborder_insight.jobs import models, store

TENANT = "t-archive"
OLD = "2020-01-01T00:00:00.000"


def _finished_job(conn, job_id: str, *, key: str | None = None, finished_at: str = OLD) -> str:
    """造一条已成功、且带结果的任务。全程走状态机，不直接写 status。"""
    idem = key or f"key-{job_id}"
    row, _ = store.submit(
        conn,
        job_id=job_id,
        kind=models.KIND_ANALYZE,
        payload={"dataset": "wireless_charger", "top_k": 3},
        tenant_id=TENANT,
        idempotency_key=idem,
    )
    assert str(row["status"]) == models.QUEUED
    store.claim(conn, "worker-1", lease_seconds=60)
    store.complete(conn, job_id, result={"themes": [{"label": "heat"}], "review_count": 3})
    # 完成时间必须回拨：归档的判据是「完成多久了」，不是「提交多久了」。
    conn.execute("UPDATE jobs SET finished_at = ? WHERE job_id = ?", (finished_at, job_id))
    return idem


def test_archive_clears_result_but_keeps_row_and_idempotency(conn):
    """归档后：行还在、result 空了、幂等键仍然生效。"""
    key = _finished_job(conn, "job-a")
    before = store.get(conn, "job-a")
    assert before["result"] is not None

    result = store.archive_finished(conn, older_than_days=30, dry_run=False)
    assert result["candidates"] == 1
    assert result["archived"] == ["job-a"]

    after = store.get(conn, "job-a")
    assert after["result"] is None, "归档应清空 result"
    assert after["archived_at"] is not None
    # 任务行的其余部分必须原样保留：这是「归档」与「删除」的分界线
    assert after["status"] == models.SUCCEEDED
    assert after["finished_at"] == before["finished_at"]
    assert after["prompt_tokens"] == before["prompt_tokens"]

    # 关键断言：同一个幂等键重放，必须仍然命中这条任务，而不是新建一条
    replayed, created = store.submit(
        conn,
        job_id="job-should-not-exist",
        kind=models.KIND_ANALYZE,
        payload={"dataset": "wireless_charger", "top_k": 3},
        tenant_id=TENANT,
        idempotency_key=key,
    )
    assert created is False, "归档把幂等键弄丢了：重放变成了新任务，会重新花钱"
    assert replayed["job_id"] == "job-a"


def test_dry_run_changes_nothing(conn):
    _finished_job(conn, "job-b")
    result = store.archive_finished(conn, older_than_days=30, dry_run=True)
    assert result["candidates"] == 1
    assert result["archived"] == []
    assert result["dry_run"] is True

    row = store.get(conn, "job-b")
    assert row["result"] is not None, "预演不得改动数据"
    assert row["archived_at"] is None


def test_only_old_terminal_jobs_are_archived(conn):
    """三条任务：旧的已成功、刚完成的已成功、旧的还在排队。只应归档第一条。"""
    _finished_job(conn, "job-old", finished_at=OLD)
    _finished_job(conn, "job-new", finished_at=store.now())
    store.submit(
        conn,
        job_id="job-queued",
        kind=models.KIND_ANALYZE,
        payload={"dataset": "wireless_charger"},
        tenant_id=TENANT,
        idempotency_key="key-queued",
    )

    result = store.archive_finished(conn, older_than_days=30, dry_run=False)
    assert result["archived"] == ["job-old"]
    assert store.get(conn, "job-old")["result"] is None
    assert store.get(conn, "job-new")["result"] is not None
    assert store.get(conn, "job-queued")["result"] is None  # 本来就没有结果
    assert store.get(conn, "job-queued")["archived_at"] is None, "排队中的任务不应被打上归档标记"


def test_public_view_distinguishes_archived_from_never_had_result(conn):
    """「结果已归档」与「本来就没有结果」在接口上必须能分开。

    两者在 body 里都表现为「没有 result 字段」。若不加标记，
    调用方看到的是一个成功却没有结果的任务——那在任何语境下都像 bug。
    """
    _finished_job(conn, "job-c")
    store.archive_finished(conn, older_than_days=30, dry_run=False)

    archived = store.to_public(store.get(conn, "job-c"))
    assert "result" not in archived
    assert archived["result_archived"] is True

    # 一条从未产生结果的任务（失败）：既没有 result，也不该有归档标记
    store.submit(
        conn,
        job_id="job-failed",
        kind=models.KIND_ANALYZE,
        payload={"dataset": "wireless_charger"},
        tenant_id=TENANT,
        idempotency_key="key-failed",
    )
    store.claim(conn, "worker-1", lease_seconds=60)
    store.fail(conn, "job-failed", "llm_timeout")
    failed = store.to_public(store.get(conn, "job-failed"))
    assert "result" not in failed
    assert "result_archived" not in failed


def test_archive_rejects_non_positive_days(conn):
    with pytest.raises(ValueError):
        store.archive_finished(conn, older_than_days=0, dry_run=True)


def test_archived_rows_stay_out_of_the_candidate_set(conn):
    """归档过的行必须退出候选集，否则每次巡检都会把同一批行重算一遍。"""
    _finished_job(conn, "job-d")
    first = store.archive_finished(conn, older_than_days=30, dry_run=False)
    assert first["candidates"] == 1

    second = store.archive_finished(conn, older_than_days=30, dry_run=False)
    assert second["candidates"] == 0
    assert second["archived"] == []


def test_archive_measures_freed_bytes(conn):
    """预演要报「能省多少」，否则没人能判断值不值得清。"""
    _finished_job(conn, "job-e")
    size = len(store.get(conn, "job-e")["result"])
    result = store.archive_finished(conn, older_than_days=30, dry_run=True)
    assert result["bytes"] == size
    assert json.loads(store.get(conn, "job-e")["result"])["review_count"] == 3
