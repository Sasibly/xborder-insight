"""`/metrics` 的口径测试。

这个端点最容易出的不是「报错」，而是**报了一个看起来合理的错数**：
成功率算错分母、延迟拿字段名当含义、额度 0 被当成「用完了」。
所以这里断言的全是口径，不是格式。

两处刻意不测：
- 具体的小数位与行顺序（格式由 Prometheus 规范管，改格式不该让业务用例红）；
- `collect()` 内部排序（分位数按 nearest-rank 现算，输入乱序也应有同样结果，
  这一点用乱序样本覆盖，而不是断言实现细节）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from xborder_insight import api, metrics
from xborder_insight.db import migrate
from xborder_insight.db.connection import connect
from xborder_insight.jobs import store as job_store
from xborder_insight.security import keys


def _job(
    conn,
    job_id: str,
    status: str = "succeeded",
    *,
    tenant: str = "local",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    created_at: str = "2026-11-23T10:00:00.000",
) -> None:
    """直接写库造状态，不走 submit/claim/complete。

    理由同 `test_auth_api._seed_usage`：状态机是**被测对象之外的**一件事，
    用它造前置条件，一旦状态机改了，指标测试会跟着红，测的就不是指标了。
    这里只遵守表上的 CHECK（终态必须有 finished_at、失败必须有 error_code）。
    """
    finished = None if status == "queued" else created_at
    # running 必须带租约（表上的 CHECK 要求），否则「运行中」可以是任何没人心跳的任务
    started = created_at if status == "running" else None
    lease = "2026-11-23T10:05:00.000" if status == "running" else None
    conn.execute(
        "INSERT INTO jobs (job_id, tenant_id, kind, status, request_fingerprint, payload, "
        "prompt_version, model, taxonomy_version, created_at, started_at, finished_at, "
        "lease_expires_at, result, error_code, prompt_tokens, completion_tokens) "
        "VALUES (?, ?, 'analyze', ?, ?, '{}', 'p1', 'm1', 't1', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            tenant,
            status,
            f"fp-{job_id}",
            created_at,
            started,
            finished,
            lease,
            "{}" if status == "succeeded" else None,
            "boom" if status == "failed" else None,
            prompt_tokens,
            completion_tokens,
        ),
    )


def _run(conn, run_id: str, latency: float | None, *, status: str = "succeeded",
         calls: int = 0, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, dataset, status, started_at, finished_at, "
        "latency_p95_ms, llm_calls, prompt_tokens, completion_tokens) "
        "VALUES (?, 'd1', ?, '2026-11-23T10:00:00.000', ?, ?, ?, ?, ?)",
        (
            run_id,
            status,
            None if status == "running" else "2026-11-23T10:00:01.000",
            latency,
            calls,
            prompt_tokens,
            completion_tokens,
        ),
    )


def test_empty_database_declares_zero_jobs_and_omits_latency(conn):
    """空库也要输出四个状态为 0：序列消失会让告警规则静默失效。

    但延迟族必须缺席——输出 0ms 等于宣称「我们很快」，那是假数据。
    """
    text = metrics.collect(conn)

    for status in metrics.STATUSES:
        assert f'xbi_jobs_total{{status="{status}"}} 0' in text
    assert "xbi_run_latency_ms" not in text
    assert "xbi_jobs_success_ratio" not in text


def test_status_counts_and_success_ratio(conn):
    _job(conn, "j1", "succeeded")
    _job(conn, "j2", "succeeded")
    _job(conn, "j3", "failed")
    _job(conn, "j4", "queued")
    _job(conn, "j5", "running")

    text = metrics.collect(conn)

    assert 'xbi_jobs_total{status="succeeded"} 2' in text
    assert 'xbi_jobs_total{status="failed"} 1' in text
    assert 'xbi_jobs_total{status="queued"} 1' in text
    assert 'xbi_jobs_total{status="running"} 1' in text
    # 分母只算终态：把 queued/running 算进去，成功率会随着排队变长而假装变差
    assert "xbi_jobs_success_ratio 0.666667" in text


def test_latency_quantiles_are_nearest_rank_over_run_samples(conn):
    """乱序样本 [40, 10, 30, 20]：p50 = 20（第 2 个），p95 = 40（第 4 个）。

    注意字段名叫 `latency_p95_ms`，存的却是**单个 run 的端到端耗时**。
    这里断言的就是「跨 run 再取分位」这个口径——若有人改成对字段值再取一次
    p95，四个样本会被压成一个数，这条立刻红。
    """
    for run_id, latency in (("r1", 40), ("r2", 10), ("r3", 30), ("r4", 20)):
        _run(conn, run_id, latency)

    text = metrics.collect(conn)

    assert 'xbi_run_latency_ms{quantile="0.5"} 20' in text
    assert 'xbi_run_latency_ms{quantile="0.95"} 40' in text
    assert "xbi_run_latency_samples 4" in text


def test_failed_runs_are_excluded_from_tokens_and_latency(conn):
    """失败的 run 不计 token、不计延迟。

    token 计了会把「重试烧掉的钱」混进正常成本；延迟计了会让失败快、
    成功慢的场景看起来变好了——两个方向都是「越失败越好看」。
    """
    _run(conn, "ok", 30, calls=2, prompt_tokens=100, completion_tokens=50)
    _run(conn, "bad", 5, status="failed", calls=9, prompt_tokens=900, completion_tokens=900)

    text = metrics.collect(conn)

    assert "xbi_llm_calls_total 2" in text
    assert 'xbi_tokens_total{kind="prompt"} 100' in text
    assert 'xbi_tokens_total{kind="completion"} 50' in text
    assert 'xbi_run_latency_ms{quantile="0.5"} 30' in text


def test_quota_lines_use_the_same_ledger_as_enforcement(conn):
    """配额口径 = `jobs` 台账当日聚合，与 `quota.assert_quota` 同源。

    净化器那类的结论都建立在「今天花了多少」上，指标若另建计数器，
    迟早出现「接口说还有额度、账上说没有」。
    """
    _job(conn, "t1", prompt_tokens=300, completion_tokens=200)
    _job(conn, "t2", prompt_tokens=100, completion_tokens=0)

    text = metrics.collect(conn, tenant_id="local", daily_quota=1000, day="2026-11-23")

    assert 'xbi_quota_tokens_used{tenant="local"} 600' in text
    assert 'xbi_quota_jobs_used{tenant="local"} 2' in text
    assert 'xbi_quota_tokens_limit{tenant="local"} 1000' in text
    assert 'xbi_quota_tokens_remaining{tenant="local"} 400' in text


def test_remaining_clamps_at_zero_instead_of_going_negative(conn):
    """超支时余量是 0，不是负数——负数会让「>0」类告警漏报。"""
    _job(conn, "t1", prompt_tokens=900, completion_tokens=500)

    text = metrics.collect(conn, tenant_id="local", daily_quota=1000, day="2026-11-23")

    assert 'xbi_quota_tokens_remaining{tenant="local"} 0' in text


def test_unlimited_quota_omits_limit_series(conn):
    """不限量时不出 limit / remaining。

    出 `limit 0` 会被读成「额度用完」，与「没有限制」正好相反。
    """
    _job(conn, "t1", prompt_tokens=10)

    text = metrics.collect(conn, tenant_id="local", daily_quota=None, day="2026-11-23")

    assert "xbi_quota_tokens_used" in text
    assert "xbi_quota_tokens_limit" not in text
    assert "xbi_quota_tokens_remaining" not in text


def test_tenant_label_is_escaped(conn):
    """租户名里的引号必须转义，否则整段 exposition 变成非法格式。"""
    text = metrics.collect(conn, tenant_id='a"b\\c', daily_quota=1, day="2026-11-23")

    assert 'xbi_quota_tokens_used{tenant="a\\"b\\\\c"}' in text


def test_endpoint_serves_prometheus_text():
    """HTTP 层：状态码与 Content-Type 都要对。

    Content-Type 里的 `version=0.0.4` 是格式版本，抓取端据此解析；
    写成 `application/json` 或漏掉 version，Prometheus 会直接丢弃整页。
    """
    with TestClient(api.app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in response.headers["content-type"]
    assert "xbi_jobs_total" in response.text


@pytest.fixture
def auth_db(tmp_path):
    path = tmp_path / "metrics_auth.sqlite3"
    conn = connect(path)
    migrate.upgrade(conn)
    conn.close()
    return path


def test_metrics_authenticates_without_consuming_rate_limit(auth_db, monkeypatch):
    """开了鉴权时：无 key 401，有 key 200，且**不写限流窗口**。

    Prometheus 默认 15 秒抓一次，一分钟四次。若抓取走 `current_principal`
    （它每次都 `quota.hit()`），rpm=60 的 key 两小时就会被自己的监控打死——
    「服务还活着，但监控抓不到」是最难查的一类故障。
    """
    monkeypatch.setattr(api, "DB_FILE", auth_db)
    monkeypatch.setattr(
        api,
        "settings",
        replace(api.settings, auth_required=True, jobs_worker_enabled=False),
    )

    conn = connect(auth_db)
    raw, _ = keys.create(conn, tenant_id="acme", daily_token_quota=1000, rpm_limit=2)
    conn.close()

    with TestClient(api.app) as client:
        assert client.get("/metrics").status_code == 401

        headers = {"Authorization": f"Bearer {raw}"}
        # 连抓 5 次：rpm_limit=2 也不该被自己的监控打 429
        statuses = [client.get("/metrics", headers=headers).status_code for _ in range(5)]

    assert set(statuses) == {200}

    conn = connect(auth_db)
    try:
        hits = conn.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0]
    finally:
        conn.close()
    assert hits == 0, "抓取不应占用限流窗口，否则监控会把自己限死"
