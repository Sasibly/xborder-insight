"""数据库运维 CLI。

用法：
    python scripts/db.py status              # 当前版本与迁移清单
    python scripts/db.py up                  # 升级到最新
    python scripts/db.py up --target 1       # 只升到 0001
    python scripts/db.py down --to 0 --yes   # 全部回滚（破坏性，必须 --yes）
    python scripts/db.py roundtrip           # 在临时库验证 up→down→up，不动真库
    python scripts/db.py params              # 参数核实进度
    python scripts/db.py fba                 # FBA 费率维表覆盖率与缺口

破坏性操作一律要求 --yes，且默认作用在配置的 DB_PATH 上；
要动真库请显式传 --db。默认值永远是最安全的那条路径。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xborder_insight.db import fba as fba_db  # noqa: E402
from xborder_insight.db import migrate  # noqa: E402
from xborder_insight.db.connection import MEMORY, connect, resolve_path  # noqa: E402
from xborder_insight.db.params import coverage, list_params  # noqa: E402


def _dump_status(conn) -> int:
    rows = migrate.status(conn)
    version = migrate.current_version(conn)
    print(f"当前版本 {version} / 最新 {migrate.LATEST}")
    print("-" * 58)
    for row in rows:
        mark = "✓" if row.applied else "·"
        stamp = row.applied_at or "未应用"
        print(f"  {mark} {row.label:<20} {stamp}")
    pending = migrate.pending(conn)
    if pending:
        print(f"\n待执行 {len(pending)} 个：{', '.join(pending)}")
    return 0


def _dump_params(conn) -> int:
    info = coverage(conn)
    ranged = {p.param_key: p for p in list_params(conn) if p.has_range}
    print(f"成本参数核实进度  市场={info['marketplace']}")
    print("-" * 58)
    print(f"  总数 {info['total']} 项 · 已核实 {info['verified']} · 待核实 {info['draft']}")
    if info["verified"]:
        print("  已核实：")
        for key in info["verified_keys"]:
            param = ranged.get(key)
            suffix = f"   区间 {param.range_label}" if param else "   区间 全区间"
            print(f"    · {key}{suffix}")
    if info["missing_keys"]:
        print("  待核实：")
        for key in info["missing_keys"]:
            print(f"    · {key}")
    print(f"\n  已声明适用区间 {len(ranged)} 项 / 共 {info['total']} 项")
    print("\n注：未核实的参数无法被业务代码使用（require() 会抛 param_unverified）。")
    print("    声明了区间的参数，若 require() 的 applied_to 越界会抛 param_out_of_range。")
    return 0


def _fmt_oz(value: float | None) -> str:
    return "∞" if value is None else f"{value:g}"


def _dump_fba(conn, marketplace: str) -> int:
    audit = fba_db.audit(conn, marketplace)
    print(f"FBA 配送费维表  市场={marketplace}")
    print("-" * 58)
    print(
        f"  共 {audit.total_rows} 行 · 已核实 {audit.verified_rows} · "
        f"待核实 {audit.draft_rows}"
    )
    print(
        f"  已声明角度组合 {audit.banded_dimensions} 个 · "
        f"重量缺口 {audit.gap_count} 处 · 区间重叠 {audit.overlap_count} 处"
    )
    for group in audit.groups:
        gap_text = (
            "无缺口"
            if not group.gaps
            else "缺口 " + "、".join(f"{_fmt_oz(lo)}–{_fmt_oz(hi)}" for lo, hi in group.gaps)
        )
        overlap_text = (
            "" if not group.overlaps
            else f"  ⚠ 重叠 {len(group.overlaps)} 处"
        )
        print(f"\n  · {group.key}  （{len(group.bands)} 档 / {gap_text}{overlap_text}）")
        for lo, hi in group.bands:
            print(f"      {_fmt_oz(lo):>5} – {_fmt_oz(hi):<5} oz")
    print(
        "\n注：这张表只登记了 large_standard / 非旺季 / 非服饰 / 未含燃油附加费的口径。"
    )
    print("    未核实行无法用于报价：resolve() 会抛 param_unverified（503），")
    print("    查询缺口不会外推到相邻尺寸段或相邻重量档。")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="数据库运维")
    parser.add_argument("--db", default=None, help="数据库路径，默认取配置 DB_PATH")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="升级")
    up.add_argument("--target", type=int, default=None)

    down = sub.add_parser("down", help="回滚")
    down.add_argument("--to", type=int, default=0)
    down.add_argument("--yes", action="store_true", help="确认执行破坏性操作")

    sub.add_parser("status", help="查看版本")
    sub.add_parser("roundtrip", help="在临时库验证可逆性")
    sub.add_parser("params", help="参数核实进度")
    fba_parser = sub.add_parser("fba", help="FBA 费率维表覆盖率与缺口")
    fba_parser.add_argument("--market", default="US")

    reset = sub.add_parser("reset", help="回滚到 0 再升级（清空数据）")
    reset.add_argument("--yes", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "roundtrip":
        # 刻意用临时文件而非真库：验证迁移不该有副作用。
        # 必须 try/finally 关连接，否则 Windows 上临时目录删不掉（文件被占用）。
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "roundtrip.sqlite3")
            try:
                result = migrate.roundtrip(conn)
            finally:
                conn.close()
        ok = bool(result["ok"])
        print(f"往返校验 {'通过' if ok else '失败'}  版本={result['version']}")
        print(f"  表：{', '.join(result['tables'])}")
        if result["leftover_after_down"]:
            print(f"  down(0) 后残留：{result['leftover_after_down']}")
        if result["schema_drift"]:
            print(f"  schema 漂移：{result['schema_drift']}")
        return 0 if ok else 1

    if args.db:
        target = args.db
    else:
        target = resolve_path(None)
    conn = connect(target)
    try:
        if args.command == "status":
            return _dump_status(conn)
        if args.command == "params":
            return _dump_params(conn)
        if args.command == "fba":
            return _dump_fba(conn, args.market)
        if args.command == "up":
            applied = migrate.upgrade(conn, args.target if args.target is not None else migrate.LATEST)
            print(f"已应用 {len(applied)} 个迁移：{applied or '无需变更'}")
            return _dump_status(conn)
        if args.command == "down":
            if not args.yes:
                print("[拒绝执行] down 会删除数据。确认请加 --yes。")
                return 2
            rolled = migrate.downgrade(conn, args.to)
            print(f"已回滚 {len(rolled)} 个迁移：{rolled or '无需变更'}")
            return _dump_status(conn)
        if args.command == "reset":
            if not args.yes:
                print("[拒绝执行] reset 会清空全部数据。确认请加 --yes。")
                return 2
            migrate.downgrade(conn, 0)
            migrate.upgrade(conn)
            print("已重置到最新版本")
            return _dump_status(conn)
    finally:
        conn.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
