"""HTTP 服务层：只做请求解析、编排调用、响应格式化，不含业务逻辑。

启动：
    .venv/Scripts/python.exe -m uvicorn xborder_insight.api:app --app-dir src --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__, dashboard, metrics
from .config import ROOT_DIR, settings
from .db.connection import connect
from .db import migrate
from .db import runs as runs_db
from .errors import AppError, DbNotReady, FeatureDisabled, InputInvalid, Unauthorized
from .jobs import JobRunner
from .jobs import models as job_models
from .jobs import store as job_store
from .llm import build_llm_client
from .logging import log_event, new_request_id, request_id_var, setup_logging
from .pipeline.analyze import analyze_reviews
from .pipeline.ingest import load_reviews
from .schemas import (
    AnalysisReport,
    AnalyzeRequest,
    DecisionRequest,
    ErrorResponse,
    JobPayload,
)
from .security import keys as api_keys
from .security import quota as quota_mod
from .service import decide as decide_service

logger = logging.getLogger("xborder_insight.api")
DATA_DIR = ROOT_DIR / "data" / "samples"

# DB_PATH 允许写成相对路径（.env 里就是相对写法），统一按项目根解析，
# 否则从不同工作目录启动会连到不同的库——这类问题在本地很难被发现。
DB_FILE = Path(settings.db_path)
if not DB_FILE.is_absolute():
    DB_FILE = ROOT_DIR / DB_FILE


@contextmanager
def open_db():
    """短连接，用完即关。

    不做连接池：SQLite 本地文件的连接开销远小于「长连接被多线程共用」带来的
    锁与线程亲和问题。每次请求一条连接，语义最简单，出问题也最好查。
    """
    if not DB_FILE.exists():
        raise DbNotReady(
            "数据库尚未初始化",
            detail=f"缺少 {DB_FILE}，先执行: python scripts/db.py up",
        )
    conn = connect(DB_FILE)
    try:
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class Principal:
    """一次请求的身份。tenant 只从这里来，客户端不能自己声明。

    让客户端传 `tenant_id` 是最省事也最危险的做法：任何持有合法 key 的人
    都能填别人的租户，于是「隔离」变成一句注释。
    """

    tenant_id: str
    daily_token_quota: int | None
    rpm_limit: int | None
    key_prefix: str | None
    authenticated: bool


def _api_key_from(request: Request) -> str | None:
    """支持两种携带方式。`Authorization` 优先，且只认 Bearer。

    如果 `Authorization` 存在但 scheme 不是 Bearer（比如误用了 Basic），
    直接报错而不是回落到 `X-API-Key`——静默回落会让一个配置错误
    在本地看起来「能用」，在联调时突然变成 401。
    """
    header = request.headers.get("Authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            raise Unauthorized("鉴权头只支持 Bearer", detail="Authorization: Bearer <key>")
        return value.strip() or None
    return request.headers.get("X-API-Key")


def current_principal(request: Request) -> Principal:
    """鉴权 + 限流，一次连接里做完。

    顺序固定：先认人（401），再数次数（429）。反过来会把「你是谁」
    泄漏给未鉴权的调用方——被限流的人至少知道了限额是多少。
    """
    if not settings.auth_required:
        return Principal(
            tenant_id="local",
            daily_token_quota=None,
            rpm_limit=None,
            key_prefix=None,
            authenticated=False,
        )

    raw = _api_key_from(request)
    with open_db() as conn:
        row = api_keys.authenticate(conn, raw)
        quota_mod.hit(conn, str(row["tenant_id"]), int(row["rpm_limit"]))
    return Principal(
        tenant_id=str(row["tenant_id"]),
        daily_token_quota=row["daily_token_quota"],
        rpm_limit=int(row["rpm_limit"]),
        key_prefix=str(row["key_prefix"]),
        authenticated=True,
    )


def _metrics_principal(request: Request) -> Principal:
    """`/metrics` 的鉴权：认人，但不计限流。

    不能复用 `current_principal`：它每调一次就 `quota.hit()` 记一个限流窗口。
    Prometheus 默认 15 秒抓一次，一分钟四次抓取就会吃掉 rpm 配额，抓取开始
    被 429——「监控挂了」和「服务挂了」同时发生，而且恰好是在最需要指标的时候。

    鉴权本身不能省：指标里有租户名与额度，免密暴露等于给了一个绕过
    「跨租户一律 404」的侧信道。
    """
    if not settings.auth_required:
        return Principal(
            tenant_id="local",
            daily_token_quota=None,
            rpm_limit=None,
            key_prefix=None,
            authenticated=False,
        )

    raw = _api_key_from(request)
    with open_db() as conn:
        row = api_keys.authenticate(conn, raw)
    return Principal(
        tenant_id=str(row["tenant_id"]),
        daily_token_quota=row["daily_token_quota"],
        rpm_limit=int(row["rpm_limit"]),
        key_prefix=str(row["key_prefix"]),
        authenticated=True,
    )


def _enforce_quota(conn: sqlite3.Connection, principal: Principal) -> None:
    """配额检查。`daily_token_quota is None` 表示不限量。"""
    quota_mod.assert_quota(conn, principal.tenant_id, principal.daily_token_quota)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    app.state.llm = build_llm_client(settings)

    if not settings.auth_required:
        # 不静默：这个状态下任何人都能提交任务、花掉你的 token 预算。
        # 本地开发可以接受，但必须在日志里留下明确的痕迹。
        log_event(logger, "auth.disabled", hint="所有 /v1/* 接口视为租户 local，无鉴权、无限流")

    # 重启恢复：把「running 但租约已过期」的任务放回队列。
    # 库不存在/未迁移时不能让进程起不来——那是部署步骤缺失，交给 /ready 报告，
    # 否则一次忘跑迁移会让服务彻底不可观测（连健康检查都进不去）。
    recovered: dict[str, list[str]] = {"requeued": [], "failed": []}
    try:
        with open_db() as conn:
            recovered = job_store.requeue_expired(conn)
    except (DbNotReady, sqlite3.Error) as exc:
        log_event(logger, "app.recovery_skipped", reason=type(exc).__name__, detail=str(exc)[:200])

    app.state.job_stop = asyncio.Event()
    app.state.job_runner = JobRunner(
        session=open_db,
        client=app.state.llm,
        settings=settings,
        lease_seconds=settings.job_lease_seconds,
        concurrency=settings.job_concurrency,
    )
    app.state.job_task = None
    if settings.jobs_worker_enabled:
        app.state.job_task = asyncio.create_task(
            app.state.job_runner.serve(
                stop=app.state.job_stop, poll_interval=settings.job_poll_seconds
            )
        )

    log_event(
        logger,
        "app.startup",
        run_mode=settings.run_mode,
        client=app.state.llm.name,
        version=__version__,
        worker=settings.jobs_worker_enabled,
        recovered_requeued=len(recovered["requeued"]),
        recovered_failed=len(recovered["failed"]),
    )
    yield

    app.state.job_stop.set()
    if app.state.job_task is not None:
        with suppress(asyncio.CancelledError):
            await app.state.job_task
    await app.state.llm.aclose()
    log_event(logger, "app.shutdown")


app = FastAPI(
    title="跨境选品决策引擎",
    version=__version__,
    description="把竞品评论转成可排序、可溯源的选品机会清单",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or new_request_id()
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = int((time.perf_counter() - started) * 1000)
        log_event(logger, "request.failed", path=request.url.path, elapsed_ms=elapsed)
        request_id_var.reset(token)
        raise
    elapsed = int((time.perf_counter() - started) * 1000)
    log_event(
        logger,
        "request.done",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        elapsed_ms=elapsed,
    )
    request_id_var.reset(token)
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    log_event(logger, "app.error", code=exc.code, detail=exc.detail or "")
    # headers 目前用于 429 的 Retry-After 与 401 的 WWW-Authenticate。
    # 少了 Retry-After，客户端只能靠猜——猜短了继续撞墙，猜长了白等。
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_public(request_id_var.get()),
        headers=exc.headers or None,
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    message = f"{'.'.join(str(p) for p in first.get('loc', ()))}: {first.get('msg', '入参不合法')}"
    return JSONResponse(
        status_code=422,
        content={"code": "input_invalid", "message": message,
                 "request_id": request_id_var.get()},
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "message": "服务内部错误",
                 "request_id": request_id_var.get()},
    )


def _list_datasets() -> list[str]:
    return sorted(p.stem for p in DATA_DIR.glob("*.jsonl"))


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok", "run_mode": settings.run_mode}


@app.get("/ready", tags=["ops"])
async def ready() -> dict[str, str]:
    """就绪检查：配置可用、数据目录可读、数据库已迁移到最新版本。

    「库文件存在」不等于「库可用」：文件在但结构是上一版时，服务会一路跑到
    第一个写任务表的请求才 500，而 /ready 一直报 ok——这种「健康检查说健康」
    的故障，排查成本远高于启动时直接拒绝。
    """
    if not DATA_DIR.exists():
        raise InputInvalid("数据目录不可用", detail=str(DATA_DIR))
    if not settings.is_mock and not settings.llm_api_key:
        raise InputInvalid("live 模式缺少 LLM_API_KEY")
    if not DB_FILE.exists():
        raise DbNotReady(
            "数据库尚未初始化",
            detail=f"缺少 {DB_FILE}，先执行: python scripts/db.py up",
        )
    with open_db() as conn:
        version = migrate.current_version(conn)
    if version != migrate.LATEST:
        raise DbNotReady(
            "数据库结构未升级",
            detail=f"当前 v{version}，需要 v{migrate.LATEST}；执行: python scripts/db.py up",
        )
    # 鉴权状态放进就绪响应：部署方可以据此在网关层面做判断，
    # 而不是靠人去记住「这次有没有配 AUTH_REQUIRED」。
    #
    # 聚类阈值同理，而且更隐蔽：它有两个来源（provider 标定值 / 环境变量覆盖），
    # 两种都能让服务正常启动，但产出的主题细分粒度完全不同。不回显的话，
    # 「线上跑的是标定值还是某次临时改的值」只能靠翻部署记录猜。
    return {
        "status": "ready",
        "schema_version": str(version),
        "auth": "required" if settings.auth_required else "disabled",
        "embedding": settings.embedding_provider,
        "cluster_threshold": f"{settings.cluster_threshold:g}",
        "cluster_threshold_source": settings.cluster_threshold_source,
    }


@app.get("/metrics", tags=["ops"])
async def prometheus_metrics(principal: Principal = Depends(_metrics_principal)) -> Response:
    """运行指标：Prometheus 文本格式，聚合 `jobs` / `runs` 台账，不建计数器。

    三个运维端点分工必须分清，否则故障时只能翻日志猜：

        /health   进程还活着吗（进程内、不碰库）
        /ready    现在能接请求吗（库结构、鉴权、配置）
        /metrics  过去这段时间它到底干了什么（任务成功率、延迟、token、配额）

    指标口径以台账为准（`metrics.py` 顶部有理由）。这里只做取数 + 格式化，
    不写库、不改状态——`GET` 语义必须保持只读。
    """
    with open_db() as conn:
        body = metrics.collect(
            conn,
            tenant_id=principal.tenant_id,
            daily_quota=principal.daily_token_quota,
        )
    return Response(content=body, media_type=metrics.CONTENT_TYPE)


@app.get("/v1/datasets", tags=["analysis"])
async def list_datasets(principal: Principal = Depends(current_principal)) -> dict[str, list[str]]:
    """可用的样本数据集。鉴权只为统一入口，不含租户数据。"""
    return {"datasets": _list_datasets()}


@app.post("/v1/analyze", response_model=AnalysisReport, tags=["analysis"])
async def analyze(
    payload: AnalyzeRequest, principal: Principal = Depends(current_principal)
) -> AnalysisReport:
    """同步分析。**生产环境默认关闭**。

    它直接烧模型 token 却不落 `jobs` 台账，也就是说这条路绕过 token 配额。
    配额若能被绕过就等于没有，所以把开关交给部署方显式打开，
    而不是留一个「反正也没人滥用」的默认开口。
    """
    if not settings.sync_analyze_enabled:
        # 替代方案写进 message 而不只是 detail：对外响应只暴露 code/message/request_id，
        # 写进 detail 等于只留在日志里——调用方只看到「不开放」，不知道该怎么办。
        raise FeatureDisabled(
            "同步分析接口在此环境未开放，请改用 POST /v1/jobs（kind=analyze）",
            detail="生产环境以异步任务为准：同步路径不落台账，不受 token 配额约束",
        )
    if payload.dataset:
        reviews = load_reviews(payload.dataset)
        dataset_name = payload.dataset
    else:
        reviews = list(payload.reviews or [])
        dataset_name = "inline"
    if not reviews:
        raise InputInvalid("没有可分析的评论")

    # 同步接口只检查不记账：额度用尽的租户仍然被挡住，
    # 但它产生的消耗不计入台账，因此边界处可能略超——这条已写进 README 已知限制。
    with open_db() as conn:
        _enforce_quota(conn, principal)

    result = await analyze_reviews(
        reviews=reviews,
        settings=settings,
        client=app.state.llm,
        dataset=dataset_name,
        top_k=payload.top_k,
    )
    return result.report


def run_decision(payload: DecisionRequest) -> dict:
    """同步执行一次选品决策。

    逻辑在 `service.decide()`，与异步任务共用同一份实现——
    否则「同步接口」和「异步任务」会各自演化，结果对不上时无法判断谁是对的。
    """
    with open_db() as conn:
        return decide_service(conn, payload)


@app.post(
    "/v1/decision",
    tags=["decision"],
    response_description=(
        "三档结论 + 逐条依据 + 利润明细 + `returns` 块（走 sku_key 时含两档毛利、"
        "「值不值得谈」与证据强弱）+ `warnings`"
    ),
    responses={
        422: {
            "model": ErrorResponse,
            "description": (
                "入参不合法（`input_invalid`）。最常见的两种：包装事实与 `fba_fee` 同时给；"
                "`sku_key` 与 `cost.return_rate` 同时给（退货率只能有一个来源）。"
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": (
                "结论依赖的数据未就绪。决策接口会出现 `return_rate_missing`"
                "（`sku_key` 未在 product_returns 登记）；"
                "参数闸门类错误见 runbook §7——那是设计，不是故障。"
            ),
        },
    },
)
async def decide_product(
    payload: DecisionRequest, principal: Principal = Depends(current_principal)
) -> dict:
    """按成本档案 + 合规规则表出 GO / NO-GO / 需补充信息。

    成本数据由调用方提供（`data_provenance` 会原样回显），
    合规结论来自系统内置规则表——当前规则全为 draft，因此该品类会返回
    NO_GO 并标明「含未核实项」。这是有意的：不确定就说不确定。

    退货率有两个来源，二选一（`docs/04` §30）：带 `sku_key` 走
    `product_returns` 的两档，响应多一个 `returns` 块（含压力档与证据强弱）；
    不带则用 `cost.return_rate`，没有压力档。两个都给是 422——
    基线来自调用方、压力档来自库，两档不同源，比出来的差值是假的。

    六项成本不留默认值（`docs/04` §31）：不填会进 `profit.missing_inputs`，
    并把结论从 PASS 降级为 NEED_MORE_INFO——按 0 算出来的是上界，上界不得称 PASS。

    不做配额判定：本接口不调用模型，token 消耗为零，按额度拦它属于误伤。
    """
    return run_decision(payload)


# ---------- 任务化：把"一次请求"与"一次执行"解耦 ----------


def _submit_job(
    payload: JobPayload,
    idempotency_key: str | None,
    principal: Principal,
) -> tuple[dict, bool]:
    body = payload.model_dump()
    kind = body.pop("kind")
    with open_db() as conn:
        # 额度检查要跳过「重放」：重放不会产生新的 token 消耗，
        # 若在额度用尽后连查结果都被 429 挡住，调用方就永远拿不到已经付过钱的结果。
        replay = job_store.find_by_idempotency(conn, principal.tenant_id, idempotency_key)
        if replay is None and kind == job_models.KIND_ANALYZE:
            _enforce_quota(conn, principal)

        row, created = job_store.submit(
            conn,
            job_id=f"job-{uuid4().hex[:16]}",
            kind=kind,
            payload=body,
            tenant_id=principal.tenant_id,
            idempotency_key=idempotency_key,
            max_attempts=settings.job_max_attempts,
        )
        return job_store.to_public(row, include_result=False), created


@app.post("/v1/jobs", status_code=202, tags=["jobs"])
async def submit_job(
    payload: JobPayload,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(current_principal),
) -> dict:
    """提交任务，立即返回 202，不等待执行完成。

    幂等：带同一个 `Idempotency-Key` 重发，返回同一个 job_id（`idempotent_replay=true`），
    不会产生第二个任务。键相同但请求体不同 → 409，不返回旧任务。

    租户从密钥派生：同一个 `Idempotency-Key` 在两个租户下互不影响，
    也不可能用别人的键去「认领」别人的任务。
    """
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip() or None

    body, created = _submit_job(payload, idempotency_key, principal)
    body["idempotent_replay"] = not created
    response.headers["Idempotency-Replayed"] = "false" if created else "true"
    body["links"] = {"self": f"/v1/jobs/{body['job_id']}"}
    log_event(
        logger,
        "job.submitted",
        job_id=body["job_id"],
        kind=body["kind"],
        replayed=not created,
        tenant=principal.tenant_id,
    )
    return body


@app.get("/v1/jobs", tags=["jobs"])
async def list_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    principal: Principal = Depends(current_principal),
) -> dict:
    if status is not None and status not in {*job_models.ALLOWED}:
        raise InputInvalid(f"未知状态: {status}")
    with open_db() as conn:
        rows = job_store.list_jobs(
            conn, status=status, tenant_id=principal.tenant_id, limit=limit
        )
    return {"jobs": [job_store.to_public(row, include_result=False) for row in rows]}


@app.get("/v1/jobs/{job_id}", tags=["jobs"])
async def get_job(job_id: str, principal: Principal = Depends(current_principal)) -> dict:
    """任务详情。成功时带 `result`，并附每次执行的台账（重试过的会有多条）。"""
    with open_db() as conn:
        row = job_store.get_scoped(conn, job_id, principal.tenant_id)
        body = job_store.to_public(row)
        body["executions"] = [
            {
                "run_id": run["run_id"],
                "attempt": run["attempt"],
                "status": run["status"],
                "dataset": run["dataset"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "error_code": run["error_code"],
                "llm_calls": run["llm_calls"],
                "total_tokens": run["prompt_tokens"] + run["completion_tokens"],
            }
            for run in runs_db.runs_for_job(conn, job_id)
        ]
    return body


@app.post("/v1/jobs/{job_id}/retry", status_code=202, tags=["jobs"])
async def retry_job(
    job_id: str, response: Response, principal: Principal = Depends(current_principal)
) -> dict:
    """重试失败的任务。成功或仍在排队的任务不能重试（409）。

    归属校验与详情接口一致：重试等于替这个租户再花一次钱，
    不能让持有任意合法 key 的人替你花。
    """
    with open_db() as conn:
        row = job_store.retry(conn, job_id, tenant_id=principal.tenant_id)
        body = job_store.to_public(row, include_result=False)
    response.headers["Idempotency-Replayed"] = "false"
    body["links"] = {"self": f"/v1/jobs/{job_id}"}
    log_event(logger, "job.retried", job_id=job_id, tenant=principal.tenant_id)
    return body


@app.get("/", response_class=HTMLResponse, tags=["ui"])
async def index(dataset: str = Query(default="")) -> HTMLResponse:
    datasets = _list_datasets()
    if not datasets:
        raise InputInvalid("没有可用的数据集")
    current = dataset if dataset in datasets else datasets[0]
    reviews = load_reviews(current)
    result = await analyze_reviews(
        reviews=reviews,
        settings=settings,
        client=app.state.llm,
        dataset=current,
        top_k=6,
    )
    return HTMLResponse(dashboard.render(result.report, datasets, current))
