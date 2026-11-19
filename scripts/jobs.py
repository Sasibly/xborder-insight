"""任务运维 CLI。

用法：
    python scripts/jobs.py status                 # 队列快照 + 成本归因
    python scripts/jobs.py list --status failed   # 列出任务
    python scripts/jobs.py show job-xxxx          # 单个任务详情（含执行台账）
    python scripts/jobs.py recover                # 回收租约过期的 running 任务
    python scripts/jobs.py drain                  # 在本进程里把队列跑到空
    python scripts/jobs.py archive --days 30      # 预演：哪些旧任务结果可以清
    python scripts/jobs.py archive --days 30 --yes  # 真清（保留任务行与幂等键）

`drain` 是给「不想起 HTTP 服务、只想把积压跑完」的场景用的：
它与 API 内的 worker 完全同一份执行器代码，所以不会出现
「脚本跑出来的结果和线上不一致」这种问题。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xborder_insight.config import settings  # noqa: E402
from xborder_insight.db import runs as runs_db  # noqa: E402
from xborder_insight.db.connection import connect, resolve_path  # noqa: E402
from xborder_insight.jobs import store  # noqa: E402
from xborder_insight.jobs.runner import JobRunner  # noqa: E402
from xborder_insight.llm import build_llm_client  # noqa: E402


@contextmanager
def session():
    conn = connect(resolve_path())
    try:
        yield conn
    finally:
        conn.close()


def _status() -> int:
    with session() as conn:
        counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status ORDER BY status"
        ).fetchall()
        print("队列状态")
        print("-" * 46)
        if not counts:
            print("  （空）")
        for row in counts:
            print(f"  {row['status']:<10}{row['n']:>5}")
        stale = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = 'running' "
            "AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
            (store.now(),),
        ).fetchone()["n"]
        if stale:
            print(f"\n⚠ {stale} 个 running 任务租约已过期，执行 recover 回收")

        usage = store.usage_totals(conn)
        print("\n成本归因（仅成功任务）")
        print("-" * 46)
        if not usage:
            print("  （无）")
        for row in usage:
            print(
                f"  {row['kind']:<10} 任务 {row['jobs']:>3} · 调用 {row['llm_calls'] or 0:>5} · "
                f"token {row['total_tokens'] or 0:>7}"
            )
    return 0


def _list(status: str | None) -> int:
    with session() as conn:
        rows = store.list_jobs(conn, status=status, limit=50)
    if not rows:
        print("没有匹配的任务")
        return 0
    print(f"{'job_id':<24}{'kind':<10}{'status':<11}{'attempts':<10}created_at")
    print("-" * 78)
    for row in rows:
        attempts = f"{row['attempts']}/{row['max_attempts']}"
        print(
            f"{row['job_id']:<24}{row['kind']:<10}{row['status']:<11}"
            f"{attempts:<10}{row['created_at']}"
        )
    return 0


def _show(job_id: str) -> int:
    with session() as conn:
        row = store.get(conn, job_id)
        executions = runs_db.runs_for_job(conn, job_id)
    print(json.dumps(store.to_public(row), ensure_ascii=False, indent=2))
    if executions:
        print("\n执行台账")
        print("-" * 70)
        for run in executions:
            print(
                f"  attempt {run['attempt']}  {run['status']:<10} "
                f"tokens={run['prompt_tokens'] + run['completion_tokens']:<7} "
                f"{run['error_code'] or ''}"
            )
    return 0


def _recover() -> int:
    with session() as conn:
        result = store.requeue_expired(conn)
    print(f"重新入队 {len(result['requeued'])} 个，判定失败 {len(result['failed'])} 个")
    if result["requeued"]:
        print(f"  {', '.join(result['requeued'])}")
    if result["failed"]:
        print(f"  {', '.join(result['failed'])}")
    return 0


def _drain() -> int:
    client = build_llm_client(settings)

    async def run() -> int:
        runner = JobRunner(
            session=session,
            client=client,
            settings=settings,
            lease_seconds=settings.job_lease_seconds,
        )
        done = await runner.drain()
        await client.aclose()
        return done

    done = asyncio.run(run())
    print(f"处理完成 {done} 个任务")
    return 0


def _archive(days: int, execute: bool) -> int:
    """归档旧任务结果。默认只预演。

    「预演」不是谨慎过度：这条命令一次能改掉几十万行，
    而它在任何一次误触里都不会报错——只会让调用方第二天发现结果查不到了。
    """
    if days < 1:
        # 在 CLI 层就拦，不让它变成 traceback：
        # 「--days 0」的真实含义是「把刚跑完的结果立刻清掉」，几乎不可能是本意。
        print("[拒绝执行] --days 必须 >= 1；0 表示归档刚完成的任务，等于立即丢掉结果。")
        return 2

    with session() as conn:
        result = store.archive_finished(conn, older_than_days=days, dry_run=not execute)

    head = "已归档" if execute else "[预演] 待归档"
    print(f"{head} {result['candidates']} 条任务结果 · 约 {result['bytes'] / 1024:.1f} KiB")
    print(f"  范围：finished_at < {result['cutoff']}（终态且仍有结果）")
    if not execute and result["candidates"]:
        print("  确认执行请加 --yes；任务行、执行台账、幂等键都会保留，只清 result。")
    if result["archived"]:
        for job_id in result["archived"][:10]:
            print(f"    {job_id}")
        if len(result["archived"]) > 10:
            print(f"    …共 {len(result['archived'])} 条")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务运维")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="队列快照与成本归因")

    list_parser = sub.add_parser("list", help="列出任务")
    list_parser.add_argument("--status", default=None)

    show_parser = sub.add_parser("show", help="任务详情")
    show_parser.add_argument("job_id")

    sub.add_parser("recover", help="回收租约过期的任务")
    sub.add_parser("drain", help="在本进程把队列跑到空")

    archive_parser = sub.add_parser("archive", help="归档 N 天前完成的任务结果（默认预演）")
    archive_parser.add_argument("--days", type=int, default=30)
    archive_parser.add_argument("--yes", action="store_true", help="确认执行（默认只预演）")

    args = parser.parse_args(argv)

    if args.command == "status":
        return _status()
    if args.command == "list":
        return _list(args.status)
    if args.command == "show":
        return _show(args.job_id)
    if args.command == "recover":
        return _recover()
    if args.command == "drain":
        return _drain()
    if args.command == "archive":
        return _archive(args.days, args.yes)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
