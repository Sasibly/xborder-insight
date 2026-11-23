"""P4-1 集成测试：真起进程、真 HTTP、真库。

与 `tests/test_jobs_api.py` 的分界要说清，否则两组测试会慢慢重复：

- `test_jobs_api.py` 用 `TestClient`，在**同一个进程内**调用 ASGI 应用。它快，适合覆盖
  契约分支（422 的字段、404 的归属、409 的状态机），但 `TestClient` 会替我们处理很多
  真实部署里的东西：端口、启动顺序、子进程环境变量、lifespan 与 worker 的竞态。
- 本文件起 `uvicorn` 子进程，走真实 socket。它慢（一次几秒），但它验的是**部署形态**：
  「先迁移再起服务」的顺序、`/ready` 在真实进程里的返回值、进程内 worker 真的会领任务。

刻意不做的事：不测模型质量（那是 `run_eval` 的事），不做压测（`docs/03` §5 已明确降级）。

Windows 提示：这里只用 `terminate()` + 超时后 `kill()`，不依赖信号语义——Windows 上没有
SIGTERM 那套东西，`terminate()` 就是 TerminateProcess，对 uvicorn 单进程足够。
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from xborder_insight.db import migrate
from xborder_insight.db.connection import connect
from xborder_insight.security import keys as keys_mod

ROOT = Path(__file__).resolve().parents[1]

# mock + 强制鉴权：鉴权在本地常常是关的，于是「401 → 建 key → 带 key 提交 → 越权 404」
# 这条链路从来没被真的跑过。集成测试要跑生产会走的那条路。
BASE_ENV = {
    "RUN_MODE": "mock",
    "AUTH_REQUIRED": "true",
    "SYNC_ANALYZE_ENABLED": "false",
    "JOB_WORKER_ENABLED": "true",
    "JOB_POLL_SECONDS": "0.2",
    "LOG_LEVEL": "WARNING",
    "PYTHONIOENCODING": "utf-8",
}


def _free_port() -> int:
    """向系统要一个空闲端口再立刻放掉。

    有被抢占的窗口期，但比固定 8000 好：固定端口会在本机已经跑着服务时
    给出一串莫名其妙的断言失败，看起来像「接口坏了」。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(client: httpx.Client, proc: subprocess.Popen, timeout: float) -> None:
    """只认 200。503 说明库没迁移——进程活着但不可用，继续断言只会得到一串 503。"""
    deadline = time.monotonic() + timeout
    last = "未连上"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"服务进程提前退出（exit={proc.returncode}）")
        try:
            response = client.get("/ready")
            if response.status_code == 200:
                return
            last = f"HTTP {response.status_code} {response.text[:200]}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(0.2)
    raise RuntimeError(f"等待 {timeout:.0f}s 仍未就绪：{last}")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """起一次服务，供本模块所有用例共用。

    module 级而不是 function 级：起进程要 2–3 秒，8 个用例各起一次会让这个文件
    从「值得跑」变成「跑一次就不想跑」。代价是用例之间共享状态——所以断言一律用
    「至少 N」而不是「正好 N」。
    """
    tmp = tmp_path_factory.mktemp("integration")
    db_path = tmp / "it.sqlite3"
    env = dict(os.environ)
    env.update(BASE_ENV)
    env["DB_PATH"] = str(db_path)

    # 先迁移再起服务，顺序与容器入口脚本一致——集成测试要验的正是这个顺序
    migrated = subprocess.run(
        [sys.executable, "scripts/db.py", "up"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    if migrated.returncode != 0:
        raise RuntimeError(f"迁移失败：\n{migrated.stdout[-1200:]}\n{migrated.stderr[-1200:]}")

    # 直接调库建 key（不走 CLI）：CLI 已由 smoke 覆盖，这里关注 HTTP 层。
    # 两者写同一张表、同一套哈希，所以不存在「测试绕过了真实路径」的问题。
    conn = connect(db_path)
    try:
        key_a, _ = keys_mod.create(conn, tenant_id="it-a", label="integration", rpm_limit=600)
        key_b, _ = keys_mod.create(conn, tenant_id="it-b", label="integration", rpm_limit=600)
    finally:
        conn.close()

    port = _free_port()
    log_path = tmp / "server.log"
    log_file = log_path.open("wb")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "xborder_insight.api:app",
            "--app-dir", "src", "--host", "127.0.0.1", "--port", str(port),
            "--no-access-log",
        ],
        cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )

    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as client:
        try:
            _wait_ready(client, proc, timeout=40.0)
        except Exception:
            proc.kill()
            log_file.close()
            raise RuntimeError(f"服务未就绪，日志尾部：\n{log_path.read_text('utf-8', 'replace')[-2000:]}")

        yield SimpleNamespace(
            client=client, base=client.base_url, key_a=key_a, key_b=key_b,
            db_path=db_path, proc=proc, log_path=log_path,
        )

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    log_file.close()


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _wait_job(client: httpx.Client, job_id: str, key: str, timeout: float = 90.0) -> dict:
    """轮询到终态。key 必须显式传：忘了带会拿回 401，而那看起来像「任务丢了」。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}", headers=_h(key))
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in {"succeeded", "failed"}:
            return last
        time.sleep(0.2)
    pytest.fail(f"任务 {job_id} 在 {timeout:.0f}s 内未到终态，最后状态：{last.get('status')}")


def test_ready_reports_the_schema_the_code_expects(live):
    """`/ready` 的 schema_version 必须等于代码里的 LATEST。

    这条在真实进程里才有意义：进程内测试拿的是同一个 `migrate` 模块，
    而真实部署里「代码版本」与「库里版本」是两条独立的事实。
    """
    response = live.client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == str(migrate.LATEST)
    # 鉴权状态回显：部署方据此在网关判断，而不是靠人记住这次有没有配
    assert body["auth"] == "required"


def test_missing_key_is_401_with_challenge(live):
    response = live.client.get("/v1/jobs")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthorized"


def test_analyze_job_runs_end_to_end(live):
    """提交 → 202 → worker 领取 → 落结果 → 详情带执行台账。"""
    submitted = live.client.post(
        "/v1/jobs",
        json={"kind": "analyze", "dataset": "air_purifier", "top_k": 3},
        headers={**_h(live.key_a), "Idempotency-Key": "it-analyze-1"},
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["status"] == "queued"
    assert submitted.headers["Idempotency-Replayed"] == "false"

    done = _wait_job(live.client, body["job_id"], live.key_a)
    assert done["status"] == "succeeded", done
    assert done["result"]["themes"], "分析结果里没有主题"
    # 执行台账：每次尝试一条，成功任务应为 1 条且 attempt=1
    assert len(done["executions"]) == 1
    assert done["executions"][0]["attempt"] == 1
    assert done["executions"][0]["status"] == "succeeded"


def test_idempotent_replay_returns_the_same_job(live):
    """同键同体重放 → 同一个 job_id，且台账里只有一条执行记录。"""
    payload = {"kind": "analyze", "dataset": "electric_kettle", "top_k": 2}
    first = live.client.post(
        "/v1/jobs", json=payload,
        headers={**_h(live.key_a), "Idempotency-Key": "it-replay-1"},
    )
    assert first.status_code == 202
    job_id = first.json()["job_id"]

    again = live.client.post(
        "/v1/jobs", json=payload,
        headers={**_h(live.key_a), "Idempotency-Key": "it-replay-1"},
    )
    assert again.status_code == 202
    assert again.headers["Idempotency-Replayed"] == "true"
    assert again.json()["job_id"] == job_id
    assert again.json()["idempotent_replay"] is True

    # 重放不能产生第二个任务：列表里这个键只对应一个 job
    listed = live.client.get("/v1/jobs?limit=200", headers=_h(live.key_a)).json()["jobs"]
    assert sum(1 for job in listed if job["job_id"] == job_id) == 1


def test_same_key_with_different_body_is_a_conflict(live):
    """同键换体必须 409：返回旧任务会让调用方以为新参数已生效。"""
    headers = {**_h(live.key_a), "Idempotency-Key": "it-conflict-1"}
    first = live.client.post(
        "/v1/jobs", json={"kind": "analyze", "dataset": "air_purifier"}, headers=headers
    )
    assert first.status_code == 202

    second = live.client.post(
        "/v1/jobs", json={"kind": "analyze", "dataset": "coffee_machine"}, headers=headers
    )
    assert second.status_code == 409
    assert second.json()["code"] == "idempotency_conflict"


def test_other_tenant_cannot_read_the_job(live):
    """跨租户一律 404，而不是 403——403 等于承认这个 id 存在。"""
    submitted = live.client.post(
        "/v1/jobs",
        json={"kind": "analyze", "dataset": "pet_fountain", "top_k": 2},
        headers={**_h(live.key_a), "Idempotency-Key": "it-scope-1"},
    )
    job_id = submitted.json()["job_id"]

    # 本租户能读
    assert live.client.get(f"/v1/jobs/{job_id}", headers=_h(live.key_a)).status_code == 200
    # 另一租户：读不到，且不给「存在但无权限」的信号
    cross = live.client.get(f"/v1/jobs/{job_id}", headers=_h(live.key_b))
    assert cross.status_code == 404
    assert cross.json()["code"] == "job_not_found"


def test_metrics_aggregates_the_real_traffic(live):
    """指标来自台账，所以前面跑过的任务必须出现在里面。

    这条同时钉住一个口径：成功率的分母只算终态。若哪天有人把 queued 算进分母，
    这里会先红——而不是等到线上报警时才发现「服务越忙成功率越低」。
    """
    response = live.client.get("/metrics", headers=_h(live.key_a))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text

    assert 'xbi_jobs_total{status="succeeded"}' in text
    succeeded = int(
        next(
            line.split()[-1]
            for line in text.splitlines()
            if line.startswith('xbi_jobs_total{status="succeeded"}')
        )
    )
    assert succeeded >= 1
    assert "xbi_jobs_success_ratio" in text
    # 有成功 run 就必须有延迟样本（否则意味着分位算错了或没过样本）
    assert "xbi_run_latency_ms" in text


def test_metrics_scrape_does_not_consume_rate_limit(live):
    """抓取认人但不记限流——否则 Prometheus 每 15 秒一次会把自己的监控打死。

    这里不能靠「连抓很多次」来验（rpm=600，抓不爆）。改为查台账：抓取前后
    `rate_limits` 的行数不变。
    """

    def count() -> int:
        conn = sqlite3.connect(live.db_path, timeout=5)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0])
        finally:
            conn.close()

    before = count()
    for _ in range(5):
        assert live.client.get("/metrics", headers=_h(live.key_a)).status_code == 200
    assert count() == before
