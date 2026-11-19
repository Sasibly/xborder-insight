"""运行台账 + 状态机。

为什么必须有状态机：
`runs` 表的 status 一旦允许业务代码随便 UPDATE，就会出现「status=succeeded
但 finished_at 为空」「先 failed 又变 succeeded」这类账实不符。这类脏数据
不会让程序报错，只会让成本统计和对账永远对不上——而这恰恰是运维最先要用的东西。

所以规则写在这里，且只有这一处能改 status：
    running → succeeded | failed
终态不可回退。API 层、脚本层、以后的任务执行器都必须经过这里。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..errors import InvalidStateTransition
from .connection import transaction

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

ALLOWED: dict[str, frozenset[str]] = {
    RUNNING: frozenset({SUCCEEDED, FAILED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    dataset: str
    status: str

    @property
    def is_terminal(self) -> bool:
        return not ALLOWED.get(self.status, frozenset())


def create_run(
    conn: sqlite3.Connection,
    run_id: str,
    dataset: str,
    *,
    request_fingerprint: str | None = None,
    job_id: str | None = None,
    attempt: int = 1,
) -> None:
    """开一条执行台账。

    `job_id + attempt` 上有部分唯一索引：同一次尝试只能记一次账。
    这**不是**「重试不计费」——重试真的又调了一次模型，就该再记一笔，
    但必须是新的一条（新的 attempt），而不是把上一条覆盖或复制。
    """
    with transaction(conn):
        conn.execute(
            "INSERT INTO runs (run_id, dataset, status, started_at, request_fingerprint, "
            "job_id, attempt) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, dataset, RUNNING, _now(), request_fingerprint, job_id, attempt),
        )


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"run 不存在: {run_id}")
    return row


def _transition(
    conn: sqlite3.Connection, run_id: str, to_status: str, fields: dict[str, object]
) -> None:
    with transaction(conn):
        row = get_run(conn, run_id)
        from_status = str(row["status"])
        if to_status not in ALLOWED.get(from_status, frozenset()):
            raise InvalidStateTransition(
                f"不允许的状态流转: {from_status} → {to_status}",
                detail=f"run_id={run_id}",
            )

        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values())
        conn.execute(
            f"UPDATE runs SET status = ?, finished_at = ?, {assignments} WHERE run_id = ?",
            [to_status, _now(), *values, run_id],
        )


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    review_count: int,
    parsed_count: int,
    parse_success_rate: float,
    llm_calls: int,
    prompt_tokens: int,
    completion_tokens: int,
    latency_p95_ms: float | None = None,
    params_snapshot: dict[str, object] | None = None,
) -> None:
    _transition(
        conn,
        run_id,
        SUCCEEDED,
        {
            "review_count": review_count,
            "parsed_count": parsed_count,
            "parse_success_rate": parse_success_rate,
            "llm_calls": llm_calls,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_p95_ms": latency_p95_ms,
            # 存快照而非引用：参数会变，历史账不能跟着变
            "params_snapshot": (
                json.dumps(params_snapshot, ensure_ascii=False, sort_keys=True)
                if params_snapshot is not None
                else None
            ),
        },
    )


def fail_run(conn: sqlite3.Connection, run_id: str, error_code: str) -> None:
    _transition(conn, run_id, FAILED, {"error_code": error_code})


def runs_for_job(conn: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    """某个任务的全部执行记录，按尝试次数升序。

    重试过的任务会有多条：失败那次也烧了 token，成本口径必须包含它。
    """
    return conn.execute(
        "SELECT * FROM runs WHERE job_id = ? ORDER BY attempt ASC", (job_id,)
    ).fetchall()


def token_totals(conn: sqlite3.Connection, dataset: str | None = None) -> sqlite3.Row:
    """成本归因的最小查询：按数据集汇总 token 与调用次数，只统计成功 run。"""
    where = "WHERE status = 'succeeded'"
    params: list[object] = []
    if dataset:
        where += " AND dataset = ?"
        params.append(dataset)
    return conn.execute(
        f"SELECT dataset, COUNT(*) AS runs, SUM(llm_calls) AS llm_calls, "
        f"SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, "
        f"SUM(prompt_tokens + completion_tokens) AS total_tokens "
        f"FROM runs {where} GROUP BY dataset ORDER BY dataset",
        params,
    ).fetchall()
