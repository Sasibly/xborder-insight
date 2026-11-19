"""交付冒烟：一条命令回答「这份代码 / 这个镜像，真的能跑起来吗」。

与 `make check` 的分工（两者不可互相替代）：

| | make check | smoke |
|---|---|---|
| 回答 | 改了会不会变差 | 现在能不能用 |
| 载体 | 进程内、TestClient、临时库 | 真起服务、真走端口、真建库 |
| 覆盖 | 逻辑正确性、迁移可逆、指标退化 | 启动顺序、迁移时机、鉴权、任务闭环 |
| 跑不起来时 | 也可能全绿 | 必然红 |

`make check` 全绿但服务起不来，是这类项目最典型的交付事故：
测试里 `TestClient` 自己建库、自己装 key，跳过了「谁在什么时候建库」这一整段。

三个目标：

    python scripts/smoke.py                  # local：临时库 + 临时端口，测代码
    python scripts/smoke.py --target docker  # docker：构建镜像 + 起容器，测交付物
    python scripts/smoke.py --base-url http://host:8000 --api-key xb_xxx
                                             # remote：只打已经跑起来的环境

退出码：0 全过；1 有断言失败；2 环境问题（无 docker、端口不可用、服务起不来）。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xborder_insight.db import migrate  # noqa: E402
from xborder_insight.domain import decision as decision_mod  # noqa: E402

VERDICTS = {decision_mod.GO, decision_mod.NO_GO, decision_mod.NEED_MORE_INFO}

IMAGE_TAG = "xborder-insight:smoke"
CONTAINER_PORT = "8000"

# 故意用 3 条内联评论而不是整个数据集：
# 冒烟要能在 live 模式下也跑得起（42 条评论就是 42 次真实调用）。
# 「数据集能读」这件事由 GET /v1/datasets 单独验证，不占模型预算。
SMOKE_REVIEWS = [
    {
        "review_id": "smoke-1",
        "asin": "B0SMOKE001",
        "rating": 2,
        "title": "Charges too slowly",
        "body": "Takes over two hours to charge my phone, and the pad gets hot.",
        "lang": "en",
    },
    {
        "review_id": "smoke-2",
        "asin": "B0SMOKE001",
        "rating": 1,
        "title": "Stopped working",
        "body": "After three weeks it just died. No light, no charge.",
        "lang": "en",
    },
    {
        "review_id": "smoke-3",
        "asin": "B0SMOKE002",
        "rating": 4,
        "title": "Fine for the price",
        "body": "Works as expected, slightly slower than my old one.",
        "lang": "en",
    },
]

DECISION_PAYLOAD = {
    "asin": "B0SMOKE001",
    "category": "wireless_charger",
    "marketplace": "US",
    "opportunity_score": 0.66,
    "data_provenance": "smoke_fixture",
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


# ---------- 输出 ----------


class Checker:
    """按顺序记账。任何一步失败都继续跑下一 步——

    冒烟的价值一半在「哪一步先炸」：401 没拦住说明鉴权挂了，
    任务不完成说明 worker 没起，两个症状指向完全不同的修复动作。
    遇到第一个失败就退出，等于只告诉你一个症状。
    """

    def __init__(self) -> None:
        self.index = 0
        self.failures = 0

    def _line(self, mark: str, name: str, detail: str) -> None:
        self.index += 1
        body = f"{self.index:02d} {name}"
        print(f"  {mark} {body}{'  · ' + detail if detail else ''}", flush=True)

    def ok(self, name: str, detail: str = "") -> None:
        self._line("✓", name, detail)

    def fail(self, name: str, detail: str = "") -> None:
        self.failures += 1
        self._line("✗", name, detail)

    def skip(self, name: str, detail: str = "") -> None:
        """跳过不是通过。用 '–' 区分，避免「没测」被读成「测过了没问题」。"""
        self._line("–", name, detail)

    def expect(self, condition: bool, name: str, good: str = "", bad: str = "") -> bool:
        if condition:
            self.ok(name, good)
        else:
            self.fail(name, bad)
        return bool(condition)


def _echo(text: str = "") -> None:
    print(text, flush=True)


def _rule(char: str = "-", width: int = 74) -> None:
    _echo(char * width)


# ---------- 进程/容器生命周期 ----------


def _python() -> Path:
    for candidate in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def _free_port() -> int:
    """向系统要一个当前空闲的端口，然后立刻放掉。

    存在被别的进程抢走的窗口期，所以调用方要能重试；对冒烟来说，
    撞上一个「Address already in use」并重试一次，比固定端口 8000 好得多——
    固定端口会在本机已经跑着服务时给出莫名其妙的断言失败。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _base_env() -> dict[str, str]:
    """起服务用的环境。

    刻意在 mock 下**强制打开鉴权**：鉴权在本地常常是关的，
    于是一整条「401 → 建 key → 带 key 提交 → 越权 404」的链路从来没被真的跑过。
    冒烟的价值就在这里——它跑的是生产会走的那条路，不是本地那条。
    """
    env = dict(os.environ)
    env.update(
        {
            "RUN_MODE": "mock",
            "AUTH_REQUIRED": "true",
            "SYNC_ANALYZE_ENABLED": "false",
            "JOB_WORKER_ENABLED": "true",
            "JOB_POLL_SECONDS": "0.2",
            "LOG_LEVEL": "WARNING",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def _wait_ready(base_url: str, timeout: float, proc: subprocess.Popen | None = None) -> tuple[bool, str]:
    """等 /ready 返回 200。

    只认 200：503 表示库没迁移，进程活着但不可用，
    这时候断言后面的接口只会得到一串 503，把真正的根因埋在中间。
    """
    deadline = time.monotonic() + timeout
    last = "未连上"
    with httpx.Client(timeout=3.0) as client:
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                return False, f"进程已退出（exit={proc.returncode}）"
            try:
                response = client.get(f"{base_url}/ready")
                if response.status_code == 200:
                    return True, ""
                last = f"HTTP {response.status_code} {response.text[:160]}"
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            time.sleep(0.3)
    return False, f"等待 {timeout:.0f}s 仍未就绪：{last}"


def _start_local(tmp: Path) -> tuple[str, subprocess.Popen, Path]:
    port = _free_port()
    env = _base_env()
    env.update(
        {
            "DB_PATH": str(tmp / "smoke.sqlite3"),
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(port),
        }
    )

    # 先迁移再起服务。顺序与容器入口脚本一致——冒烟验的就是这个顺序。
    migrated = subprocess.run(
        [str(_python()), "scripts/db.py", "up"],
        cwd=ROOT, env=env, capture_output=True,
    )
    if migrated.returncode != 0:
        raise RuntimeError(
            "迁移失败：\n" + migrated.stdout.decode("utf-8", "replace")[-1200:]
            + migrated.stderr.decode("utf-8", "replace")[-1200:]
        )

    log_path = tmp / "server.log"
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [
            str(_python()), "-m", "uvicorn", "xborder_insight.api:app",
            "--app-dir", "src", "--host", "127.0.0.1", "--port", str(port),
            "--no-access-log",
        ],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    return f"http://127.0.0.1:{port}", proc, log_path


def _docker(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, check=check)


def _docker_available() -> tuple[bool, str]:
    try:
        finished = _docker("version", "--format", "{{.Server.Version}}")
    except FileNotFoundError:
        return False, "未安装 docker CLI"
    if finished.returncode != 0:
        message = finished.stderr.decode("utf-8", "replace").strip().splitlines()
        return False, message[-1] if message else "docker daemon 不可达"
    return True, finished.stdout.decode("utf-8", "replace").strip()


def _start_docker(name: str, base_image: str = "") -> tuple[str, str]:
    build_args = ["build", "-t", IMAGE_TAG]
    if base_image:
        build_args += ["--build-arg", f"PYTHON_IMAGE={base_image}"]
        _echo(f"  基础镜像覆盖为 {base_image}")
    build = _docker(*build_args, ".")
    if build.returncode != 0:
        raise RuntimeError(
            "镜像构建失败：\n" + build.stderr.decode("utf-8", "replace")[-2000:]
        )

    port = _free_port()
    run = _docker(
        "run", "-d", "--name", name,
        "-e", "RUN_MODE=mock",
        "-e", "AUTH_REQUIRED=true",
        "-e", "LOG_LEVEL=INFO",
        "-p", f"127.0.0.1:{port}:{CONTAINER_PORT}",
        IMAGE_TAG,
    )
    if run.returncode != 0:
        raise RuntimeError("容器启动失败：\n" + run.stderr.decode("utf-8", "replace")[-2000:])
    return f"http://127.0.0.1:{port}", run.stdout.decode().strip()


def _create_key_local(tenant: str, tmp: Path) -> str:
    env = _base_env()
    env["DB_PATH"] = str(tmp / "smoke.sqlite3")
    finished = subprocess.run(
        [str(_python()), "scripts/keys.py", "create", "--tenant", tenant, "--rpm", "600"],
        cwd=ROOT, env=env, capture_output=True,
    )
    if finished.returncode != 0:
        raise RuntimeError("建 key 失败：" + finished.stderr.decode("utf-8", "replace")[-800:])
    return _extract_key(finished.stdout.decode("utf-8", "replace"))


def _create_key_docker(container: str, tenant: str) -> str:
    finished = _docker(
        "exec", container, "python", "scripts/keys.py", "create", "--tenant", tenant, "--rpm", "600"
    )
    if finished.returncode != 0:
        raise RuntimeError("容器内建 key 失败：" + finished.stderr.decode("utf-8", "replace")[-800:])
    return _extract_key(finished.stdout.decode("utf-8", "replace"))


def _extract_key(output: str) -> str:
    """从 CLI 输出里捞明文 key。

    不用固定行号/固定缩进：那种解析在有人调了输出格式之后会静默拿到空串，
    然后冒烟的所有请求变成 401，报错看起来像「鉴权坏了」而不是「解析坏了」。
    """
    match = re.search(r"xb_[A-Za-z0-9_\-]{20,}", output)
    if match is None:
        raise RuntimeError("没能从 CLI 输出里解析出 API Key：\n" + output[-800:])
    return match.group(0)


# ---------- 断言流程 ----------


def _wait_job(client: httpx.Client, job_id: str, headers: dict, timeout: float = 30.0) -> dict:
    """轮询到终态。`headers` 必须显式传：忘了带 key 时这里拿回 401，
    症状会显示成「任务没完成」，而真因是冒烟自己没鉴权——
    这类自欺欺人的失败比直接报错更耗时间。
    """
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}", headers=headers)
        if response.status_code != 200:
            return {"status": f"http_{response.status_code}", "raw": response.text[:200]}
        body = response.json()
        if body.get("status") in {"succeeded", "failed", "canceled"}:
            return body
        time.sleep(0.25)
    body["status"] = body.get("status", "timeout") + "_timeout"
    return body


def run_checks(
    base_url: str,
    check: Checker,
    *,
    create_key,
    container: str | None = None,
) -> None:
    """冒烟的全部断言。`create_key` 由目标决定（本机子进程 / docker exec）。"""

    client = httpx.Client(base_url=base_url, timeout=20.0)

    # 01 活着
    response = client.get("/health")
    check.expect(
        response.status_code == 200,
        "/health 存活",
        response.text[:80],
        f"HTTP {response.status_code} {response.text[:120]}",
    )

    # 02 就绪：库已迁移到「源码所认为的最新版本」。
    # 对比的是本地源码的 LATEST 而不是容器自报的数字——镜像里漏打一个迁移文件时，
    # 容器自己会认为「我已是最新」，只有拿源码版本对账才抓得到。
    response = client.get("/ready")
    ready = response.json() if response.status_code == 200 else {}
    schema = str(ready.get("schema_version", ""))
    check.expect(
        response.status_code == 200 and schema == str(migrate.LATEST),
        "/ready 就绪且 schema 与源码一致",
        f"schema=v{schema} auth={ready.get('auth')}",
        f"HTTP {response.status_code} schema={schema or '?'} 期望 v{migrate.LATEST}：{response.text[:160]}",
    )
    check.expect(
        ready.get("auth") == "required",
        "/ready 回报 auth=required",
        "",
        f"auth={ready.get('auth')!r}——鉴权没打开，任务谁都能提交",
    )

    # 03 没 key 必须 401，且给 WWW-Authenticate
    response = client.get("/v1/jobs")
    check.expect(
        response.status_code == 401 and "www-authenticate" in {k.lower() for k in response.headers},
        "无密钥访问 /v1/jobs → 401 + WWW-Authenticate",
        "",
        f"HTTP {response.status_code} headers={dict(response.headers)}",
    )

    # 04 建两把 key：主租户 + 旁观租户（用于验证越权 404）
    key_a = create_key("smoke-a")
    key_b = create_key("smoke-b")
    if not key_a:
        check.fail("创建租户密钥", "拿不到主租户密钥，后续断言无法进行")
        return
    auth_a = {"Authorization": f"Bearer {key_a}"}
    auth_b = {"Authorization": f"Bearer {key_b}"} if key_b else None
    check.ok("创建租户密钥", "tenant=smoke-a" + (" / smoke-b" if key_b else "（无旁观密钥）"))

    # 05 样本数据在交付物里（镜像漏 COPY data/samples 时这里红）
    response = client.get("/v1/datasets", headers=auth_a)
    datasets = response.json().get("datasets", []) if response.status_code == 200 else []
    check.expect(
        response.status_code == 200 and bool(datasets),
        "/v1/datasets 可读",
        ", ".join(datasets[:4]),
        f"HTTP {response.status_code} {response.text[:120]}",
    )

    # 06 提交分析任务 → 202 且立刻返回（不阻塞等模型）
    response = client.post(
        "/v1/jobs", json={"kind": "analyze", "reviews": SMOKE_REVIEWS, "top_k": 3}, headers=auth_a
    )
    submitted = response.json() if response.status_code == 202 else {}
    job_id = submitted.get("job_id", "")
    check.expect(
        response.status_code == 202 and bool(job_id),
        "提交分析任务 → 202",
        f"{job_id} status={submitted.get('status')}",
        f"HTTP {response.status_code} {response.text[:200]}",
    )
    if not job_id:
        return

    # 08 轮询到终态。失败在这里的意义是「worker 没起来」或「迁移没跑」，
    # 所以把最后状态原样打出来，而不是只说一句超时。
    done = _wait_job(client, job_id, auth_a)
    check.expect(
        done.get("status") == "succeeded",
        "分析任务执行完成",
        f"llm_calls={done.get('result', {}).get('llm_calls')}",
        f"最终状态={done.get('status')} error={done.get('error_code')} {str(done.get('error_message'))[:160]}",
    )
    result = done.get("result") or {}
    check.expect(
        bool(result.get("themes")),
        "结果含主题与证据",
        f"{len(result.get('themes') or [])} 个主题 · {result.get('review_count')} 条评论",
        f"result={str(result)[:200]}",
    )

    # 10 幂等：同键同体重放 → 同一个 job_id
    idem = {"Idempotency-Key": "smoke-analyze-1", **auth_a}
    first = client.post(
        "/v1/jobs", json={"kind": "analyze", "reviews": SMOKE_REVIEWS, "top_k": 3}, headers=idem
    )
    second = client.post(
        "/v1/jobs", json={"kind": "analyze", "reviews": SMOKE_REVIEWS, "top_k": 3}, headers=idem
    )
    first_id = first.json().get("job_id")
    second_id = second.json().get("job_id")
    check.expect(
        first.status_code == 202 and second.status_code == 202 and first_id == second_id,
        "同键重放 → 同一 job_id",
        f"{first_id} · Idempotency-Replayed={second.headers.get('idempotency-replayed')}",
        f"{first.status_code}/{second.status_code} {first_id} vs {second_id}",
    )

    # 11 同键换体 → 409。返回旧任务会让调用方以为新参数生效。
    conflict = client.post(
        "/v1/jobs",
        json={"kind": "analyze", "reviews": SMOKE_REVIEWS[:1], "top_k": 3},
        headers=idem,
    )
    check.expect(
        conflict.status_code == 409,
        "同键换请求体 → 409",
        conflict.json().get("code", ""),
        f"HTTP {conflict.status_code} {conflict.text[:160]}",
    )

    # 12 越权读 → 404（不是 403：403 会确认这个 id 存在）
    if auth_b is None:
        check.skip("另一租户读同一任务 → 404", "未提供第二把密钥")
    else:
        peek = client.get(f"/v1/jobs/{job_id}", headers=auth_b)
        check.expect(
            peek.status_code == 404,
            "另一租户读同一任务 → 404",
            "",
            f"HTTP {peek.status_code} {peek.text[:160]}",
        )

    # 13 台账：同一次尝试只记一笔
    detail = client.get(f"/v1/jobs/{job_id}", headers=auth_a)
    executions = detail.json().get("executions", []) if detail.status_code == 200 else []
    check.expect(
        len(executions) == 1,
        "执行台账每次尝试一条",
        f"attempt={executions[0].get('attempt') if executions else '?'}",
        f"executions={len(executions)} 条",
    )

    # 14 决策任务（另一条业务线，纯本地计算不耗 token）
    response = client.post(
        "/v1/jobs", json={"kind": "decision", **DECISION_PAYLOAD}, headers=auth_a
    )
    decision_id = response.json().get("job_id", "") if response.status_code == 202 else ""
    check.expect(
        response.status_code == 202 and bool(decision_id),
        "提交决策任务 → 202",
        decision_id,
        f"HTTP {response.status_code} {response.text[:200]}",
    )
    if decision_id:
        done = _wait_job(client, decision_id, auth_a)
        result = done.get("result") or {}
        verdict = result.get("verdict")
        # 只断言「是三种结论之一」，不断言具体是哪一种：
        # 具体结论取决于种子合规规则是否核实，那是数据状态而不是链路健康度。
        # 把 NO_GO 写死进冒烟，等于以后每核实一条规则都要改冒烟脚本。
        check.expect(
            done.get("status") == "succeeded" and verdict in VERDICTS and bool(result.get("headline")),
            "决策任务出结论",
            f"verdict={verdict} · {str(result.get('headline'))[:48]}",
            f"status={done.get('status')} verdict={verdict!r} headline={result.get('headline')!r}",
        )

    client.close()

    if container:
        # 15 非 root：镜像里 USER 写错、或 RUN chown 之后又切回 root，都会在这里露出来
        whoami = _docker("exec", container, "id", "-u")
        uid = whoami.stdout.decode().strip()
        check.expect(
            whoami.returncode == 0 and uid == "10001",
            "容器以非 root 运行",
            f"uid={uid}",
            f"uid={uid!r} 期望 10001",
        )


def _healthcheck_status(container: str, timeout: float = 60.0) -> str:
    """等 Docker 自己把 healthcheck 跑出结果。

    这一项只能由容器运行时判定：`HEALTHCHECK` 写错（URL 拼错、端口写死）时，
    docker 会一直报 starting 然后 unhealthy，而应用层看起来完全正常。
    """
    deadline = time.monotonic() + timeout
    status = "starting"
    while time.monotonic() < deadline:
        finished = _docker("inspect", "--format", "{{.State.Health.Status}}", container)
        if finished.returncode != 0:
            return "inspect_failed"
        status = finished.stdout.decode().strip()
        if status in {"healthy", "unhealthy"}:
            return status
        time.sleep(2.0)
    return status


# ---------- 入口 ----------


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="交付冒烟")
    parser.add_argument("--target", choices=["local", "docker", "remote"], default="local")
    parser.add_argument("--base-url", default="", help="remote 目标地址")
    parser.add_argument("--api-key", default="", help="remote 目标的主租户密钥")
    parser.add_argument("--api-key-b", default="", help="remote 目标的第二个租户密钥（验证越权 404）")
    parser.add_argument("--keep", action="store_true", help="失败时保留临时目录/容器")
    parser.add_argument("--skip-healthcheck", action="store_true", help="跳过容器 HEALTHCHECK 判定")
    parser.add_argument("--ready-timeout", type=float, default=45.0)
    parser.add_argument(
        "--base-image",
        default="",
        help="docker 目标的基础镜像（覆盖 Dockerfile 的 PYTHON_IMAGE，用于 Docker Hub 不可达时的替代源）",
    )
    args = parser.parse_args(argv)

    _rule("=")
    _echo(f"交付冒烟 · target={args.target}")
    _rule("=")

    check = Checker()
    tmp: Path | None = None
    proc: subprocess.Popen | None = None
    container: str | None = None
    log_path: Path | None = None

    try:
        if args.target == "remote":
            if not args.base_url or not args.api_key:
                _echo("[环境错误] remote 需要 --base-url 与 --api-key")
                return 2
            base_url = args.base_url.rstrip("/")
            ok, detail = _wait_ready(base_url, args.ready_timeout)
            if not ok:
                _echo(f"[环境错误] {detail}")
                return 2
            run_checks(
                base_url,
                check,
                create_key=lambda tenant: (
                    args.api_key_b if tenant == "smoke-b" else args.api_key
                ) or None,
            )

        elif args.target == "local":
            tmp = Path(tempfile.mkdtemp(prefix="xb-smoke-"))
            try:
                base_url, proc, log_path = _start_local(tmp)
            except RuntimeError as exc:
                _echo(f"[环境错误] {exc}")
                return 2
            ok, detail = _wait_ready(base_url, args.ready_timeout, proc)
            if not ok:
                _echo(f"[环境错误] 服务未能就绪：{detail}")
                _dump_log(log_path)
                return 2
            run_checks(
                base_url, check,
                create_key=lambda tenant: _create_key_local(tenant, tmp),
            )

        else:  # docker
            available, detail = _docker_available()
            if not available:
                _echo(f"[环境错误] docker 不可用：{detail}")
                _echo("          先启动 Docker Desktop，或用 --target local 验证代码链路。")
                return 2
            _echo(f"  docker engine {detail}")
            container = f"xb-smoke-{os.getpid()}"
            _docker("rm", "-f", container)
            try:
                base_url, container_id = _start_docker(container, base_image=args.base_image)
            except RuntimeError as exc:
                _echo(f"[环境错误] {exc}")
                return 2
            _echo(f"  容器 {container_id[:12]} → {base_url}")
            ok, detail = _wait_ready(base_url, args.ready_timeout)
            if not ok:
                _echo(f"[环境错误] 容器未能就绪：{detail}")
                _dump_docker_logs(container)
                return 2
            run_checks(
                base_url, check,
                create_key=lambda tenant: _create_key_docker(container, tenant),
                container=container,
            )
            if not args.skip_healthcheck:
                status = _healthcheck_status(container)
                check.expect(
                    status == "healthy", "容器 HEALTHCHECK 判定健康", status, f"状态={status}"
                )

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if container is not None and not args.keep:
            _docker("rm", "-f", container)
        if tmp is not None:
            if check.failures and not args.keep:
                # 失败时保留现场：日志是唯一能说明「为什么没起来」的东西
                _dump_log(log_path)
                _echo(f"  现场保留：{tmp}")
            elif not check.failures:
                shutil.rmtree(tmp, ignore_errors=True)

    _rule()
    total = check.index
    if check.failures:
        _echo(f"冒烟失败：{check.failures}/{total} 项不通过")
        return 1
    _echo(f"冒烟通过：{total}/{total}")
    return 0


def _dump_log(path: Path | None, lines: int = 40) -> None:
    if path is None or not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    if not text:
        return
    _echo("  服务日志（末尾）：")
    for line in text[-lines:]:
        _echo(f"    {line}")


def _dump_docker_logs(container: str, lines: int = 40) -> None:
    finished = _docker("logs", "--tail", str(lines), container)
    text = (finished.stdout + finished.stderr).decode("utf-8", "replace").strip()
    if text:
        _echo("  容器日志（末尾）：")
        for line in text.splitlines()[-lines:]:
            _echo(f"    {line}")


if __name__ == "__main__":
    raise SystemExit(main())
