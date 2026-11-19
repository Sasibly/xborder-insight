"""租户限流与配额。

两个不同的限制，名字必须分开：

    限流（rate limit）—— 单位时间请求数。问题是「太急」，解法是退避重试
    配额（quota）     —— 当日 token 总量。问题是「用完了」，重试没有意义

合成一个 429 是常见的省事做法，但调用方拿到的信息不足以决定该做什么：
立即重试（限流）会一直撞墙，等到明天（配额）则白等一天。

**配额不另建计数器**，直接聚合 `jobs` 台账。理由：计数器一定会漂移——
补记、重试、改口径、手工修数据，任何一次都会让计数器和真实消耗对不上，
而配额错了要么少收钱要么误伤客户。台账是唯一真相，聚合它就不会自相矛盾。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..db.connection import transaction
from ..errors import QuotaExceeded, RateLimited

WINDOW_SECONDS = 60
# 只保留一小时内的窗口行。限流表的全部价值在于「当前窗口」，
# 历史留在库里除了让表变大之外没有用途。
CLEANUP_SECONDS = 3600


@dataclass(frozen=True)
class Usage:
    jobs: int
    tokens: int

    def as_dict(self) -> dict[str, int]:
        return {"jobs": self.jobs, "tokens": self.tokens}


def _now() -> datetime:
    return datetime.now()


def _day_bounds(day: str | None = None) -> tuple[str, str]:
    """返回当日的 [起, 止) 字符串边界。

    `created_at` 存的是 `2026-11-18T10:13:59.123`，
    所以区间比较必须带上 `T...` 而不只是日期——只写 `'2026-11-18'` 时，
    字符串比较会把当天所有记录都算成「大于边界」，边界就形同不存在。
    """
    target = date.fromisoformat(day) if day else date.today()
    start = datetime.combine(target, datetime.min.time())
    end = start + timedelta(days=1)
    return (
        start.isoformat(timespec="milliseconds"),
        end.isoformat(timespec="milliseconds"),
    )


def usage_today(conn: sqlite3.Connection, tenant_id: str, *, day: str | None = None) -> Usage:
    """当日消耗。只统计已记录 token 的任务——还没跑完的任务尚未产生消耗。

    这也意味着配额是**软限**：一个正在跑的大任务可能把余额用穿。
    这是有意的取舍——中途杀掉正在跑的任务，损失比超一点额更大。
    """
    start, end = _day_bounds(day)
    row = conn.execute(
        "SELECT COUNT(*) AS jobs, "
        "COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens "
        "FROM jobs WHERE tenant_id = ? AND created_at >= ? AND created_at < ?",
        (tenant_id, start, end),
    ).fetchone()
    return Usage(jobs=int(row["jobs"]), tokens=int(row["tokens"]))


def assert_quota(
    conn: sqlite3.Connection, tenant_id: str, quota: int | None, *, day: str | None = None
) -> Usage:
    """额度检查。`quota is None` 表示不限量。

    判定用 `>=`：额度是「今天最多能花多少」，花到刚好等于上限时就该停，
    而不是允许再花一次才发现超额。
    """
    usage = usage_today(conn, tenant_id, day=day)
    if quota is not None and usage.tokens >= quota:
        raise QuotaExceeded(
            "当日 token 额度已用尽",
            detail=f"tenant={tenant_id} 已用 {usage.tokens}/{quota} token；次日 00:00 重置",
        )
    return usage


def hit(
    conn: sqlite3.Connection,
    tenant_id: str,
    limit: int,
    *,
    window_seconds: int = WINDOW_SECONDS,
    moment: datetime | None = None,
) -> int:
    """记一次请求并返回本窗口累计次数。超限抛 `RateLimited`。

    计数先提交、再抛错：如果在同一个事务里抛，被拒的请求不会计数，
    那么「被拒」本身就是绕过限流的手段——反复重试永远是第一个请求。
    """
    now = moment or _now()
    epoch = int(now.timestamp())
    start_epoch = epoch - epoch % window_seconds
    window_start = datetime.fromtimestamp(start_epoch).isoformat(timespec="seconds")

    with transaction(conn):
        row = conn.execute(
            "INSERT INTO rate_limits (tenant_id, window_start, hits) VALUES (?, ?, 1) "
            "ON CONFLICT(tenant_id, window_start) DO UPDATE SET hits = rate_limits.hits + 1 "
            "RETURNING hits",
            (tenant_id, window_start),
        ).fetchone()
        hits = int(row["hits"])
        if hits == 1:
            _cleanup(conn, moment=now)

    if hits > limit:
        # 窗口内剩余秒数，至少 1：Retry-After: 0 会让客户端立刻重试并再次被拒
        retry_after = max(1, window_seconds - (epoch - start_epoch))
        raise RateLimited(
            "请求过于频繁",
            retry_after=retry_after,
            detail=f"限制 {limit} 次/{window_seconds}s，当前窗口已 {hits} 次",
        )
    return hits


def _cleanup(conn: sqlite3.Connection, *, moment: datetime | None = None) -> None:
    cutoff = (moment or _now()) - timedelta(seconds=CLEANUP_SECONDS)
    conn.execute(
        "DELETE FROM rate_limits WHERE window_start < ?",
        (cutoff.isoformat(timespec="seconds"),),
    )


def reset(conn: sqlite3.Connection, tenant_id: str) -> int:
    """清空某租户的限流计数。仅用于测试与运维解封，不对外暴露。"""
    with transaction(conn):
        cursor = conn.execute("DELETE FROM rate_limits WHERE tenant_id = ?", (tenant_id,))
    return cursor.rowcount
