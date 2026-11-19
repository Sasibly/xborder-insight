"""任务处理器：把「提交的任务」翻译成一次领域调用。

约束：处理器只负责取数、调用、记录台账，不做任何业务判断。
判断留在 domain/，这样处理器出问题时定位很快——要么取数错了，要么记录错了。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..db import runs
from ..errors import InputInvalid
from ..llm import LLMClient
from ..logging import log_event
from ..pipeline.analyze import analyze_reviews
from ..pipeline.ingest import load_reviews
from ..schemas import AnalyzeRequest, DecisionRequest
from ..service import decide
from . import models

import logging

logger = logging.getLogger(__name__)


@dataclass
class JobContext:
    conn: sqlite3.Connection
    client: LLMClient
    settings: Settings
    job_id: str
    attempt: int
    request_fingerprint: str


@dataclass
class JobOutcome:
    result: dict[str, Any]
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    run_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[dict[str, Any], JobContext], Awaitable[JobOutcome]]


def _new_run_id() -> str:
    return f"run-{uuid4().hex[:12]}"


async def handle_analyze(payload: dict[str, Any], ctx: JobContext) -> JobOutcome:
    """评论分析任务。

    每次执行都写一条 `runs` 台账（含 job_id + attempt）：
    分析任务的 token 消耗是主要成本，不落台账就等于成本失控而无人知晓。
    """
    request = AnalyzeRequest.model_validate(payload)
    dataset = request.dataset if request.dataset is not None else "inline"

    # 台账开在取数之前：数据源不存在、解析失败这类错误也要留下一条记录。
    # 若等取数成功再开台账，失败的尝试在账上根本不存在——查「为什么这批没数据」
    # 时只能去翻日志，而日志会过期。
    run_id = _new_run_id()
    runs.create_run(
        ctx.conn,
        run_id,
        dataset,
        request_fingerprint=ctx.request_fingerprint,
        job_id=ctx.job_id,
        attempt=ctx.attempt,
    )

    try:
        if request.dataset is not None:
            reviews = load_reviews(request.dataset)
        else:
            reviews = list(request.reviews or [])
        if not reviews:
            raise InputInvalid("没有可分析的评论")

        analysed = await analyze_reviews(
            reviews=reviews,
            settings=ctx.settings,
            client=ctx.client,
            dataset=dataset,
            top_k=request.top_k,
        )
    except BaseException as exc:
        runs.fail_run(ctx.conn, run_id, getattr(exc, "code", "internal_error"))
        raise

    report = analysed.report
    runs.finish_run(
        ctx.conn,
        run_id,
        review_count=report.review_count,
        parsed_count=report.parsed_count,
        parse_success_rate=report.parse_success_rate,
        llm_calls=report.llm_calls,
        prompt_tokens=report.prompt_tokens,
        completion_tokens=report.completion_tokens,
        latency_p95_ms=float(report.elapsed_ms),
        params_snapshot={
            "cluster_threshold": ctx.settings.cluster_threshold,
            # 快照必须能区分「标定值」与「环境变量覆盖」：两次任务主题粒度不同时，
            # 只有这一项能解释差异，否则会被误当成模型波动。
            "cluster_threshold_source": ctx.settings.cluster_threshold_source,
            "embedding_provider": ctx.settings.embedding_provider,
            "embedding_model": ctx.settings.embedding_model,
            "run_mode": ctx.settings.run_mode,
        },
    )

    return JobOutcome(
        result=report.model_dump(),
        llm_calls=report.llm_calls,
        prompt_tokens=report.prompt_tokens,
        completion_tokens=report.completion_tokens,
        run_id=run_id,
        extra={"unparsed_ids": analysed.unparsed_ids, "repair_attempts": analysed.repair_attempts},
    )


async def handle_decision(payload: dict[str, Any], ctx: JobContext) -> JobOutcome:
    """选品决策任务。纯计算 + 查规则表，不调用模型，因此不计 token。"""
    request = DecisionRequest.model_validate(payload)
    body = decide(ctx.conn, request)
    log_event(
        logger,
        "job.decision",
        job_id=ctx.job_id,
        verdict=body.get("verdict"),
    )
    return JobOutcome(result=body)


HANDLERS: dict[str, Handler] = {
    models.KIND_ANALYZE: handle_analyze,
    models.KIND_DECISION: handle_decision,
}
