"""接入控制层在 HTTP 上的行为。

这里只测「接口看得见的部分」，密钥与配额的内部逻辑在 test_auth / test_quota 里测。
关注四件事：

1. 未鉴权就是 401，且不泄漏任何关于 key 存在与否的信息
2. 租户从密钥派生，**客户端不能自己声明**（声明了会被契约拒绝）
3. 别人的任务一律 404，不是 403——403 等于确认了这个 id 存在
4. 限流 429 带 Retry-After，额度 429 带 quota_exceeded，两者能区分
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from xborder_insight import api
from xborder_insight.db import migrate
from xborder_insight.db.connection import connect
from xborder_insight.jobs import store as job_store
from xborder_insight.security import keys

ANALYZE = {
    "kind": "analyze",
    "reviews": [
        {"review_id": "r1", "asin": "B0TEST0001", "rating": 1, "body": "充电太慢了，一晚上充不满"},
    ],
    "top_k": 3,
}

DECISION = {
    "kind": "decision",
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


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "auth_api.sqlite3"
    conn = connect(path)
    migrate.upgrade(conn)
    conn.close()
    return path


@pytest.fixture
def client(db_file, monkeypatch):
    monkeypatch.setattr(api, "DB_FILE", db_file)
    monkeypatch.setattr(
        api,
        "settings",
        replace(api.settings, auth_required=True, jobs_worker_enabled=False, sync_analyze_enabled=False),
    )
    with TestClient(api.app) as test_client:
        yield test_client


def _key(db_file, *, tenant: str, quota: int | None = None, rpm: int = 60) -> str:
    conn = connect(db_file)
    try:
        raw, _ = keys.create(conn, tenant_id=tenant, daily_token_quota=quota, rpm_limit=rpm)
        return raw
    finally:
        conn.close()


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def _tenant_of(db_file, job_id: str) -> str:
    conn = connect(db_file)
    try:
        return str(job_store.get(conn, job_id)["tenant_id"])
    finally:
        conn.close()


def _seed_usage(db_file, *, tenant: str, tokens: int) -> None:
    """造一条已完成的消耗记录，作为「额度已用」的前置状态。

    直接写库而不是走 claim→complete：`claim` 取的是队列里最早的一条，
    在已有待办任务的用例里会抢错对象，把「造前置状态」变成「改坏被测对象」。
    """
    conn = connect(db_file)
    try:
        stamp = job_store.now()
        conn.execute(
            "INSERT INTO jobs (job_id, tenant_id, kind, status, request_fingerprint, payload, "
            "prompt_version, model, taxonomy_version, created_at, started_at, finished_at, "
            "prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                f"job-seed-{tenant}-{tokens}",
                tenant,
                "analyze",
                "succeeded",
                f"fp-seed-{tokens}",
                "{}",
                "prompt-x",
                "model-x",
                "tax-x",
                stamp,
                stamp,
                stamp,
                tokens,
            ),
        )
    finally:
        conn.close()


# ---------- 鉴权 ----------


def test_missing_key_is_401_with_www_authenticate(client):
    response = client.get("/v1/jobs")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["request_id"]


def test_invalid_key_is_401(client):
    response = client.get("/v1/jobs", headers={"Authorization": "Bearer xb_" + "a" * 43})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_non_bearer_scheme_is_rejected_explicitly(client):
    """误用 Basic 时直接报错，而不是悄悄回落到 X-API-Key。

    静默回落会让一个配置错误在本地看起来「能用」，联调时突然 401。
    """
    response = client.get("/v1/jobs", headers={"Authorization": "Basic abc"})
    assert response.status_code == 401


def test_x_api_key_header_also_works(client, db_file):
    raw = _key(db_file, tenant="acme")
    response = client.get("/v1/jobs", headers={"X-API-Key": raw})
    assert response.status_code == 200


def test_valid_key_creates_a_job_owned_by_its_tenant(client, db_file):
    raw = _key(db_file, tenant="acme")
    response = client.post("/v1/jobs", json=ANALYZE, headers=_auth(raw))
    assert response.status_code == 202
    assert _tenant_of(db_file, response.json()["job_id"]) == "acme"


def test_revoked_key_stops_working_immediately(client, db_file):
    raw = _key(db_file, tenant="acme")
    assert client.get("/v1/jobs", headers=_auth(raw)).status_code == 200

    conn = connect(db_file)
    try:
        keys.revoke(conn, raw[: keys.HINT_LEN])
    finally:
        conn.close()

    assert client.get("/v1/jobs", headers=_auth(raw)).status_code == 401


def test_client_cannot_declare_its_own_tenant(client, db_file):
    """请求体里塞 tenant_id 会被契约拒绝（extra=forbid）。

    如果它被静默忽略，调用方会以为自己切到别的租户了；
    如果它被接受，隔离就形同虚设。
    """
    raw = _key(db_file, tenant="acme")
    response = client.post("/v1/jobs", json={**ANALYZE, "tenant_id": "someone-else"}, headers=_auth(raw))
    assert response.status_code == 422
    assert "tenant_id" in response.json()["message"]


# ---------- 租户隔离 ----------


def test_other_tenant_cannot_read_the_job(client, db_file):
    acme = _key(db_file, tenant="acme")
    other = _key(db_file, tenant="other")
    job_id = client.post("/v1/jobs", json=ANALYZE, headers=_auth(acme)).json()["job_id"]

    response = client.get(f"/v1/jobs/{job_id}", headers=_auth(other))
    assert response.status_code == 404
    assert response.json()["code"] == "job_not_found"
    # 与「真的不存在」完全一致的响应，不给 id 探测留口子
    missing = client.get("/v1/jobs/job-doesnotexist", headers=_auth(other))
    assert missing.status_code == response.status_code
    assert missing.json()["code"] == response.json()["code"]


def test_other_tenant_cannot_retry_the_job(client, db_file):
    """重试等于替别人再花一次钱，必须在状态校验之前就拦住。"""
    acme = _key(db_file, tenant="acme")
    other = _key(db_file, tenant="other")
    job_id = client.post("/v1/jobs", json=ANALYZE, headers=_auth(acme)).json()["job_id"]

    response = client.post(f"/v1/jobs/{job_id}/retry", headers=_auth(other))
    assert response.status_code == 404


def test_list_only_shows_own_jobs(client, db_file):
    acme = _key(db_file, tenant="acme")
    other = _key(db_file, tenant="other")
    client.post("/v1/jobs", json=ANALYZE, headers=_auth(acme))
    client.post("/v1/jobs", json=ANALYZE, headers=_auth(acme))

    assert len(client.get("/v1/jobs", headers=_auth(acme)).json()["jobs"]) == 2
    assert client.get("/v1/jobs", headers=_auth(other)).json()["jobs"] == []


def test_idempotency_keys_are_scoped_per_tenant(client, db_file):
    """两个租户用同一个 Idempotency-Key 应各自独立，不能互相占键。"""
    acme = _key(db_file, tenant="acme")
    other = _key(db_file, tenant="other")

    first = client.post("/v1/jobs", json=ANALYZE, headers={**_auth(acme), "Idempotency-Key": "same"})
    second = client.post("/v1/jobs", json=ANALYZE, headers={**_auth(other), "Idempotency-Key": "same"})

    assert first.json()["idempotent_replay"] is False
    assert second.json()["idempotent_replay"] is False
    assert first.json()["job_id"] != second.json()["job_id"]


# ---------- 限流与配额 ----------


def test_rate_limit_returns_429_with_retry_after(client, db_file):
    raw = _key(db_file, tenant="acme", rpm=2)
    assert client.get("/v1/jobs", headers=_auth(raw)).status_code == 200
    assert client.get("/v1/jobs", headers=_auth(raw)).status_code == 200

    response = client.get("/v1/jobs", headers=_auth(raw))
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= 60


def test_rate_limit_is_per_tenant(client, db_file):
    noisy = _key(db_file, tenant="noisy", rpm=1)
    quiet = _key(db_file, tenant="quiet", rpm=1)
    client.get("/v1/jobs", headers=_auth(noisy))
    assert client.get("/v1/jobs", headers=_auth(noisy)).status_code == 429
    assert client.get("/v1/jobs", headers=_auth(quiet)).status_code == 200


def test_quota_blocks_analyze_but_not_decision(client, db_file):
    """额度用尽时不该连不花钱的接口一起挡。

    决策任务的成本由调用方提供，不调模型，按额度拦它属于误伤。
    """
    raw = _key(db_file, tenant="acme", quota=100)
    _seed_usage(db_file, tenant="acme", tokens=100)

    blocked = client.post("/v1/jobs", json=ANALYZE, headers=_auth(raw))
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "quota_exceeded"

    allowed = client.post("/v1/jobs", json=DECISION, headers=_auth(raw))
    assert allowed.status_code == 202


def test_quota_allows_replay_of_an_existing_job(client, db_file):
    """额度用尽后仍要能查回已经付过钱的任务，否则等于把结果扣住不给。"""
    raw = _key(db_file, tenant="acme", quota=100)
    headers = {**_auth(raw), "Idempotency-Key": "keep-me"}
    first = client.post("/v1/jobs", json=ANALYZE, headers=headers)
    assert first.status_code == 202

    _seed_usage(db_file, tenant="acme", tokens=100)

    replay = client.post("/v1/jobs", json=ANALYZE, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["job_id"] == first.json()["job_id"]


# ---------- 其他 ----------


def test_sync_analyze_is_disabled_when_configured(client, db_file):
    """关闭同步分析时必须告诉调用方替代路径，而不只是「不开放」。"""
    raw = _key(db_file, tenant="acme")
    response = client.post("/v1/analyze", json={"dataset": "wireless_charger"}, headers=_auth(raw))
    assert response.status_code == 403
    assert response.json()["code"] == "feature_disabled"
    assert "/v1/jobs" in response.json()["message"]


def test_ready_reports_auth_state(client):
    body = client.get("/ready").json()
    assert body["auth"] == "required"


def test_ops_endpoints_stay_open(client):
    """健康检查要能在没有密钥时被探活，否则编排系统无法判断容器是否活着。"""
    assert client.get("/health").status_code == 200
