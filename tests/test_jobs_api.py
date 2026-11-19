"""任务接口的 HTTP 契约测试。

重点：
1. 提交必须立刻返回 202，不能等执行完（这是任务化的全部意义）
2. 同一个 Idempotency-Key 重复提交只产生 1 个任务
3. 键相同但请求体不同 → 409，不是「返回旧任务」
4. 任务详情要能回答「跑了几次、每次花了多少」

测试里的执行器用 `drain()` 手动推进，不等后台轮询——
后台 worker 会让用例变成「等一会儿再断言」，那种测试迟早会 flaky。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from xborder_insight import api
from xborder_insight.db import migrate
from xborder_insight.db.connection import connect
from xborder_insight.jobs.runner import JobRunner
from xborder_insight.llm import build_llm_client

DECISION = {
    "asin": "B0TEST0001",
    "category": "wireless_charger",
    "marketplace": "US",
    "opportunity_score": 0.66,
    "data_provenance": "test_fixture",
    "cost": {
        "quoted_price": 8.00,
        "selling_price": 29.99,
        "inbound_freight": 1.20,
        "duty_rate": 0.075,
        "referral_rate": 0.15,
        "fba_fee": 3.50,
        "return_rate": 0.08,
        "returns_handling_fee": 0.80,
        "returns_refund_admin_rate": 0.20,
        "returns_goods_loss_ratio": 0.30,
        "returns_inbound_loss_ratio": 1.00,
        "incoterm": "FOB",
    },
}

ANALYZE = {
    "kind": "analyze",
    "reviews": [
        {
            "review_id": "r1",
            "asin": "B0TEST0001",
            "rating": 1,
            "body": "充电太慢了，充一晚上还是不满",
        },
        {
            "review_id": "r2",
            "asin": "B0TEST0001",
            "rating": 2,
            "body": "用了一周就发烫，很担心",
        },
    ],
    "top_k": 3,
}


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "jobs_api.sqlite3"
    conn = connect(path)
    migrate.upgrade(conn)
    conn.close()
    return path


@pytest.fixture
def client(db_file, monkeypatch):
    """关掉后台 worker，让用例用 drain() 自己推进执行。"""
    monkeypatch.setattr(api, "DB_FILE", db_file)
    monkeypatch.setattr(api, "settings", replace(api.settings, jobs_worker_enabled=False))
    with TestClient(api.app) as test_client:
        yield test_client


def _drain(db_file) -> int:
    @contextmanager
    def session():
        conn = connect(db_file)
        try:
            yield conn
        finally:
            conn.close()

    runner = JobRunner(
        session=session,
        client=build_llm_client(api.settings),
        settings=api.settings,
        worker_id="w-test",
        lease_seconds=60,
    )
    return asyncio.run(runner.drain())


def _submit(client, payload, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/v1/jobs", json=payload, headers=headers)


# ---------- 提交 ----------


def test_submit_returns_202_immediately(client):
    response = _submit(client, {"kind": "decision", **DECISION})
    assert response.status_code == 202

    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"].startswith("job-")
    assert body["idempotent_replay"] is False
    assert body["links"]["self"] == f"/v1/jobs/{body['job_id']}"


def test_submit_records_versions(client):
    body = _submit(client, {"kind": "decision", **DECISION}).json()
    assert body["versions"]["prompt_version"].startswith("prompt-")
    assert body["versions"]["taxonomy_version"].startswith("tax-")


def test_same_key_five_times_produces_one_job(client):
    """同一幂等键重复提交 5 次，只产生 1 个任务、1 次计费。"""
    job_ids = set()
    for _ in range(5):
        response = _submit(client, {"kind": "decision", **DECISION}, key="fixed-key")
        assert response.status_code == 202
        job_ids.add(response.json()["job_id"])

    assert len(job_ids) == 1
    assert len(client.get("/v1/jobs").json()["jobs"]) == 1


def test_replay_is_flagged(client):
    first = _submit(client, {"kind": "decision", **DECISION}, key="k1")
    second = _submit(client, {"kind": "decision", **DECISION}, key="k1")

    assert first.headers["Idempotency-Replayed"] == "false"
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.json()["idempotent_replay"] is True


def test_same_key_different_body_is_409(client):
    _submit(client, {"kind": "decision", **DECISION}, key="k1")
    response = _submit(
        client, {"kind": "decision", **DECISION, "opportunity_score": 0.91}, key="k1"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_conflict"
    # 冲突响应同样要带 request_id，否则排障时无法关联日志
    assert response.json()["request_id"]


def test_unknown_kind_is_422(client):
    response = _submit(client, {"kind": "teleport", "dataset": "x"})
    assert response.status_code == 422


def test_analyze_with_both_sources_is_422(client):
    """继承同步契约后，任务入口的校验规则必须完全一致。"""
    response = _submit(client, {**ANALYZE, "dataset": "wireless_charger"})
    assert response.status_code == 422


# ---------- 查询 ----------


def test_get_missing_job_is_404(client):
    response = client.get("/v1/jobs/job-nope")
    assert response.status_code == 404
    assert response.json()["code"] == "job_not_found"


def test_list_filters_by_status(client):
    _submit(client, {"kind": "decision", **DECISION})
    assert len(client.get("/v1/jobs", params={"status": "queued"}).json()["jobs"]) == 1
    assert client.get("/v1/jobs", params={"status": "succeeded"}).json()["jobs"] == []


def test_list_rejects_unknown_status(client):
    assert client.get("/v1/jobs", params={"status": "zombie"}).status_code == 422


# ---------- 执行 ----------


def test_decision_job_runs_to_succeeded(client, db_file):
    job_id = _submit(client, {"kind": "decision", **DECISION}).json()["job_id"]
    assert _drain(db_file) == 1

    detail = client.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "succeeded"
    assert detail["result"]["verdict"] in {"GO", "NO_GO", "NEED_INFO"}
    # 决策不调模型，因此不该产生 token；记录了就是口径错了
    assert detail["usage"]["total_tokens"] == 0
    # 决策不写 runs 台账（没有模型调用需要归因）
    assert detail["executions"] == []


def test_analyze_job_writes_run_ledger(client, db_file):
    """分析任务必须落 runs 台账：token 消耗是主要成本，不记账就等于成本失控。"""
    job_id = _submit(client, ANALYZE).json()["job_id"]
    _drain(db_file)

    detail = client.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "succeeded"
    assert detail["result"]["review_count"] == 2
    assert detail["usage"]["llm_calls"] > 0

    assert len(detail["executions"]) == 1
    execution = detail["executions"][0]
    assert execution["attempt"] == 1
    assert execution["status"] == "succeeded"
    assert execution["total_tokens"] > 0


def test_failed_job_reports_error_code_and_can_retry(client, db_file):
    """数据集不存在 → 任务失败并给出 error_code；retry 后回到队列。"""
    job_id = _submit(client, {"kind": "analyze", "dataset": "no_such_dataset"}).json()["job_id"]
    _drain(db_file)

    failed = client.get(f"/v1/jobs/{job_id}").json()
    assert failed["status"] == "failed"
    assert failed["error_code"] == "input_invalid"

    retried = client.post(f"/v1/jobs/{job_id}/retry")
    assert retried.status_code == 202
    assert retried.json()["status"] == "queued"
    # 失败那次不隐藏：次数保留，重试是第 2 次尝试
    assert retried.json()["attempts"] == 1


def test_retry_succeeded_job_is_409(client, db_file):
    job_id = _submit(client, {"kind": "decision", **DECISION}).json()["job_id"]
    _drain(db_file)
    response = client.post(f"/v1/jobs/{job_id}/retry")
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_state_transition"


def test_retried_analysis_records_two_attempts(client, db_file):
    """重试后 `runs` 应有两条：失败那次也烧了 token，成本口径必须包含它。"""
    job_id = _submit(client, {"kind": "analyze", "dataset": "missing_a"}).json()["job_id"]
    _drain(db_file)
    client.post(f"/v1/jobs/{job_id}/retry")
    _drain(db_file)

    detail = client.get(f"/v1/jobs/{job_id}").json()
    assert detail["attempts"] == 2
    attempts = [row["attempt"] for row in detail["executions"]]
    assert attempts == [1, 2]
    assert {row["status"] for row in detail["executions"]} == {"failed"}
