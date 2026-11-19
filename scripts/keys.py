"""API Key 运维 CLI。

用法：
    python scripts/keys.py create --tenant acme --label "acme-prod" --quota 200000 --rpm 120
    python scripts/keys.py list
    python scripts/keys.py list --tenant acme
    python scripts/keys.py revoke --prefix xb_abcd1234
    python scripts/keys.py revoke --hash <64 位哈希> --yes
    python scripts/keys.py stale --days 90

`create` 会把明文打印**一次**。库里只有 sha256，之后任何「帮我查一下这把钥匙」
的请求都只能通过新建一把来解决——找回明文的能力本身就是漏洞。
所以创建时请当场把它写进密钥管理系统，不要留在终端回滚缓冲里。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xborder_insight.db.connection import connect, resolve_path  # noqa: E402
from xborder_insight.security import keys  # noqa: E402


def _fmt(value: object) -> str:
    if value is None:
        return "不限"
    return str(value)


def _cmd_create(conn, args: argparse.Namespace) -> int:
    raw, row = keys.create(
        conn,
        tenant_id=args.tenant,
        label=args.label or "",
        daily_token_quota=args.quota,
        rpm_limit=args.rpm,
    )
    print("已创建 API Key（明文只显示这一次）：")
    print("-" * 58)
    print(f"  {raw}")
    print("-" * 58)
    print(f"  租户      {row['tenant_id']}")
    print(f"  标识      {row['label'] or '(无)'}")
    print(f"  前缀      {row['key_prefix']}  （吊销时可用它指代这把 key）")
    print(f"  日额度    {_fmt(row['daily_token_quota'])} token")
    print(f"  限流      {row['rpm_limit']} 次/分钟")
    print()
    print("客户端用法：")
    print(f"  curl -H 'Authorization: Bearer {raw[:8]}…' http://127.0.0.1:8000/v1/jobs")
    return 0


def _cmd_list(conn, args: argparse.Namespace) -> int:
    rows = keys.list_keys(conn, tenant_id=args.tenant)
    if not rows:
        print("没有匹配的 key")
        return 0
    print(f"{'前缀':<14}{'租户':<16}{'日额度':>10}{'RPM':>6}  {'状态':<6}{'最近使用'}")
    print("-" * 78)
    for row in rows:
        status = "有效" if row["revoked_at"] is None else "已吊销"
        print(
            f"{row['key_prefix']:<14}{row['tenant_id']:<16}"
            f"{_fmt(row['daily_token_quota']):>10}{row['rpm_limit']:>6}  "
            f"{status:<6}{row['last_used_at'] or '从未'}"
        )
    return 0


def _cmd_revoke(conn, args: argparse.Namespace) -> int:
    identifier = args.hash or args.prefix
    if not identifier:
        print("需要 --prefix 或 --hash")
        return 2
    if args.hash and not args.yes:
        print("按哈希吊销需要 --yes（哈希不可逆，敲错就是吊销了另一把）")
        return 2
    row = keys.revoke(conn, identifier)
    print(f"已吊销 {row['key_prefix']}（租户 {row['tenant_id']}，revoked_at={row['revoked_at']}）")
    print("注意：吊销立即生效，下一次请求即返回 401。")
    return 0


def _cmd_stale(conn, args: argparse.Namespace) -> int:
    rows = keys.stale(conn, days=args.days)
    if not rows:
        print(f"没有超过 {args.days} 天未轮换的有效 key")
        return 0
    print(f"超过 {args.days} 天未轮换的有效 key（{len(rows)} 把）：")
    for row in rows:
        print(f"  {row['key_prefix']}  {row['tenant_id']:<16}{keys.key_age_days(row)} 天  {row['label']}")
    print("\n轮换方式：新建一把 → 切换客户端 → 确认无调用后吊销旧 key。")
    print("直接吊销会造成一个窗口期的 401，而这个窗口正是最需要可用的时刻。")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="API Key 运维")
    parser.add_argument("--db", default=None, help="数据库路径，默认取配置 DB_PATH")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="创建密钥")
    create.add_argument("--tenant", required=True)
    create.add_argument("--label", default="")
    create.add_argument("--quota", type=int, default=None, help="每日 token 额度，不传为不限")
    create.add_argument("--rpm", type=int, default=60, help="每分钟请求上限，默认 60")

    show = sub.add_parser("list", help="列出密钥")
    show.add_argument("--tenant", default=None)

    revoke = sub.add_parser("revoke", help="吊销密钥")
    revoke.add_argument("--prefix", default=None, help="创建时展示的明文前缀")
    revoke.add_argument("--hash", default=None, help="完整 key_hash（需配合 --yes）")
    revoke.add_argument("--yes", action="store_true")

    stale = sub.add_parser("stale", help="查看长期未轮换的密钥")
    stale.add_argument("--days", type=int, default=90)

    args = parser.parse_args(argv)
    conn = connect(resolve_path(args.db))
    try:
        return {
            "create": _cmd_create,
            "list": _cmd_list,
            "revoke": _cmd_revoke,
            "stale": _cmd_stale,
        }[args.command](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
