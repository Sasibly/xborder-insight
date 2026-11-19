"""任务存储层：幂等提交、租约领取、状态流转。

四个关键实现选择：

1. **幂等靠唯一索引，不靠「先查再插」**
   先 SELECT 再 INSERT 在并发下必然漏：两个请求同时查到「不存在」，然后都插入。
   正确做法是直接 INSERT，撞唯一键后再回查——让数据库做仲裁。

2. **同一个幂等键 + 不同请求体 = 冲突，不是重放**
   如果直接返回旧 job，调用方会以为「新请求已被接受」，实际上跑的是老参数。
   这种错不会报错，只会让结论莫名其妙。所以比对指纹，不同就拒。

3. **领取用 `WHERE status='queued'` 做乐观锁**
   先选出候选 id，再用带状态条件的 UPDATE 抢占；`rowcount == 1` 才算抢到。
   这样多个 worker 并发领取也不会重复执行，无需分布式锁。

4. **租约（lease）而非「重启时把所有 running 改成 queued」**
   后者在多实例部署时会误杀另一个实例正在跑的任务。租约到期才回收，
   单实例多实例都成立。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ..db.connection import transaction
from ..errors import IdempotencyConflict, InvalidStateTransition, JobNotFound
from ..versions import VersionTriple, current as current_versions
from . import models

# 领取时最多重试几次候选：每次失败说明被别人抢走了，重试即可
_CLAIM_RETRIES = 8


def _now() -> str:
    # 毫秒精度：秒级会让同一秒内提交的多个任务排序不稳定，
    # 而 FIFO 是队列最容易被观察到、也最容易被质疑的性质。
    return datetime.now().isoformat(timespec="milliseconds")


def _plus(seconds: float) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def now() -> str:
    """库内时间格式的统一出口。运维脚本比较租约时用它，避免自己拼格式。"""
    return _now()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def submit(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    kind: str,
    payload: dict[str, Any],
    tenant_id: str = "local",
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    versions: VersionTriple | None = None,
) -> tuple[sqlite3.Row, bool]:
    """提交任务。返回 (任务行, 是否新建)。

    幂等键相同且请求体相同 → 返回已有任务，`created=False`；
    幂等键相同但请求体不同 → 抛 `IdempotencyConflict`（409）。
    """
    if kind not in models.KINDS:
        raise ValueError(f"未知任务类型: {kind}")
    triple = versions or current_versions()
    fp = models.fingerprint(payload)

    values = (
        job_id,
        tenant_id,
        kind,
        models.QUEUED,
        idempotency_key,
        fp,
        _json(payload),
        max_attempts,
        triple.prompt_version,
        triple.model,
        triple.taxonomy_version,
        _now(),
    )

    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO jobs (job_id, tenant_id, kind, status, idempotency_key, "
                "request_fingerprint, payload, max_attempts, prompt_version, model, "
                "taxonomy_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
    except sqlite3.IntegrityError:
        # 唯一键冲突有两种可能：幂等键重复，或 job_id 撞了（极小概率）。
        # 只有前者可以当作重放处理，后者必须原样抛出，否则会静默丢失新任务。
        existing = _by_idempotency_key(conn, tenant_id, idempotency_key)
        if existing is None:
            raise
        _assert_same_request(existing, fp)
        return existing, False

    return get(conn, job_id), True


def find_by_idempotency(
    conn: sqlite3.Connection, tenant_id: str, key: str | None
) -> sqlite3.Row | None:
    """按幂等键查已有任务。

    对外暴露是为了让调用方能在「额度检查」之前判断这是不是重放：
    重放不产生新消耗，不该因为额度用完而连结果都查不到。
    """
    return _by_idempotency_key(conn, tenant_id, key)


def _by_idempotency_key(
    conn: sqlite3.Connection, tenant_id: str, key: str | None
) -> sqlite3.Row | None:
    if key is None:
        return None
    return conn.execute(
        "SELECT * FROM jobs WHERE tenant_id = ? AND idempotency_key = ?",
        (tenant_id, key),
    ).fetchone()


def _assert_same_request(row: sqlite3.Row, fp: str) -> None:
    if str(row["request_fingerprint"]) != fp:
        raise IdempotencyConflict(
            "同一幂等键被用于不同请求",
            detail=(
                f"job_id={row['job_id']} 已存在且请求体不同；"
                "重试请复用原请求体，或换一个新的幂等键"
            ),
        )


def get(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobNotFound(f"任务不存在: {job_id}")
    return row


def get_scoped(conn: sqlite3.Connection, job_id: str, tenant_id: str) -> sqlite3.Row:
    """按租户取任务。不是自己的一律当作不存在。

    这里刻意**不返回 403**：403 等于告诉调用方「这个 id 存在，只是不给你看」，
    于是 job_id 变成一个可枚举的探测接口——能探测出别人有多少任务、什么时候提交的。
    对外表现必须是「查无此物」，与真正的不存在完全一致。
    """
    row = get(conn, job_id)
    if str(row["tenant_id"]) != tenant_id:
        raise JobNotFound(f"任务不存在: {job_id}")
    return row


def claim(
    conn: sqlite3.Connection, worker_id: str, *, lease_seconds: float
) -> sqlite3.Row | None:
    """领取一个排队任务并开启租约。队列空时返回 None。"""
    for _ in range(_CLAIM_RETRIES):
        candidate = conn.execute(
            "SELECT job_id FROM jobs WHERE status = ? ORDER BY created_at, job_id LIMIT 1",
            (models.QUEUED,),
        ).fetchone()
        if candidate is None:
            return None

        job_id = str(candidate["job_id"])
        with transaction(conn):
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, started_at = ?, lease_expires_at = ?, "
                "worker_id = ?, attempts = attempts + 1 "
                "WHERE job_id = ? AND status = ? AND attempts < max_attempts",
                (models.RUNNING, _now(), _plus(lease_seconds), worker_id, job_id, models.QUEUED),
            )
        if cursor.rowcount == 1:
            return get(conn, job_id)

        # 抢不到有两种原因：被别人抢走（重试即可），或重试预算已耗尽（必须判死，
        # 否则这条队列会被它永久堵住，看起来只是「队列不动了」）。
        still_queued = conn.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE job_id = ? AND status = ?",
            (job_id, models.QUEUED),
        ).fetchone()
        if still_queued is not None and still_queued["attempts"] >= still_queued["max_attempts"]:
            fail(conn, job_id, "retry_exhausted")
            continue
        if still_queued is None:
            continue
        return None
    return None


def extend_lease(
    conn: sqlite3.Connection, job_id: str, worker_id: str, *, lease_seconds: float
) -> bool:
    """心跳续期。只有当前持有者能续，避免被抢后旧 worker 又续上。"""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE jobs SET lease_expires_at = ? "
            "WHERE job_id = ? AND status = ? AND worker_id = ?",
            (_plus(lease_seconds), job_id, models.RUNNING, worker_id),
        )
    return cursor.rowcount == 1


def complete(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    result: dict[str, Any],
    llm_calls: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    _finish(
        conn,
        job_id,
        models.SUCCEEDED,
        fields={
            "result": _json(result),
            "error_code": None,
            "llm_calls": llm_calls,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    )


def fail(conn: sqlite3.Connection, job_id: str, error_code: str) -> None:
    _finish(conn, job_id, models.FAILED, fields={"error_code": error_code})


def _finish(
    conn: sqlite3.Connection, job_id: str, to_status: str, *, fields: dict[str, Any]
) -> None:
    with transaction(conn):
        row = get(conn, job_id)
        from_status = str(row["status"])
        if not models.can_transition(from_status, to_status):
            raise InvalidStateTransition(
                f"不允许的任务状态流转: {from_status} → {to_status}",
                detail=f"job_id={job_id}",
            )
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(
            f"UPDATE jobs SET status = ?, finished_at = ?, worker_id = NULL, "
            f"lease_expires_at = NULL, {assignments} WHERE job_id = ?",
            [to_status, _now(), *fields.values(), job_id],
        )


def retry(conn: sqlite3.Connection, job_id: str, *, tenant_id: str | None = None) -> sqlite3.Row:
    """人工重试：failed → queued。

    重试预算已耗尽时**抬高预算**而不是绕开约束：
    自动重试的次数上限是成本保护，人工重试是一个显式决定，
    所以正确做法是「把预算改成至少够这一次」，让库的 CHECK 继续成立。

    `tenant_id` 给定时会先校验归属：否则任何持有合法 key 的人都能重试别人的任务，
    而重试等于替别人花钱。
    """
    with transaction(conn):
        row = get_scoped(conn, job_id, tenant_id) if tenant_id is not None else get(conn, job_id)
        if str(row["status"]) != models.FAILED:
            raise InvalidStateTransition(
                f"只有 failed 任务可以重试，当前是 {row['status']}",
                detail=f"job_id={job_id}",
            )
        attempts = int(row["attempts"])
        new_max = max(int(row["max_attempts"]), attempts + 1)
        conn.execute(
            "UPDATE jobs SET status = ?, started_at = NULL, finished_at = NULL, "
            "worker_id = NULL, lease_expires_at = NULL, error_code = NULL, max_attempts = ? "
            "WHERE job_id = ? AND status = ?",
            (models.QUEUED, new_max, job_id, models.FAILED),
        )
    return get(conn, job_id)


def requeue_expired(conn: sqlite3.Connection, *, now: str | None = None) -> dict[str, list[str]]:
    """回收租约过期的 running 任务。进程重启后调用，也是「不永久卡死」的唯一保证。

    预算未耗尽 → 回到 queued；已耗尽 → 直接判 failed。
    判死而不是留在 running，是因为留在 running 的任务永远不会被任何人处理，
    而它看起来「正在运行」——比明确失败更难发现。
    """
    moment = now or _now()
    rows = conn.execute(
        "SELECT job_id, attempts, max_attempts FROM jobs "
        "WHERE status = ? AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
        (models.RUNNING, moment),
    ).fetchall()

    requeued: list[str] = []
    failed: list[str] = []
    for row in rows:
        job_id = str(row["job_id"])
        if int(row["attempts"]) >= int(row["max_attempts"]):
            fail(conn, job_id, "lease_expired")
            failed.append(job_id)
            continue
        with transaction(conn):
            conn.execute(
                "UPDATE jobs SET status = ?, started_at = NULL, lease_expires_at = NULL, "
                "worker_id = NULL WHERE job_id = ? AND status = ?",
                (models.QUEUED, job_id, models.RUNNING),
            )
        requeued.append(job_id)
    return {"requeued": requeued, "failed": failed}


def archive_finished(
    conn: sqlite3.Connection,
    *,
    older_than_days: int,
    dry_run: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    """把 N 天前完成的任务结果清空，保留任务行。

    **为什么不清行**：幂等靠 `UNIQUE(tenant_id, idempotency_key)`。行没了，
    幂等键也就没了，同一个键重放会变成一条新任务——重新花一次钱，
    而调用方看到的响应和正常重放一模一样。归档只能清重字段，不能清键。

    **为什么默认 dry_run**：这是个批量写操作，影响面取决于表里有多少历史数据。
    默认只报「会动多少行、能省多少字节」，要真动手必须显式关掉 dry-run。
    与 `db.py down` 要求 `--yes` 是同一个取舍：破坏性动作不设默认路径。
    """
    if older_than_days < 1:
        raise ValueError(f"older_than_days 必须 >= 1，当前值: {older_than_days}")

    moment = now or _now()
    cutoff = (
        datetime.fromisoformat(moment) - timedelta(days=older_than_days)
    ).isoformat(timespec="milliseconds")

    statuses = tuple(sorted(models.TERMINAL))
    placeholders = ", ".join("?" for _ in statuses)
    condition = (
        f"status IN ({placeholders}) AND finished_at IS NOT NULL "
        "AND finished_at < ? AND result IS NOT NULL AND archived_at IS NULL"
    )
    params: tuple[Any, ...] = (*statuses, cutoff)

    summary = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(length(result)), 0) AS bytes FROM jobs WHERE {condition}",
        params,
    ).fetchone()
    job_ids = [
        str(row["job_id"])
        for row in conn.execute(f"SELECT job_id FROM jobs WHERE {condition}", params).fetchall()
    ]

    if dry_run or not job_ids:
        return {
            "candidates": int(summary["n"]),
            "bytes": int(summary["bytes"]),
            "archived": [],
            "cutoff": cutoff,
            "dry_run": True,
        }

    with transaction(conn):
        conn.execute(
            f"UPDATE jobs SET result = NULL, archived_at = ? WHERE {condition}",
            (moment, *params),
        )
    return {
        "candidates": len(job_ids),
        "bytes": int(summary["bytes"]),
        "archived": job_ids,
        "cutoff": cutoff,
        "dry_run": False,
    }


def list_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    tenant_id: str | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if tenant_id:
        where.append("tenant_id = ?")
        params.append(tenant_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM jobs {clause} ORDER BY created_at DESC, job_id DESC LIMIT ?",
        params,
    ).fetchall()


def usage_totals(conn: sqlite3.Connection, *, tenant_id: str | None = None) -> list[sqlite3.Row]:
    """成本归因：按任务类型汇总 token 与调用次数，只统计成功任务。

    与 `runs.token_totals` 的口径一致（只算 succeeded），两处口径必须相同，
    否则「任务层花了多少」和「执行层花了多少」会永远对不上账。
    """
    where = ["status = ?"]
    params: list[Any] = [models.SUCCEEDED]
    if tenant_id:
        where.append("tenant_id = ?")
        params.append(tenant_id)
    return conn.execute(
        f"SELECT kind, COUNT(*) AS jobs, SUM(llm_calls) AS llm_calls, "
        f"SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, "
        f"SUM(prompt_tokens + completion_tokens) AS total_tokens "
        f"FROM jobs WHERE {' AND '.join(where)} GROUP BY kind ORDER BY kind",
        params,
    ).fetchall()


def to_public(row: sqlite3.Row, *, include_result: bool = True) -> dict[str, Any]:
    """对外表示。刻意不返回 payload 之外的内部字段（worker_id 等）。"""
    body: dict[str, Any] = {
        "job_id": row["job_id"],
        "kind": row["kind"],
        "status": row["status"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error_code": row["error_code"],
        "request_fingerprint": row["request_fingerprint"],
        "archived_at": row["archived_at"],
        "versions": {
            "prompt_version": row["prompt_version"],
            "model": row["model"],
            "taxonomy_version": row["taxonomy_version"],
        },
        "usage": {
            "llm_calls": row["llm_calls"],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "total_tokens": row["prompt_tokens"] + row["completion_tokens"],
        },
    }
    if include_result and row["result"] is not None:
        body["result"] = json.loads(row["result"])
    elif include_result and row["archived_at"] is not None:
        # 「结果已归档」必须能和「本来就没有结果」区分开。
        # 少了这个标记，调用方看到的是一个没有 result 的成功任务，
        # 而「成功却没有结果」在任何人眼里都是 bug。
        body["result_archived"] = True
    return body
