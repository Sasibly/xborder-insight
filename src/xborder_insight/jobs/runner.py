"""任务执行器：领取 → 执行 → 落结果，进程内异步 worker。

四个刻意的设计：

1. **状态全部落库，内存里不留任务**
   队列在 `jobs` 表，租约也在表里。进程被 kill -9 后重启，任何「running 但租约过期」
   的任务都会被回收重跑。把队列放内存里（asyncio.Queue）写起来更快，
   但重启即丢，且丢得毫无痕迹。

2. **心跳续租**
   一次 500 条评论的分析可能跑几分钟，租约必然过期。不续租的话，任务会在自己
   还在跑的时候被别人重新领取——同一份工作付两遍钱，而且两边都"成功"。
   续租按租约的 1/3 周期进行，比固定周期更稳：租约调长调短都不用改心跳。

3. **执行期间不吞异常**
   失败要把 error_code 落到任务上，并让 `runs` 台账记一笔 failed。吞异常的
   执行器看起来永远健康，这是运维最难查的一类故障。

4. **`drain()` 是给测试和 CLI 用的同步语义**
   后台 worker 是异步常驻的，测试里等它「有空」很别扭且易 flaky。
   提供 drain() 把队列跑到空，测试就变成确定性的。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import uuid4

from ..config import Settings
from ..llm import LLMClient
from ..logging import log_event
from . import store
from .handlers import HANDLERS, Handler, JobContext

import logging

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


def new_worker_id() -> str:
    # 带上主机名与 pid：多实例部署时，从任务行上就能看出是谁领的
    host = socket.gethostname()[:16]
    return f"{host}-{os.getpid()}-{uuid4().hex[:6]}"


class JobRunner:
    def __init__(
        self,
        *,
        session: SessionFactory,
        client: LLMClient,
        settings: Settings,
        handlers: dict[str, Handler] | None = None,
        worker_id: str | None = None,
        lease_seconds: float = 120.0,
        concurrency: int = 2,
    ) -> None:
        self._session = session
        self._client = client
        self._settings = settings
        # 用 `is None` 而不是 `or`：显式传入的空注册表是「什么都不处理」，
        # 不是「没传，用默认的」。`handlers or HANDLERS` 会让空注册表静默变成默认表，
        # 测试里表现为「明明配了空注册表，任务却成功了」。
        self._handlers = HANDLERS if handlers is None else handlers
        self.worker_id = worker_id or new_worker_id()
        self.lease_seconds = lease_seconds
        self.concurrency = max(1, concurrency)

    # ---------- 单个任务 ----------

    async def process_one(self) -> bool:
        """领取并执行一个任务。返回 False 表示队列已空。"""
        with self._session() as conn:
            row = store.claim(conn, self.worker_id, lease_seconds=self.lease_seconds)
        if row is None:
            return False

        job_id = str(row["job_id"])
        attempt = int(row["attempts"])
        kind = str(row["kind"])
        payload: dict[str, Any] = json.loads(row["payload"])
        log_event(logger, "job.claimed", job_id=job_id, kind=kind, attempt=attempt)

        handler = self._handlers.get(kind)
        if handler is None:
            # 不认识的类型要立刻判死，否则它会一直被领取、一直失败
            with self._session() as conn:
                store.fail(conn, job_id, "unknown_kind")
            log_event(logger, "job.unknown_kind", job_id=job_id, kind=kind)
            return True

        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        try:
            with self._session() as conn:
                outcome = await handler(
                    payload,
                    JobContext(
                        conn=conn,
                        client=self._client,
                        settings=self._settings,
                        job_id=job_id,
                        attempt=attempt,
                        request_fingerprint=str(row["request_fingerprint"]),
                    ),
                )
            with self._session() as conn:
                store.complete(
                    conn,
                    job_id,
                    result=outcome.result,
                    llm_calls=outcome.llm_calls,
                    prompt_tokens=outcome.prompt_tokens,
                    completion_tokens=outcome.completion_tokens,
                )
            log_event(
                logger,
                "job.succeeded",
                job_id=job_id,
                kind=kind,
                attempt=attempt,
                llm_calls=outcome.llm_calls,
            )
        except asyncio.CancelledError:
            # 关闭时的取消不记失败：任务仍是 running，租约到期后会被重新领取。
            # 记成 failed 反而会误导——它没有失败，是被中断了。
            raise
        except Exception as exc:
            code = getattr(exc, "code", "internal_error")
            with self._session() as conn:
                with contextlib.suppress(Exception):
                    store.fail(conn, job_id, code)
            logger.exception("job failed: %s", job_id)
            log_event(logger, "job.failed", job_id=job_id, kind=kind, error_code=code)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
        return True

    async def _heartbeat(self, job_id: str) -> None:
        interval = max(self.lease_seconds / 3.0, 0.2)
        while True:
            await asyncio.sleep(interval)
            with self._session() as conn:
                kept = store.extend_lease(
                    conn, job_id, self.worker_id, lease_seconds=self.lease_seconds
                )
            if not kept:
                # 续不上说明已被回收（或被别人领走）：继续跑只会重复计费
                log_event(logger, "job.lease_lost", job_id=job_id, worker_id=self.worker_id)
                return

    # ---------- 队列 ----------

    async def drain(self, *, max_jobs: int = 100) -> int:
        """把队列跑到空。测试与 CLI 用，确定性优先，不并发。"""
        done = 0
        while done < max_jobs and await self.process_one():
            done += 1
        return done

    async def serve(self, *, stop: asyncio.Event, poll_interval: float = 0.5) -> None:
        """常驻轮询。并发度 = 启动的轮询协程数。"""
        loops = [
            asyncio.create_task(self._poll(stop, poll_interval)) for _ in range(self.concurrency)
        ]
        try:
            await asyncio.gather(*loops)
        finally:
            for task in loops:
                task.cancel()
            for task in loops:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _poll(self, stop: asyncio.Event, poll_interval: float) -> None:
        while not stop.is_set():
            try:
                worked = await self.process_one()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 轮询协程不能因为一次异常就退出：库未迁移、短暂锁冲突这类问题
                # 修好后应当自动恢复。一个悄悄死掉的 worker 会让队列看起来只是"很慢"，
                # 而日志里什么都没有。
                log_event(
                    logger,
                    "job.loop_error",
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                worked = False
            if worked:
                continue
            # 用 wait 而不是 sleep：关闭时能立刻退出，不必等满一个轮询周期
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
