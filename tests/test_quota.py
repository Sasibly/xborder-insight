"""限流与配额的回归测试。

两个最容易被写错的性质：

1. **被拒的请求也必须计数**
   如果计数写在抛错之后（或与抛错同一个事务里被回滚），
   被拒的请求就不计数，那么「一直重试」就能永远在第一个请求的位置，
   限流彻底失效。所以这里专门断言「抛错之后计数仍然增加了」。

2. **配额用 >= 判**
   额度是「最多能花多少」，花到刚好等于上限时就该停。
   写成 `>` 会允许再花一个完整任务，而那个任务可能很贵。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from xborder_insight.errors import QuotaExceeded, RateLimited
from xborder_insight.security import quota


def _job(conn, *, job_id, tenant_id, tokens=0, created_at=None):
    conn.execute(
        "INSERT INTO jobs (job_id, tenant_id, kind, status, request_fingerprint, payload, "
        "prompt_version, model, taxonomy_version, created_at, prompt_tokens, completion_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            job_id,
            tenant_id,
            "analyze",
            "queued",
            f"fp-{job_id}",
            "{}",
            "prompt-x",
            "model-x",
            "tax-x",
            created_at or datetime.now().isoformat(timespec="milliseconds"),
            tokens,
        ),
    )


# ---------- 配额 ----------


def test_usage_sums_todays_tokens_for_one_tenant(conn):
    _job(conn, job_id="j1", tenant_id="acme", tokens=40)
    _job(conn, job_id="j2", tenant_id="acme", tokens=60)
    _job(conn, job_id="j3", tenant_id="other", tokens=999)

    usage = quota.usage_today(conn, "acme")
    assert usage.jobs == 2
    assert usage.tokens == 100


def test_usage_excludes_other_days(conn):
    """边界必须真的排他，否则「每日额度」会变成「历史总额」。"""
    today = datetime.now()
    yesterday = (today - timedelta(days=1)).isoformat(timespec="milliseconds")
    tomorrow = (today + timedelta(days=1)).isoformat(timespec="milliseconds")

    _job(conn, job_id="j-yesterday", tenant_id="acme", tokens=500, created_at=yesterday)
    _job(conn, job_id="j-tomorrow", tenant_id="acme", tokens=700, created_at=tomorrow)
    _job(conn, job_id="j-today", tenant_id="acme", tokens=10)

    assert quota.usage_today(conn, "acme").tokens == 10


def test_quota_none_means_unlimited(conn):
    _job(conn, job_id="j1", tenant_id="acme", tokens=10**9)
    usage = quota.assert_quota(conn, "acme", None)
    assert usage.tokens == 10**9


def test_quota_passes_below_limit(conn):
    _job(conn, job_id="j1", tenant_id="acme", tokens=99)
    quota.assert_quota(conn, "acme", 100)  # 不抛


def test_quota_blocks_exactly_at_limit(conn):
    """刚好用完就停：>= 而不是 >。"""
    _job(conn, job_id="j1", tenant_id="acme", tokens=100)
    with pytest.raises(QuotaExceeded) as exc:
        quota.assert_quota(conn, "acme", 100)
    assert "100/100" in (exc.value.detail or "")


def test_quota_is_per_tenant(conn):
    _job(conn, job_id="j1", tenant_id="noisy", tokens=1000)
    quota.assert_quota(conn, "quiet", 100)  # 另一租户不受影响


# ---------- 限流 ----------


def test_hit_counts_up_within_the_window(conn):
    moment = datetime(2026, 11, 18, 10, 0, 5)
    assert [quota.hit(conn, "acme", 10, moment=moment) for _ in range(3)] == [1, 2, 3]


def test_over_limit_raises_with_retry_after(conn):
    moment = datetime(2026, 11, 18, 10, 0, 50)
    for _ in range(2):
        quota.hit(conn, "acme", 2, moment=moment)

    with pytest.raises(RateLimited) as exc:
        quota.hit(conn, "acme", 2, moment=moment)
    # 10:00:50 起算，窗口还剩 10 秒
    assert exc.value.retry_after == 10
    assert exc.value.headers["Retry-After"] == "10"


def test_rejected_request_still_counts(conn):
    """关键性质：被拒的请求也要计数，否则无限重试就能一直绕开限流。"""
    moment = datetime(2026, 11, 18, 10, 0, 0)
    for _ in range(2):
        quota.hit(conn, "acme", 2, moment=moment)
    with pytest.raises(RateLimited):
        quota.hit(conn, "acme", 2, moment=moment)

    row = conn.execute("SELECT hits FROM rate_limits WHERE tenant_id = 'acme'").fetchone()
    assert row is not None, "被拒的请求没有留下计数：限流可以被「一直重试」绕过"
    assert row["hits"] == 3


def test_retry_after_is_never_zero(conn):
    """`Retry-After: 0` 会让客户端立刻重试、立刻被拒，形成自杀式重试风暴。"""
    moment = datetime(2026, 11, 18, 10, 0, 59)
    quota.hit(conn, "acme", 1, moment=moment)
    with pytest.raises(RateLimited) as exc:
        quota.hit(conn, "acme", 1, moment=moment)
    assert exc.value.retry_after >= 1


def test_new_window_resets(conn):
    first = datetime(2026, 11, 18, 10, 0, 30)
    later = first + timedelta(seconds=31)
    quota.hit(conn, "acme", 1, moment=first)
    with pytest.raises(RateLimited):
        quota.hit(conn, "acme", 1, moment=first)
    assert quota.hit(conn, "acme", 1, moment=later) == 1


def test_limits_are_per_tenant(conn):
    moment = datetime(2026, 11, 18, 10, 0, 0)
    quota.hit(conn, "a", 1, moment=moment)
    with pytest.raises(RateLimited):
        quota.hit(conn, "a", 1, moment=moment)
    assert quota.hit(conn, "b", 1, moment=moment) == 1


def test_old_windows_are_cleaned_up(conn):
    """限流表的唯一价值是当前窗口，历史行只会让表变大。"""
    old = datetime.now() - timedelta(hours=2)
    quota.hit(conn, "acme", 10, moment=old)
    assert conn.execute("SELECT COUNT(*) AS n FROM rate_limits").fetchone()["n"] == 1

    quota.hit(conn, "acme", 10)  # 新窗口的第一笔，顺手清理
    windows = [r["window_start"] for r in conn.execute("SELECT window_start FROM rate_limits")]
    assert len(windows) == 1
    assert windows[0] > old.isoformat(timespec="seconds")


def test_reset_clears_only_one_tenant(conn):
    moment = datetime(2026, 11, 18, 10, 0, 0)
    quota.hit(conn, "a", 5, moment=moment)
    quota.hit(conn, "b", 5, moment=moment)
    assert quota.reset(conn, "a") == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM rate_limits WHERE tenant_id='b'").fetchone()["n"] == 1
