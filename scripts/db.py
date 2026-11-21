"""数据库运维 CLI。

用法：
    python scripts/db.py status              # 当前版本与迁移清单
    python scripts/db.py up                  # 升级到最新
    python scripts/db.py up --target 1       # 只升到 0001
    python scripts/db.py down --to 0 --yes   # 全部回滚（破坏性，必须 --yes）
    python scripts/db.py roundtrip           # 在临时库验证 up→down→up，不动真库
    python scripts/db.py params              # 参数核实进度
    python scripts/db.py fba                 # FBA 费率维表覆盖率与缺口
    python scripts/db.py fba-envelope --product-class dangerous_goods --price 59.99
                                             # 没有真实尺寸重量时，FBA 费能确定到什么范围

破坏性操作一律要求 --yes，且默认作用在配置的 DB_PATH 上；
要动真库请显式传 --db。默认值永远是最安全的那条路径。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unicodedata
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xborder_insight.db import fba as fba_db  # noqa: E402
from xborder_insight.db import migrate  # noqa: E402
from xborder_insight.db.connection import MEMORY, connect, resolve_path  # noqa: E402
from xborder_insight.db.params import coverage, list_params  # noqa: E402
from xborder_insight.domain import profiles  # noqa: E402
from xborder_insight.domain import package as pkg  # noqa: E402
from xborder_insight.domain.package import PackageFacts  # noqa: E402
from xborder_insight.domain.fba import (  # noqa: E402
    PRICE_BAND_LABELS,
    PRODUCT_CLASSES,
    PRODUCT_CLASS_LABELS,
    SIZE_TIERS,
    SIZE_TIER_LABELS,
)
from xborder_insight.errors import AppError  # noqa: E402


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
    versions = fba_db.list_rate_versions(conn, marketplace)
    audit = fba_db.audit(conn, marketplace)
    print(f"FBA 配送费维表  市场={marketplace}")
    print("-" * 58)
    print("  费率版本：")
    for version in versions:
        flag = "已核实" if version.verified else f"待核实({version.status})"
        print(
            f"    · {version.version_key:<20} {version.period_label}  "
            f"{version.season:<9} {flag}"
        )
    if audit.version_overlaps:
        pairs = "、".join(f"{a}×{b}" for a, b in audit.version_overlaps)
        print(f"    ⚠ 版本区间重叠 {len(audit.version_overlaps)} 处：{pairs}")
    elif versions:
        print("    版本区间无重叠")

    rate, why = fba_db.fuel_surcharge(conn, marketplace)
    print(
        "  燃油与物流附加费："
        + (f"已核实 {rate:.2%}（按配送费计提）" if rate is not None else f"未施加 —— {why}")
    )
    print()
    print(
        f"  共 {audit.total_rows} 行 · 已核实 {audit.verified_rows} · "
        f"待核实 {audit.draft_rows}"
    )
    print(
        f"  已声明角组合 {audit.banded_dimensions} 个 · "
        f"重量缺口 {audit.gap_count} 处 · 区间重叠 {audit.overlap_count} 处"
    )

    for version in versions:
        groups = [g for g in audit.groups if g.version_key == version.version_key]
        if not groups:
            print(f"\n  [{version.version_key}] 一行未录")
            continue
        print(f"\n  [{version.version_key}] {version.period_label}")
        for group in groups:
            gap_text = (
                "无缺口"
                if not group.gaps
                else "缺口 " + "、".join(
                    f"{_fmt_oz(lo)}–{_fmt_oz(hi)}" for lo, hi in group.gaps
                )
            )
            overlap_text = "" if not group.overlaps else f"  ⚠ 重叠 {len(group.overlaps)} 处"
            draft_text = "" if not group.draft else f" · draft {group.draft}"
            print(
                f"    · {group.size_tier:<15}{group.product_class:<16}{group.price_band:<10}"
                f" {len(group.bands):>2} 档  {gap_text}{draft_text}{overlap_text}"
            )

    print("\n注：缺口是结论，不是缺陷——")
    print("    · 服装（apparel）、超大件（extra_large）、2027 期的大件（官方已重构为十档）整段未录；")
    print("    · 2027 旺季（10/15/2027 起）官方未发布，该日期之后的报价一律 503；")
    print("    · small_standard 在 16 oz 以上的「缺口」是尺寸段定义本身（超过 16 oz 即")
    print("      不属于小号标准件），不是费率缺失。")
    print("    resolve() 对缺版本 / 缺档位 / 未核实分别抛 param_unverified（503），")
    print("    不会外推到相邻期、相邻尺寸段或相邻产品类别。")
    return 0


def _parse_iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD，收到 {text!r}") from exc


def _parse_dims(text: str) -> tuple[float, float, float]:
    """解析 "18x14x8" / "18*14*8" / "18,14,8" 三种写法。

    允许三种分隔符是因为量完箱子随手敲的时候，没人记得文档里写的是哪个。
    但只接受三个值——两边或四边大概率是少写了一边，不能当成「差不多」。
    """
    raw = text.replace("*", "x").replace(",", "x").replace("×", "x")
    parts = [p.strip() for p in raw.split("x") if p.strip()]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"尺寸要给三个值（例：18x14x8），收到 {text!r}"
        )
    try:
        values = tuple(float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"尺寸必须是数值，收到 {text!r}") from exc
    if any(v <= 0 for v in values):
        raise argparse.ArgumentTypeError(f"尺寸必须为正数，收到 {text!r}")
    return values  # type: ignore[return-value]


def _fmt_yes_no(value: bool) -> str:
    return "是" if value else "否"


def _disp_width(text: str) -> int:
    """按终端显示宽度数：CJK 全角字符占两格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _clip(text: str, width: int) -> str:
    """按显示宽度截断，超出补省略号。列宽兜底，避免长机型名顶开整张表。"""
    if _disp_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + step > width - 1:
            break
        out.append(ch)
        used += step
    return "".join(out) + "…"


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐。用 len() 补空格会把含中文的表撑歪。"""
    clipped = _clip(text, width)
    return clipped + " " * max(0, width - _disp_width(clipped))


def _dump_profile(conn, args) -> int:
    """按市场典型规格跑一遍：这些品类到底报不报得出价。

    输出的重点是**判定**，不是费用。费用只是副产品：进了标准尺寸才有数，
    进不了就是「费率未录」，而不是一个偏低的数。
    """
    try:
        reports = profiles.load_reports(args.profile)
        if args.category:
            reports = (profiles.find_category(reports, args.category),)
    except AppError as exc:
        print(f"[画像无法使用] {exc.code}: {exc.message}")
        if getattr(exc, "detail", None):
            print(f"  {exc.detail}")
        return 1

    ship_date = args.ship_date or date.today()
    print(f"市场典型规格 → FBA 可报价性　市场={args.market}")
    print(f"  售价 ${args.price:.2f}　发货日 {ship_date.isoformat()}")
    print("  规格来源：在售机型的公开包装尺寸与毛重，状态 assumed（非实测）")
    print("=" * 104)
    print(
        _pad("品类", 14) + _pad("机型", 34) + _pad("含锂", 6)
        + _pad("包装(英寸)", 24) + _pad("实重oz", 9) + _pad("计费oz", 9) + "FBA 费"
    )
    print("-" * 104)

    unreachable = 0
    for report in reports:
        for variant in report.variants:
            dims = "×".join(f"{v:g}" for v in variant.spec.dims_in)
            # 尺寸段由包装事实推出，不由「fits 标准尺寸」这个二元判断决定：
            # 落在大件也是可报价的（2026 两期），只有超大件才是真缺。
            try:
                klass = pkg.classify(variant.as_facts())
                _, env = fba_db.envelope_for_package(
                    conn,
                    args.market,
                    variant.as_facts(),
                    selling_price=args.price,
                    ship_date=ship_date,
                )
                tiers = "/".join(SIZE_TIER_LABELS.get(t, t) for t in klass.size_tiers)
                fee = f"${env.low_usd:.2f}–${env.high_usd:.2f}（{tiers}）"
                billable = max(variant.spec.weight_oz, klass.dim_weight_oz or 0.0)
            except AppError as exc:
                unreachable += 1
                billable = variant.billable_oz
                fee = f"[{exc.code}] {exc.message[:28]}"
            print(
                _pad(report.spec.label, 14)
                + _pad(variant.spec.model, 34)
                + _pad(_fmt_yes_no(variant.spec.has_lithium_battery), 6)
                + _pad(dims, 24)
                + _pad(f"{variant.spec.weight_oz:.1f}", 9)
                + _pad(f"{billable:.1f}", 9)
                + fee
            )
        print()

    print("=" * 96)
    print("品类判定")
    print("-" * 96)
    for report in reports:
        print(f"  {report.spec.display_name}")
        print(f"    判定：{report.verdict_label}")
        for variant in report.variants:
            print(f"      · {variant.spec.model}：{variant.worst_edge.describe()}")
            print(f"        来源 {variant.spec.source_url}")
        if report.battery_divergent:
            print(
                "    含锂判定分歧：同品类既有无电池款又有含锂款 → "
                "费率表不同，不能按品类统一填"
            )
        print(f"    下一步：{report.headline()}")
        print()

    if unreachable:
        print(
            f"注意：{unreachable}/{sum(len(r.variants) for r in reports)} 台机型"
            "报不出价（超出大件范围，或该期大件未录）。"
        )
        print(
            "  大件两段（small_bulky / large_bulky）2026 两期的费率已录；"
            "仍报缺的是超大件与 2027 期（docs/07 H14），这部分不是「费用高一点」，"
            "而是根本给不出报价。"
        )
    print(
        "\n这些结论依赖 assumed 规格，不能当作该 SKU 的事实。"
        "\n要变成可决策的数，按 docs/07 H2 B 级量一次箱子：长×宽×高（英寸）+ 实重（oz）。"
    )
    return 0


def _dump_envelope(conn, args) -> int:
    """不知道精确尺寸重量时，这个 SKU 的 FBA 费最多和最多少。

    两条入口：
      · 包装事实（--lithium + 实重/尺寸）—— 推荐。产品类别、尺寸段、计费重量
        都从事实推出来，调用方不必自己判断，也就没有第二处可能出错。
      · 裸查询（--product-class [+ --size-tier/--max-*]）—— 已知类别与段位时的
        低层裁剪，不考虑体积重。两条入口不要在同一个命令里混用。
    """
    if args.lithium is not None:
        mixed = [
            flag
            for flag, value in (
                ("--product-class", args.product_class),
                ("--size-tier", args.size_tier),
                ("--max-oz/--max-lb", args.max_oz if args.max_oz is not None else args.max_lb),
            )
            if value
        ]
        if mixed:
            print(
                "[拒绝执行] --lithium 与 " + "、".join(mixed) + " 不能同时给："
                "产品类别与尺寸段由包装事实推出，再手填一次会出现两个不一致的答案"
            )
            return 2
    elif args.product_class is None:
        print(
            "[缺少输入] 给 --lithium yes|no（推荐，自动推类别与尺寸段），"
            "或直接给 --product-class"
        )
        return 2

    if args.max_oz is not None and args.max_lb is not None:
        print("[拒绝执行] --max-oz 与 --max-lb 只能给一个：单位不同，同时给必然算错")
        return 2
    if args.unit_oz is not None and args.unit_lb is not None:
        print("[拒绝执行] --unit-oz 与 --unit-lb 只能给一个：单位不同，同时给必然算错")
        return 2

    max_oz = None
    if args.max_oz is not None:
        max_oz = float(args.max_oz)
    elif args.max_lb is not None:
        max_oz = float(args.max_lb) * 16.0
    weight_range = None if max_oz is None else (0.1, max_oz)

    unit_oz = None
    if args.unit_oz is not None:
        unit_oz = float(args.unit_oz)
    elif args.unit_lb is not None:
        unit_oz = float(args.unit_lb) * 16.0

    ship_date = args.ship_date or date.today()

    try:
        if args.lithium is not None:
            facts = PackageFacts(
                has_lithium_battery=args.lithium == "yes",
                unit_weight_oz=unit_oz,
                dims_in=args.dims,
                fits_standard_size=args.fits_standard,
                volume_ceiling_in3=args.max_volume_in3,
            )
            package, env = fba_db.envelope_for_package(
                conn,
                args.market,
                facts,
                selling_price=args.price,
                ship_date=ship_date,
            )
        else:
            package = None
            env = fba_db.envelope(
                conn,
                args.market,
                product_class=args.product_class,
                selling_price=args.price,
                ship_date=ship_date,
                size_tiers=args.size_tier,
                weight_range_oz=weight_range,
            )
    except AppError as exc:
        print(f"[无法给出包络] {exc.code}: {exc.message}")
        if getattr(exc, "detail", None):
            print(f"  {exc.detail}")
        return 1

    print(f"FBA 费包络　市场={args.market}")
    print("-" * 62)
    print(
        f"  售价 ${args.price:.2f} → {PRICE_BAND_LABELS.get(env.price_band, env.price_band)}"
        f"　发货日 {ship_date.isoformat()} → 版本 {env.version_key}"
        f"（{env.rate_period}）"
    )
    print(f"  产品类别：{PRODUCT_CLASS_LABELS[env.product_class]}")
    if package is not None:
        print(
            "  包装事实：含锂电池 "
            + _fmt_yes_no(facts.has_lithium_battery)
            + (f"　实重 {unit_oz:g} oz（{unit_oz / 16:g} lb）" if unit_oz is not None else "　实重 未给")
            + (f"　体积重 {package.dim_weight_oz:.1f} oz（{package.dim_weight_oz / 16:.2f} lb）"
               if package.dim_weight_oz is not None else "")
        )
        print(f"  尺寸段：" + "、".join(SIZE_TIER_LABELS.get(t, t) for t in package.size_tiers))
    else:
        print(
            "  尺寸段："
            + (
                "、".join(SIZE_TIER_LABELS.get(t, t) for t in args.size_tier)
                if args.size_tier
                else "未限定"
            )
        )
    print()
    print(f"  FBA 费 ${env.low_usd:.2f} – ${env.high_usd:.2f}　（跨度 ${env.spread_usd:.2f}）")
    print(f"    最低档 {env.low_row.dimension_label()}")
    print(f"    最高档 {env.high_row.dimension_label()}")
    if env.billable_weight_oz is not None:
        # 最高档那一行的区间可能一直到 20 lb，但本单只按计费重量上界取值，
        # 不写出来的话，读的人会以为费用上界对应 20 lb。
        print(
            f"    费用上界按计费重量 {env.billable_weight_oz:.1f} oz"
            f"（{env.billable_weight_oz / 16:.2f} lb）取值"
            + (
                f"；其中体积重 {env.dim_weight_oz:.1f} oz"
                if env.dim_weight_oz is not None
                else ""
            )
        )
    print(f"  扫过 {env.row_count} 个已核实行，燃油附加费 "
          + (f"{env.fuel_surcharge_rate:.2%}" if env.fuel_surcharge_rate else "未施加"))
    print()
    print("  假设：")
    for line in env.assumptions:
        print(f"    · {line}")
    if env.warnings:
        print()
        print("  提示：")
        for line in env.warnings:
            print(f"    ! {line}")
    print()
    print("  区间两端都是官方费率，不是估计值：整体在阈值一侧时可直接定论，")
    print("  只有跨过阈值才需要量包装。你给的条件越具体，区间越窄。")
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

    env_parser = sub.add_parser(
        "fba-envelope", help="没有真实尺寸重量时，FBA 费能确定到什么范围"
    )
    env_parser.add_argument("--market", default="US")
    env_parser.add_argument("--price", type=float, required=True, help="售价（美元）")
    env_parser.add_argument("--ship-date", type=_parse_iso_date, default=None,
                            help="YYYY-MM-DD，默认今天")
    env_parser.add_argument("--lithium", choices=("yes", "no"), default=None,
                            help="是否含锂电池：yes 走危险品表，no 走普通商品表")
    env_parser.add_argument("--unit-oz", type=float, default=None,
                            help="包装后实重（oz），也可以是上界")
    env_parser.add_argument("--unit-lb", type=float, default=None,
                            help="包装后实重（lb），也可以是上界")
    env_parser.add_argument("--dims", type=_parse_dims, default=None,
                            help="包装后三边英寸，如 18x14x8")
    env_parser.add_argument("--max-volume-in3", type=float, default=None,
                            help="包装体积上界（立方英寸）：不知三边但知道「不超过一个鞋盒」时用")
    env_parser.add_argument("--fits-standard", dest="fits_standard",
                            action="store_true", default=None,
                            help="只知三边都装得进 18x14x8 时可给这个，代替 --dims")
    env_parser.add_argument("--no-fits-standard", dest="fits_standard",
                            action="store_false",
                            help="已知三边超出 18x14x8（会报不属于标准尺寸）")
    env_parser.add_argument("--product-class", choices=PRODUCT_CLASSES, default=None,
                            help="裸查询入口：直接指定类别，不考虑体积重")
    env_parser.add_argument("--size-tier", action="append", choices=SIZE_TIERS,
                            default=None, help="可多次给；不传则不限定")
    env_parser.add_argument("--max-oz", type=float, default=None,
                            help="裸查询入口：重量上界（oz），不区分尺寸段")
    env_parser.add_argument("--max-lb", type=float, default=None,
                            help="裸查询入口：重量上界（lb），不区分尺寸段")

    prof_parser = sub.add_parser(
        "fba-profile",
        help="按市场典型规格跑一遍：这些品类到底报不报得出价",
    )
    prof_parser.add_argument("--profile", default=None,
                             help="画像文件，默认 data/profiles/market_typical.json")
    prof_parser.add_argument("--market", default="US")
    prof_parser.add_argument("--price", type=float, default=59.99,
                             help="售价（美元），默认 59.99 落在 $50-100 价格带")
    prof_parser.add_argument("--ship-date", type=_parse_iso_date, default=None,
                             help="YYYY-MM-DD，默认今天")
    prof_parser.add_argument("--category", default=None,
                             help="只看一个品类，如 robot_vacuum")

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
        if args.command == "fba-envelope":
            return _dump_envelope(conn, args)
        if args.command == "fba-profile":
            return _dump_profile(conn, args)
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
