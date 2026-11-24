"""数据库运维 CLI。

用法：
    python scripts/db.py status              # 当前版本与迁移清单
    python scripts/db.py up                  # 升级到最新
    python scripts/db.py up --target 1       # 只升到 0001
    python scripts/db.py down --to 0 --yes   # 全部回滚（破坏性，必须 --yes）
    python scripts/db.py roundtrip           # 在临时库验证 up→down→up，不动真库
    python scripts/db.py params              # 参数核实进度
    python scripts/db.py returns             # 逐 SKU 退货率台账（两档 + 证据强弱）
    python scripts/db.py returns-set --sku purifier_desk_1752 --name 空气净化器 \\
        --baseline 0.126 --stress 0.17 --evidence category_threshold \\
        --source-note '后台路径' --effective-date 2026-11-16
                                             # 登记一台的两档（写入 product_returns）
    python scripts/db.py fba                 # FBA 费率维表覆盖率与缺口
    python scripts/db.py fba-envelope --product-class dangerous_goods --price 59.99
                                             # 没有真实尺寸重量时，FBA 费能确定到什么范围
    python scripts/db.py fba-export          # 导出 402 格逐格复核表（只读库）
    python scripts/db.py fba-review --in data/review/fba_rates_2026-11-21.csv
                                             # 回收复核结论并对账（只读库）
    python scripts/db.py screen --price 63.99 --cogs 18.50 --duty 0.237 --fba 14.40 \\
        --freight 6.00 --returns 0.08
                                             # 候选准入：这道品在 15% 线下还剩多少空间
                                             # 口径参数不给就真的不猜：毛利降级为上界，
                                             # 结论停在「还判不了」且不给目标价（H24）
    python scripts/db.py screen --sku purifier_desk_1752 --price 79.99 \\
        --cogs 24.30 --duty 0.125 --fba 11.35 --freight 6.00
                                             # 两档退货率从 product_returns 取，自动压测
    python scripts/db.py screen --csv data/candidates/example.csv
                                             # 批量：一张候选表进，三组结论出
    python scripts/db.py reply --sku coffee_semi_1613 --price 63.99 --cogs 18.50 \\
        --duty 0.237 --freight 6.00 --fair 20.00 --box yes --fba 11.85
                                             # 供应商回复 → 下一步动作（开口 / 还价 / 签 /
                                             # 改提售价 / 先放）。退出码即结论：0 有动作、
                                             # 1 先放、3 还在等他、2 输入不成立

破坏性操作一律要求 --yes，且默认作用在配置的 DB_PATH 上；
要动真库请显式传 --db。默认值永远是最安全的那条路径。
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import tempfile
import unicodedata
from dataclasses import replace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xborder_insight.db import fba as fba_db  # noqa: E402
from xborder_insight.db import fba_review  # noqa: E402
from xborder_insight.db import migrate  # noqa: E402
from xborder_insight.db import product_returns as returns_db  # noqa: E402
from xborder_insight.db.connection import (  # noqa: E402
    MEMORY,
    connect,
    resolve_path,
    table_names,
)
from xborder_insight.db.params import coverage, list_params  # noqa: E402
from xborder_insight.domain import admission  # noqa: E402
from xborder_insight.domain import reply as reply_mod  # noqa: E402
from xborder_insight.domain import profiles  # noqa: E402
from xborder_insight.domain import transport  # noqa: E402
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
    allp = {p.param_key: p for p in list_params(conn)}
    ranged = {p.param_key: p for p in allp.values() if p.has_range}
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
        # 有值 ≠ 已核实：值只表示「查过或假设过」，状态仍是 draft。
        # 分开打印是为了让「还没人去查」与「查了但缺生效日期」在输出里可区分。
        filled = [
            (key, allp[key].value)
            for key in info["missing_keys"]
            if key in allp and allp[key].value is not None
        ]
        empty = [key for key in info["missing_keys"] if key not in dict(filled)]
        if filled:
            print("  待核实 · 已填值（可读，但 require() 仍拒绝）：")
            for key, value in filled:
                print(f"    · {key:<32} = {value:g}")
        if empty:
            print("  待核实 · 未填值：")
            for key in empty:
                print(f"    · {key}")
    print(f"\n  已声明适用区间 {len(ranged)} 项 / 共 {info['total']} 项")
    print("\n注：未核实的参数无法被业务代码使用（require() 会抛 param_unverified）。")
    print("    「已填值」只表示查过或有假设，状态仍是 draft——证据强度看 note。")
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


def _fmt_fee(env, tiers: str = "") -> str:
    """费用一格。区间收成单点时不打两个数——那不是「更精确」，是实重已经量过了。"""
    suffix = f"（{tiers}）" if tiers else ""
    if env.spread_usd < 0.005:
        return f"${env.high_usd:.2f}{suffix}"
    return f"${env.low_usd:.2f}–${env.high_usd:.2f}{suffix}"


def _dump_profile(conn, args) -> int:
    """按市场典型规格跑一遍：这些品类到底报不报得出价。

    输出的重点是**判定**，不是费用。费用只是副产品：进了标准尺寸才有数，
    进不了就是「费率未录」，而不是一个偏低的数。
    """
    try:
        profile = profiles.load_profile(args.profile)
        reports = tuple(profiles.build_category(spec) for spec in profile.categories)
        if args.category:
            reports = (profiles.find_category(reports, args.category),)
    except AppError as exc:
        print(f"[画像无法使用] {exc.code}: {exc.message}")
        if getattr(exc, "detail", None):
            print(f"  {exc.detail}")
        return 1

    ship_date = args.ship_date or date.today()
    title = "实测包装 → FBA 可报价性" if profile.measured else "市场典型规格 → FBA 可报价性"
    print(f"{title}　市场={args.market}")
    print(f"  售价 ${args.price:.2f}　发货日 {ship_date.isoformat()}")
    print(f"  数据口径：{profile.label}")
    if profile.measured:
        # 口径说明跟着数据走：全核过了就别再写「未逐台核 BOM」，否则读者会以为还有两套表
        pending_battery = [
            v.spec.model for r in reports for v in r.variants
            if not v.spec.battery_confirmed
        ]
        if pending_battery:
            print(
                "  尺寸与重量取自实测；含锂未逐台核 BOM 的机型会同时给出两种费率表的结果"
            )
        else:
            print(
                "  尺寸、重量与含锂判定均已实测/确认；含锂与否决定用普通商品表还是危险品表"
            )
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
                    exact_weight=profile.exact_weight,
                )
                tiers = "/".join(SIZE_TIER_LABELS.get(t, t) for t in klass.size_tiers)
                fee = _fmt_fee(env, tiers)
                if not variant.spec.battery_confirmed:
                    # 含锂没逐台核过：另一套费率表的结果一起给出来。
                    # 只报「按品类推的那一边」，等于把未知当成通过——
                    # 而错到便宜那一侧时，输出上一点异常都看不出来。
                    flip = PackageFacts(
                        not variant.spec.has_lithium_battery,
                        variant.spec.weight_oz,
                        variant.spec.dims_in,
                    )
                    _, env_flip = fba_db.envelope_for_package(
                        conn,
                        args.market,
                        flip,
                        selling_price=args.price,
                        ship_date=ship_date,
                        exact_weight=profile.exact_weight,
                    )
                    here = "含锂" if variant.spec.has_lithium_battery else "普货"
                    there = "普货" if variant.spec.has_lithium_battery else "含锂"
                    fee = (
                        f"{_fmt_fee(env, '')} {here} / "
                        f"{_fmt_fee(env_flip, '')} {there}（{tiers}）"
                    )
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
            if variant.spec.source_url:
                print(f"        来源 {variant.spec.source_url}")
            elif variant.spec.measured_at:
                print(
                    f"        实测 {variant.spec.measured_at}　"
                    f"{variant.spec.weight_source}"
                )
        if report.battery_divergent:
            print(
                "    含锂判定分歧：同品类既有无电池款又有含锂款 → "
                "费率表不同，不能按品类统一填"
            )
        if profile.measured:
            pending = any(not v.spec.battery_confirmed for v in report.variants)
            tail = (
                "含锂未确认的机型两套结果都已列出，取保守侧"
                if pending
                else "含锂已确认，按对应费率表报价"
            )
            print(f"    下一步：可直接按上表报价（尺寸与重量已实测）。{tail}")
        else:
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
    shown = [v for r in reports for v in r.variants]
    unconfirmed = [v.spec.model for v in shown if not v.spec.battery_confirmed]
    if profile.measured:
        if unconfirmed:
            print(
                f"\n含锂未逐台核 BOM 的机型 {len(unconfirmed)} 台："
                + "、".join(unconfirmed)
            )
            print(
                "  上面每行给了两套费率表的结果，主值取保守侧。"
                "看一眼实物/BOM 就能收敛到一个数（docs/07 H2-c）。"
            )
        print(
            "  仍需你给：真实售价（现按 "
            f"${args.price:.2f} 算）、采购价、头程报价（docs/07 H3、H15）。"
        )
        print("  数据来源与口径见文件头的 evidence 字段，以及 docs/04 §12.8。")
    else:
        print(
            "\n这些结论依赖 assumed 规格，不能当作该 SKU 的事实。"
            "\n要变成可决策的数，按 docs/07 H2 B 级量一次箱子：长×宽×高（英寸）+ 实重（oz）。"
            "\n实测值请写进 data/profiles/measured_skus.json（status=measured），不要覆盖本文件。"
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

    if args.exact_weight and args.unit_oz is None and args.unit_lb is None:
        print("[无法给出包络] input_invalid: --exact-weight 必须同时给实重（--unit-lb / --unit-oz）")
        return 1

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
                exact_weight=args.exact_weight,
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


def _dump_transport(args) -> int:
    lithium = args.lithium == "yes"
    try:
        result = transport.check(
            args.mode,
            has_lithium_battery=lithium,
            battery_installation=args.battery_installation,
        )
    except (transport.UnknownTransportMode, transport.UnknownBatteryInstallation) as exc:
        print(f"[无法判定] {exc}")
        return 2

    print("运输方式可行性")
    print("-" * 62)
    print(f"  方式：{result.mode.label}（{result.mode.code}）")
    print(f"  货物：{'含锂电池' if lithium else '不含锂电池（普货）'}")
    if result.installation is not None:
        print(
            f"  电池：{result.installation.label}"
            f" → {result.installation.packing_instruction}"
        )
    elif lithium:
        print("  电池：形态未确认（内置不可拆 / 可拆配套 / 纯电池）")
    print(f"  结论：{result.verdict_label}")
    if result.reason:
        print(f"  原因：{result.reason}")
    print(f"  依据：{result.mode.basis}")
    if result.verdict == transport.VERDICT_BLOCKED:
        print("  需要的文件：不适用——这个组合本身不成立，补文件也发不出去")
    elif result.required_docs:
        print("  需要的文件：")
        for doc in result.required_docs:
            print(f"    · {doc}")
    else:
        print("  需要的文件：无（不含电池，按普货申报）")
    if result.ok:
        print("  头程报价单还必须给（缺一项就无法落进利润模型）：")
        for field in result.quote_fields:
            print(f"    · {field}")
    for line in result.warnings:
        print(f"  ! {line}")
    print()
    print(f"  口径状态：{result.mode.status}　"
          "PI967/PI966/PI965 是货代行业转述，未逐条核实 IATA/民航局原文；")
    print("  时效与单价一律以货代报价单为准，本工具不凭空给数。")

    if result.verdict == transport.VERDICT_BLOCKED:
        return 1
    if result.verdict == transport.VERDICT_NEED_INFO:
        return 3
    return 0


def _need_product_returns(conn) -> bool:
    """库没升到 m0014 时给一句能照着做的提示，而不是甩 "no such table"。"""
    if "product_returns" in table_names(conn):
        return True
    print("[库未升级] 缺少 product_returns 表（m0014）。先跑：python scripts/db.py up")
    return False


def _dump_returns(conn, market: str | None) -> int:
    """逐 SKU 退货率台账：两档、证据强弱、离 verified 还差什么。

    这张表的价值不在于「有数」，而在于**把「这个数是读来的还是假设的」摆在同一行里**。
    净化器 12.6% 与咖啡机 8% 在算毛利时地位相同、可信度差一档；
    输出里不区分，报告就会把假设写成事实。
    """
    if not _need_product_returns(conn):
        return 2
    records = returns_db.list_all(conn, market)
    cov = returns_db.coverage(conn, market)
    print("逐 SKU 退货率（基线 → 压力档）")
    print("-" * 62)
    if not records:
        print("  表为空。登记一台：")
        print("    python scripts/db.py returns-set --sku purifier_desk_1752 \\")
        print("        --name '空气净化器' --baseline 0.126 --stress 0.17 \\")
        print("        --evidence backend_read --source-note '后台路径' \\")
        print("        --effective-date 2026-11-16")
        return 0

    for record in records:
        gap = f"+{record.rate_gap_pts:.1f} 个点"
        print(
            f"  {record.sku_key}　{record.product_name}"
            f"　{record.baseline_rate:.2%} → {record.stress_rate:.2%}（{gap}）"
        )
        asin = f"　ASIN {record.asin}" if record.asin else ""
        print(f"    证据：{record.evidence_label}{asin}　状态 {record.status}")
        if record.source_note:
            print(f"    来源：{_clip(record.source_note, 96)}")
        if record.missing_evidence:
            print(f"    还差：{'、'.join(record.missing_evidence)}")

    print()
    print(
        f"  合计 {cov['total']} 台：可对外称已核实 {cov['usable']}、"
        f"verified 状态 {cov['verified']}、借类目阈值 {cov['threshold']}、"
        f"假设 {cov['estimate']}"
    )
    if cov["pending_keys"]:
        print(f"  待补证据：{'、'.join(cov['pending_keys'])}")
    print("  口径：两档都进库，压测不再靠命令行临时传数（docs/04 §25、§28.6）")
    return 0


def _dump_returns_set(conn, args) -> int:
    """写入一台的两档。CHECK 是唯一防线，这里只把报错翻成人话。"""
    if not _need_product_returns(conn):
        return 2
    try:
        record = returns_db.upsert(
            conn,
            sku_key=args.sku,
            product_name=args.name,
            baseline_rate=args.baseline,
            stress_rate=args.stress,
            category=args.category,
            asin=args.asin,
            evidence_kind=args.evidence,
            source_note=args.source_note,
            effective_date=args.effective_date,
            verified_at=args.verified_at,
            status=args.status,
            note=args.note,
            marketplace=args.market,
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        print(f"[写入失败] {exc}")
        print("  两档必须满足：0 ≤ 基线 < 压力 < 100%；estimate 不许 verified；")
        print("  verified 需要 来源 + 读数日期 + 核实日期 三项齐全。")
        return 2
    print(
        f"已登记 {record.sku_key}（{record.marketplace}）："
        f"{record.baseline_rate:.2%} → {record.stress_rate:.2%}"
        f"　{record.evidence_label}　{record.status}"
    )
    if record.missing_evidence:
        print(f"  还差：{'、'.join(record.missing_evidence)}")
    return 0


def _resolve_returns(args, conn):
    """把「显式参数 / product_returns 表 / 未指定」收成一个口径。

    优先级：显式参数 > 表 > **未指定**（返回 None）。

    为什么没有第三档默认 8%（H24，11-23 20:15）：`duty_rate` 在 CLI 里也没有
    默认值——缺了就是 NEED_INFO，理由是「猜一个税率会让结论静默偏乐观」。
    退货率与它同类，凭什么一个有默认一个没有？留着 8% 等于在最常走的入口上
    保留同一个洞，只是在旁边写一行「这是假设」，而读的人只记结论那个百分比。

    未指定时 `evaluate()` 会把结论降级成「上界」并关掉目标价，见
    `admission._upper_bound_admission`。要给值有两条路：`--sku`（库里的两档）
    或 `--returns`（显式声明）。

    返回 (基线, 压力档, 来源说明行, 表记录)。
    """
    record = None
    if getattr(args, "sku", None):
        if conn is None:
            raise RuntimeError("--sku 需要数据库连接")
        record = returns_db.require(conn, args.sku)

    from_cli = args.returns is not None
    if from_cli:
        baseline = args.returns
    elif record is not None:
        baseline = record.baseline_rate
    else:
        baseline = None
    stress = (
        args.stress_returns
        if args.stress_returns is not None
        else (record.stress_rate if record is not None else None)
    )

    parts: list[str] = []
    if record is not None:
        parts.append(
            f"{record.sku_key}｜{record.evidence_label}"
            f"（{'库' if not from_cli else '--returns 覆盖库值'}）"
        )
    elif from_cli:
        parts.append("--returns 指定")
    else:
        parts.append("未指定（可用 --sku 取库值，或 --returns 指定）")
    if args.stress_returns is not None:
        parts.append("压力档由 --stress-returns 指定")
    elif record is not None:
        parts.append("压力档取自库")
    return baseline, stress, "；".join(parts), record


#: 话术里可以在这些字符**之后**断行。中文没有词间空格，`textwrap.wrap` 只能按
#: 字符数硬切，结果会把「（8.0」和「也算达标）」拆成两行——那段是给人复制去发的，
#: 贴进微信就是两句半。所以断口只允许落在中文标点上。
_BREAK_AFTER = "，。；：、！？）】》”’…"


def _wrap_msg(text: str, width: int = 52) -> list[str]:
    """按中文标点断行，绝不把数字和它的单位切开。

    超宽的单块不硬切（交给终端软换行）：宁可一行长一点，也不要
    「16×13×9.5 / 英寸」这种粘不回去的断口。复现见 `tests/test_reply_actions.py`
    的 `test_cli_breaks_the_script_only_at_chinese_punctuation`。
    """
    lines: list[str] = []
    cur = ""
    chunk = ""
    for ch in text:
        chunk += ch
        if ch in _BREAK_AFTER:
            if cur and len(cur) + len(chunk) <= width:
                cur += chunk
            else:
                if cur:
                    lines.append(cur)
                cur = chunk
            chunk = ""
    if chunk:
        if cur and len(cur) + len(chunk) <= width:
            cur += chunk
        else:
            if cur:
                lines.append(cur)
            cur = chunk
    if cur:
        lines.append(cur)
    return lines


def _dump_reply(args, conn=None) -> int:
    """供应商回复 → 下一步动作。

    判定全在 `domain/reply.py`，这里只负责取数、打印、给退出码：
      0 = 有动作可做（开口 / 还价 / 签 / 改提售价）
      1 = 这台先放
      3 = 还在等对方（箱规没答，或说改不了但没说原因）

    退货率必须能定：缺它毛利是上界，而上界反解出的签字线偏高（H24），
    拿它去报价必亏。所以这里宁可退回 2，也不给默认值。
    """
    record = None
    if args.sku:
        if conn is None:
            raise RuntimeError("--sku 需要数据库连接")
        record = returns_db.require(conn, args.sku)

    baseline = (
        args.returns
        if args.returns is not None
        else (record.baseline_rate if record is not None else None)
    )
    stress = (
        args.stress_returns
        if args.stress_returns is not None
        else (record.stress_rate if record is not None else None)
    )
    if baseline is None:
        print(
            "[输入不成立] 需要退货率：--sku 从 product_returns 取，或显式给 --returns。"
            "缺它签字线会偏高（上界），不能拿去报价"
        )
        return 2

    box = {"yes": True, "no": False, "unknown": None}[args.box]
    try:
        rp = reply_mod.SupplierReply(
            box_fits=box,
            box_blocker=args.box_blocker,
            counter_price=args.counter,
            firm=args.firm,
        )
    except admission.AdmissionInputInvalid as exc:
        print(f"[输入不成立] {exc}")
        return 2

    referral = (
        args.referral if args.referral is not None else admission.DEFAULT_REFERRAL
    )
    candidate = admission.Candidate(
        name=args.name,
        selling_price=args.price,
        quoted_cost=args.cogs,
        duty_rate=args.duty,
        fba_fee=args.fba,
        inbound_freight=args.freight,
        return_rate=baseline,
        referral_rate=referral,
        incoterm=args.incoterm,
    )

    # 不在入口预检「够不够算」：那是 `resolve` 的职责，入口再判一遍就会出现
    # 「只问箱规原因也要 FBA」这种自相矛盾的拒绝（`box no` 且未给原因时压根不判价）。
    # 缺什么、上界不给价格线，都由 `resolve` 抛 `AdmissionInputInvalid` 带回来。

    try:
        res = reply_mod.resolve(
            candidate, rp, fair_cogs=args.fair, stress_returns=stress
        )
    except admission.AdmissionInputInvalid as exc:
        print(f"[输入不成立] {exc}")
        return 2

    def money(value, dash="—"):
        return dash if value is None else f"${value:,.2f}"

    box_text = {True: "能做到 ≤8 英寸", False: "改不了", None: "没答"}[rp.box_fits]
    print("供应商回复 → 下一步动作")
    print("-" * 62)
    head = f"  回复：箱规 {box_text}"
    if rp.blocker_label:
        head += f"（{rp.blocker_label}受限）"
    if rp.counter_price is not None:
        head += f"　他的报价 {money(rp.counter_price)}"
    print(head)
    duty = "—" if args.duty is None else f"{args.duty:.1%}"
    print(
        f"  输入：售价 {money(args.price)}　现报价 {money(args.cogs)}　关税 {duty}"
        f"　FBA {money(args.fba)}　头程 {money(args.freight)}"
    )
    if res.branch:
        print(f"  分支：{res.branch} 档（FBA {money(res.fba_fee)}）")
    origin = "--returns 指定" if args.returns is not None else ("库" if args.sku else "未给")
    tail = f"　压测 {stress:.2%}" if stress is not None else "　未压测"
    print(f"  退货率：基线 {baseline:.2%}（{origin}）{tail}")
    # 佣金率一直有默认值，所以必须每行都印：它是唯一一个「不填也有数、
    # 但填错就少报 $3.6/件」的参数（咖啡机 B 档 $13.70 → $17.30）。
    ref_origin = (
        "--referral 指定"
        if args.referral is not None
        else f"默认值，H1 11-23 已读到 {admission.DEFAULT_REFERRAL:.0%}"
    )
    print(f"  佣金：{referral:.0%}（{ref_origin}）")
    print()
    print(f"  动作：{res.label}")

    if res.sign_price is not None:
        bits = []
        if res.sign_ceiling is not None:
            # 三位小数：$13.719 印成 $13.72 就等于把那个「四舍五入即掉线」的数报出去
            bits.append(f"15% 线 ${res.sign_ceiling:,.3f}")
        margin = "" if res.sign_margin is None else f"（毛利 {res.sign_margin:.2%}）"
        bits.append(f"签字线 {money(res.sign_price)}{margin}")
        # 只有真要开口的动作才印开口价：`先放` / `改提售价` 时印它，
        # 读者会把「我们本来准备报这个」当成「该报这个」。
        if res.open_price is not None and res.action in (
            reply_mod.ACTION_OPEN,
            reply_mod.ACTION_COUNTER,
            reply_mod.ACTION_ACCEPT,
        ):
            bits.append(f"开口 {money(res.open_price)}")
        print("  数字：" + "　".join(bits))
    if res.reply_margin is not None:
        line = f"  他报价下毛利 {res.reply_margin:.2%}"
        if res.need_drop_pct is not None and res.need_drop_pct > 0:
            line += f"　到签字线需让步 {res.need_drop_pct:.1%}"
        print(line)
    if res.stress is not None:
        print(
            f"  压测：基线 {res.stress.baseline_margin:.2%} → 压力 "
            f"{res.stress.stress_margin:.2%}（掉 {res.stress.margin_drop_pts:.2f} 点）"
            f"｜{res.stress.label}"
        )

    print()
    for item in res.reasons:
        print(f"  · {item}")

    if res.next_message:
        print()
        print("  话术（可直接复制）：")
        for line in _wrap_msg(res.next_message):
            print(f"    {line}")
    if res.pending_question and res.next_message != res.pending_question:
        print()
        print("  还欠对方一句：")
        for line in _wrap_msg(res.pending_question):
            print(f"    {line}")

    if res.action in (
        reply_mod.ACTION_REASK_BOX,
        reply_mod.ACTION_REASK_BLOCKER,
    ):
        return 3
    if res.action == reply_mod.ACTION_DROP:
        return 1
    return 0


def _dump_screen(args, conn=None) -> int:
    """候选准入。不给 `--sku` 时完全不碰数据库：缺口与目标价都是纯计算。

    两道闸刻意都打印：闸一是粗筛（只看售价带），闸二是真算。
    两者结论相反时以闸二为准，并在下面用 `!` 标出来——否则读的人会挑爱听的那个。
    """
    if conn is not None and not _need_product_returns(conn):
        return 2
    try:
        baseline, stress_rate, origin, record = _resolve_returns(args, conn)
    except AppError as exc:
        print(f"[退货率缺失] {exc.message}")
        if exc.detail:
            print(f"  {exc.detail}")
        return 2

    name = args.name
    if record is not None and name == "candidate":
        name = record.product_name

    # 口径参数一律「不替我猜」（H24）：不给就真的当没给，让它缺进
    # `Candidate`，由 `evaluate()` 降级成上界。这里不再补 6.00 / 8% 的默认值——
    # 上一版在入口层补默认值、只在旁边打印一行「这是假设」，等于把 §31 那个洞
    # 从模型层搬到了 CLI 层，而屏幕上读的人只记结论那个百分比。
    freight = args.freight
    referral = (
        args.referral if args.referral is not None else admission.DEFAULT_REFERRAL
    )
    referral_origin = (
        "--referral 指定"
        if args.referral is not None
        else f"默认 {admission.DEFAULT_REFERRAL:.0%}（H1 已读到 15%）"
    )

    candidate = admission.Candidate(
        name=name,
        selling_price=args.price,
        quoted_cost=args.cogs,
        duty_rate=args.duty,
        fba_fee=args.fba,
        fba_ratio=args.fba_ratio,
        inbound_freight=freight,
        referral_rate=referral,
        return_rate=baseline,
        incoterm=args.incoterm,
    )
    try:
        result = admission.evaluate(candidate)
    except admission.AdmissionInputInvalid as exc:
        print(f"[输入不成立] {exc}")
        return 2

    def money(value, dash="—"):
        return dash if value is None else f"${value:,.2f}"

    duty = "未给" if args.duty is None else f"{args.duty:.1%}"
    fba = money(args.fba, "未给")
    baseline_text = "未给" if baseline is None else f"{baseline:.2%}"
    print("候选准入（闸一 → 闸二）")
    print("-" * 62)
    print(f"  候选：{result.name}")
    print(
        f"  售价 {money(args.price)}　采购 {money(args.cogs)}　关税 {duty}"
        f"　FBA {fba}　头程 {money(freight, '未给')}"
    )
    print(f"  退货率：基线 {baseline_text}　来源 {origin}")

    if result.gate is not None:
        gate = result.gate
        head = f"  闸一：{gate.label}"
        if gate.limit is not None:
            head += f"　上限 ${gate.limit:,.2f}"
        print(head)
        print(f"    {gate.reason}")

    if result.verdict == admission.VERDICT_NEED_INFO:
        # 两种「还判不了」要分开说（H24）：
        #   · 硬缺（售价/采购/关税/FBA）——连毛利都算不出来；
        #   · 上界（缺头程/退货率）——毛利有值，但那是「其余按 0 计」的上界。
        # 第二种以前会被默认值填掉，屏幕上完全看不出这一台是按假设算的。
        if result.upper_bound:
            print(f"  闸二：还判不了　毛利 {result.gross_margin:.2%} 是上界")
            print("    六项成本只增不减 → 上界达标不等于达标，真实成本补上之前不能签字。")
        else:
            print("  闸二：还判不了")
        for item in result.missing:
            print(f"    · 缺 {item}")
        print("  补法：`docs/07` 里各项写着去哪儿取数；不会用默认值替你猜。")
        for note in result.notes:
            print(f"  ! {note}")
        return 3

    print(f"  闸二：{result.label}　毛利 {result.gross_margin:.2%}")
    if result.verdict == admission.VERDICT_FAIL:
        print(f"    {result.reason}")
        if result.max_cogs is None:
            print("    目标价不给：缺项按 0 反解出来的上限偏高，补上再算。")
        else:
            print(f"    15% 线：采购必须 ≤ {money(result.max_cogs)}（现 {money(args.cogs)}）")
            print(f"    15% 线：售价必须 ≥ {money(result.min_price)}（现 {money(args.price)}）")
        print(
            f"    超预算 {result.overrun_pts:.2f} 个点 —— 与毛利缺口是同一个数"
            "（`docs/04` §18.2）"
        )
    else:
        room = None
        if result.max_cogs is not None and args.cogs:
            room = result.max_cogs - args.cogs
        print(f"    毛利额 ${result.gross_profit:,.2f}/件")
        if room is not None:
            print(f"    采购价还有 {money(room)} 的空间才到 15% 线")
        print(f"    15% 线所需售价 {money(result.min_price)}")

    # 广告空间线：盈亏平衡 ACOS 数值上等于毛利率，所以这条线严格比 15% 更紧。
    # 两条都打印，是因为谈价时最容易犯的错就是拿 15% 线的上限去开口——
    # 谈成了仍然是「有毛利、投不出量」（`decision.py` 的 MIN_BREAKEVEN_ACOS）。
    if result.max_cogs_ad_space is not None and args.cogs:
        ad_cap = result.max_cogs_ad_space
        gap = ad_cap - args.cogs
        head = f"  广告空间线（ACOS ≥ {admission.AD_SPACE_FLOOR:.0%}）：采购必须 ≤ {money(ad_cap)}"
        if gap >= 0:
            print(f"{head}　已过线（还有 {money(gap)} 空间）")
        else:
            reduction = abs(gap) / args.cogs
            print(f"{head}（现 {money(args.cogs)}，还要再降 {reduction:.1%}）")
        if result.gross_margin is not None and result.gross_margin >= admission.TARGET_MARGIN:
            if result.gross_margin < admission.AD_SPACE_FLOOR:
                print(
                    "  ! 毛利过了 15% 线、没过广告空间线：这个价谈成了也只能靠自然单，"
                    "广告投不出量（`docs/04` §10.4 / §15.5 同一口径）"
                )
    stress = None
    if stress_rate is not None:
        try:
            stress = admission.stress_test(candidate, stress_rate)
        except admission.AdmissionInputInvalid as exc:
            print(f"[压测不成立] {exc}")
            return 2
        print(
            f"  压测：基线 {stress.baseline_returns:.0%} → {stress.baseline_margin:.2%}"
            f"　压力 {stress.stress_returns:.0%} → {stress.stress_margin:.2%}"
            f"（掉 {stress.margin_drop_pts:.2f} 个点）"
        )
        print(f"    判定：{stress.label} —— {stress.reason}")

    for note in result.notes:
        print(f"  ! {note}")

    print()
    # 口径逐项标来源（H24）：读者需要的是「这一项是我给的还是程序给的」，
    # 而不是一句「三项未全部核实」——那是概括，概括会被跳过。
    print(
        f"  口径：佣金 {referral:.0%}｜{referral_origin}"
        f"　退货率 {baseline_text}｜{origin}"
        f"　头程 {money(freight, '未给')}｜"
        f"{'--freight 指定' if freight is not None else '未指定（H15 待核实）'}"
    )
    print("  对外引用这个结论时，必须连这三项口径一起写。")

    if result.verdict == admission.VERDICT_PASS:
        return 0
    return 1


_BATCH_ORDER = (
    admission.VERDICT_PASS,
    admission.VERDICT_NEED_INFO,
    admission.VERDICT_FAIL,
)


def _dump_screen_batch(args) -> int:
    """批量准入：一张候选表进，三组结论出。

    排序按可行动性：有空间的先看（要接着谈）、还判不了的中间（要补数）、
    做不成的最后（放弃即可）。组内保持 CSV 原序，方便对着表改。

    一行格式错**不影响**其它行出结论，但会让退出码变成 2——因为这一批的
    统计已经不可信了，而 CLI 的退出码是唯一能被脚本读到的地方。
    """
    path = Path(args.csv)
    if not path.exists():
        print(f"[输入不成立] 找不到候选表：{path}")
        return 2
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    try:
        candidates, problems = admission.parse_rows(rows)
    except admission.AdmissionInputInvalid as exc:
        print(f"[输入不成立] {exc}")
        return 2

    # 批量模式没有逐台绑 --sku 的余地（一张表可能混好几个品类），所以两档口径
    # 只能来自命令行或默认值。**必须显式注入**：`parse_rows` 产出的 `Candidate`
    # 不带这两项（H24 之后模型层不猜），不注入的话整批都会变成「上界」。
    freight = args.freight if args.freight is not None else admission.DEFAULT_FREIGHT
    baseline = args.returns if args.returns is not None else admission.DEFAULT_RETURNS
    referral = (
        args.referral if args.referral is not None else admission.DEFAULT_REFERRAL
    )

    results: list[admission.Admission] = []
    for candidate in candidates:
        candidate = replace(
            candidate,
            inbound_freight=freight,
            return_rate=baseline,
            referral_rate=referral,
        )
        try:
            results.append(admission.evaluate(candidate))
        except admission.AdmissionInputInvalid as exc:
            problems.append(admission.RowProblem(0, f"{candidate.name}：{exc}"))
    results.sort(key=lambda item: _BATCH_ORDER.index(item.verdict))

    print(f"候选准入（批量）　{path.name}　共 {len(rows)} 行")
    print("-" * 96)
    # 每列宽 = 内容宽 + 1：中文标签按显示宽度算正好占满时，
    # 不加这一格会和下一列黏在一起（「还判不了no_duty」）。
    header = (
        _pad("结论", 9) + _pad("候选", 21) + _pad("售价", 10) + _pad("采购", 10)
        + _pad("毛利", 9) + _pad("采购上限", 12) + _pad("需降", 8) + "备注"
    )
    print(header)

    def money(value):
        return "—" if value is None else f"${value:,.2f}"

    for result in results:
        if result.verdict == admission.VERDICT_NEED_INFO:
            # 上界时毛利有值但结论是「判不了」：用 `+` 标出来，不要让读者
            # 把 31.00%+ 记成 31.00%（`docs/04` §31 的「上界不得称 PASS」）。
            margin = "—" if result.gross_margin is None else f"{result.gross_margin:.2%}+"
            cap = "—"
            drop = "—"
            note = ("上界·" if result.upper_bound else "缺：") + "、".join(
                item.split("（")[0].split(" ")[0] for item in result.missing[:2]
            )
        else:
            margin = f"{result.gross_margin:.2%}"
            cap = money(result.max_cogs)
            drop = (
                "—" if result.cogs_reduction_pct is None
                else f"{result.cogs_reduction_pct:.1%}"
            )
            if result.verdict == admission.VERDICT_PASS:
                note = "有空间"
            else:
                note = f"差 {result.overrun_pts:.1f} 点"
        seller = money(result.selling_price)
        cost = money(result.quoted_cost)
        print(
            _pad(result.label, 9) + _pad(result.name, 21) + _pad(seller, 10)
            + _pad(cost, 10) + _pad(margin, 9) + _pad(cap, 12)
            + _pad(drop, 8) + _clip(note, 30)
        )

    counts = {verdict: 0 for verdict in _BATCH_ORDER}
    for result in results:
        counts[result.verdict] += 1
    print("-" * 96)
    print(
        f"  有空间 {counts[admission.VERDICT_PASS]} / "
        f"还判不了 {counts[admission.VERDICT_NEED_INFO]} / "
        f"做不成 {counts[admission.VERDICT_FAIL]}"
        + (f"；{len(problems)} 行未计入（格式或输入问题）" if problems else "")
    )
    for problem in problems:
        where = f"第 {problem.line} 行" if problem.line else "行"
        print(f"  ! {where}：{problem.message}")
    if results:
        print()
        print("  逐台细节（含所需售价与缺什么）：去掉 --csv，按行传 --price/--cogs/--duty/--fba")
        print(
            f"  口径：佣金 {referral:.0%}｜"
            f"{'指定' if args.referral is not None else f'默认 {admission.DEFAULT_REFERRAL:.0%}（H1 已读到 15%）'}"
            f"　退货率 {baseline:.1%}｜"
            f"{'指定' if args.returns is not None else f'默认 {admission.DEFAULT_RETURNS:.0%}（H3 未核实）'}"
            f"　头程 ${freight:.2f}｜"
            f"{'指定' if args.freight is not None else f'默认 ${admission.DEFAULT_FREIGHT:.2f}（H15 未核实）'}"
        )
        print(
            "  批量是**粗筛**口径：整批按下面这几项算（逐行给头程不现实），"
            "进入谈判前用单台模式补全 --freight / --returns。"
        )

    if problems:
        return 2
    if counts[admission.VERDICT_NEED_INFO]:
        return 3
    if counts[admission.VERDICT_FAIL]:
        return 1
    return 0


def _export_path(args) -> Path:
    if args.out:
        return Path(args.out)
    return ROOT / "data" / "review" / f"fba_rates_{date.today().isoformat()}.csv"


def _dump_export(conn, args) -> int:
    """导出 402 格为复核表。只读库，不写库。"""
    cells = fba_review.load_cells(conn, args.market)
    if not cells:
        print(f"[无可导出] 市场 {args.market} 的费率表为空")
        return 1
    path = _export_path(args)
    fba_review.write_sheet(path, cells)

    print("FBA 费率逐格复核表")
    print("-" * 62)
    print(f"  市场 {args.market} · {len(cells)} 格 → {path}")
    by_version: dict[str, int] = {}
    for cell in cells:
        by_version[cell.version_key] = by_version.get(cell.version_key, 0) + 1
    for key, count in by_version.items():
        print(f"    · {key:<22}{count:>4} 格")
    tiers: dict[str, int] = {}
    for cell in cells:
        tiers[cell.size_tier] = tiers.get(cell.size_tier, 0) + 1
    print("  按尺寸段：" + "　".join(f"{k} {v}" for k, v in sorted(tiers.items())))
    print()
    print("  怎么填：")
    print(f"    · 在 `{fba_review.COL_VERDICT}` 列写 ok 或 mismatch（也可写 对 / 错）；留空 = 未复核")
    print(f"    · mismatch 必须在 `{fba_review.COL_NOTE}` 写清错在哪，否则导入会被拒")
    print(f"    · `{fba_review.COL_REVIEWER}` 与 `{fba_review.COL_DATE}` 必填，日期 YYYY-MM-DD")
    print("    · 金额列是待核对的快照，不要改——改了等于改数据，不是复核")
    print("  填完跑：")
    print(f"    python scripts/db.py fba-review --in {path}")
    return 0


def _dump_review(conn, args) -> int:
    """回收复核表并对账。退出码：0 全一致 / 1 有错或格式问题 / 3 还没核完。"""
    path = Path(args.sheet)
    if not path.exists():
        print(f"[找不到] {path}")
        return 2
    cells = fba_review.load_cells(conn, args.market)
    try:
        rows = fba_review.read_sheet(path)
    except ValueError as exc:
        print(f"[表格不可用] {exc}")
        return 2
    summary = fba_review.summarize(cells, rows)

    print("FBA 费率逐格复核　对账结果")
    print("-" * 62)
    print(f"  库内 {summary.total_cells} 格 · 表内已给结论 {summary.reviewed} 格"
          f"（{summary.coverage:.1%}）")
    print(f"  ok {summary.ok} · mismatch {summary.mismatch} · 未复核 {summary.pending}")
    if summary.unknown_ids:
        print(f"  库里不存在的行号 {len(summary.unknown_ids)} 个")

    pairs = fba_review.mismatched_cells(cells, summary)
    if pairs:
        print("\n  mismatch（按此修数据，改完重新导出再核）：")
        for cell, row in pairs:
            weight = (
                f"{_fmt_oz(cell.weight_min_oz)}–{_fmt_oz(cell.weight_max_oz)} oz"
            )
            print(
                f"    · {cell.version_key} / {cell.size_tier} / {cell.product_class}"
                f" / {cell.price_band} / {weight}"
            )
            print(
                f"      库里 ${cell.base_fee_usd:.2f}"
                f"（含首重 {_fmt_oz(cell.included_oz)} oz、续重 ${cell.per_increment_usd:.2f}）"
                f"　{row.reviewer} {row.reviewed_at}：{row.note}"
            )

    if summary.problems:
        print(f"\n  格式问题 {len(summary.problems)} 条（未计入统计）：")
        for line in summary.problems[:20]:
            print(f"    ! {line}")
        if len(summary.problems) > 20:
            print(f"    … 另有 {len(summary.problems) - 20} 条")

    if summary.pending:
        print(f"\n  还没核的 {summary.pending} 格，前 10 个行号："
              f"{'、'.join(summary.pending_ids[:10])}")

    print()
    if summary.exit_code == 0:
        print("  结论：全部格已复核且与官方页一致——可以对外写「逐格复核通过」。")
    elif summary.exit_code == 1:
        print("  结论：还不能说复核通过（有 mismatch 或格式问题）。")
    else:
        print("  结论：复核未完成。未复核的格不得在对外表述里算作「已核对」。")
    return summary.exit_code


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

    ret_parser = sub.add_parser(
        "returns", help="逐 SKU 退货率台账：两档 + 证据强弱 + 还差什么"
    )
    ret_parser.add_argument("--market", default=None,
                            help="只看一个市场，默认全部")

    ret_set = sub.add_parser(
        "returns-set", help="登记一台的两档退货率（写入 product_returns）"
    )
    ret_set.add_argument("--sku", required=True, help="内部稳定键，如 purifier_desk_1752")
    ret_set.add_argument("--name", required=True, help="品名（输出用）")
    ret_set.add_argument("--baseline", type=float, required=True, help="基线退货率，如 0.126")
    ret_set.add_argument("--stress", type=float, required=True,
                         help="压力档退货率，必须严格高于基线，如 0.17")
    ret_set.add_argument("--market", default=returns_db.DEFAULT_MARKET)
    ret_set.add_argument("--category", default=None, help="taxonomy category key")
    ret_set.add_argument("--asin", default=None, help="10 位 ASIN（可空）")
    ret_set.add_argument("--evidence", choices=returns_db.EVIDENCE_KINDS,
                         default="estimate",
                         help="backend_read=本 SKU 后台读数 / official_page=官方页面 "
                              "/ category_threshold=借类目阈值 / estimate=假设（默认）")
    ret_set.add_argument("--source-note", default=None,
                         help="后台路径 + 读到的是什么。verified 必填")
    ret_set.add_argument("--effective-date", default=None, help="读数/生效日期 YYYY-MM-DD")
    ret_set.add_argument("--verified-at", default=None, help="核实日期 YYYY-MM-DD")
    ret_set.add_argument("--status", choices=("draft", "verified"), default="draft")
    ret_set.add_argument("--note", default=None)

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
    env_parser.add_argument("--exact-weight", action="store_true",
                            help="实重是实测值而非上界估计：区间收成单点（须与 --dims 一起给）")

    tr_parser = sub.add_parser(
        "transport",
        help="运输方式可行性：含锂货能不能走这个渠道、要哪些文件",
    )
    tr_parser.add_argument("--mode", choices=tuple(transport.TRANSPORT_MODES),
                           default="air_battery", help="默认带电渠道空运")
    tr_parser.add_argument("--lithium", choices=("yes", "no"), required=True,
                           help="该 SKU 含不含锂电池（看 BOM，不看默认使用方式）")
    tr_parser.add_argument(
        "--battery-installation",
        choices=tuple(transport.BATTERY_INSTALLATIONS),
        default=None,
        help="电池形态：built_in=内置不可拆(PI967) / removable=配套可拆(PI966) / "
             "standalone=纯电池(PI965)。含锂但不给这一项 → 结论为「需先确认电池形态」",
    )

    # help 里的百分号必须写成 `%%`：argparse 会用 % 格式化这段文本，
    # 裸写 `15%` 会让 `% 毛` 被当成格式符，整个 `--help` 直接抛 ValueError。
    scr_parser = sub.add_parser(
        "screen",
        help="候选准入：这个品在 15%% 毛利线下还剩多少空间（闸一 + 闸二）",
    )
    scr_parser.add_argument("--name", default="candidate", help="候选名（输出用）")
    scr_parser.add_argument("--price", type=float, default=None, help="售价（美元）")
    scr_parser.add_argument("--csv", default=None,
                            help="批量模式：候选表路径（列：name,price,cogs,duty,fba 或 fba_ratio）")
    scr_parser.add_argument("--cogs", type=float, default=None,
                            help="采购价（按 --incoterm 口径；默认 FOB）")
    scr_parser.add_argument("--duty", type=float, default=None,
                            help="关税率（0–1）。不给则判 need_info——不替你猜归类")
    scr_parser.add_argument("--fba", type=float, default=None, help="FBA 费（$/件）")
    scr_parser.add_argument("--fba-ratio", type=float, default=None,
                            help="FBA 占售价的典型比例（如 0.15）。与 --fba 二选一")
    scr_parser.add_argument("--freight", type=float, default=None,
                            help="头程（$/件）。不给则不猜：毛利按 0 头程记为上界，"
                                 "结论降级为「还判不了」且不给目标价（H24）")
    scr_parser.add_argument("--referral", type=float, default=None,
                            help="佣金比例。不给则用 0.15（H1 已读到 15%%，Home & Kitchen）")
    scr_parser.add_argument("--returns", type=float, default=None,
                            help="基线退货率。不给则：有 --sku 取库里的值；"
                                 "都没有就是「未指定」，毛利降级为上界")
    scr_parser.add_argument("--stress-returns", type=float, default=None,
                            help="压测档退货率（如 0.17）。不给则：有 --sku 取库里的值，"
                                 "没有则不压测。给了就一起算两档，"
                                 "按「两档都为正才值得谈」出判定（docs/04 §25）")
    scr_parser.add_argument("--sku", default=None,
                            help="从 product_returns 表取两档退货率（如 purifier_desk_1752）。"
                                 "显式传 --returns/--stress-returns 会覆盖库值并在输出里标出")
    scr_parser.add_argument("--incoterm", default="FOB",
                            choices=("FOB", "DDP"), help="默认 FOB")

    rep_parser = sub.add_parser(
        "reply",
        help="供应商回复 → 下一步动作（问箱规 / 发开口 / 还价 / 签 / 改提售价 / 先放）",
    )
    rep_parser.add_argument("--name", default="candidate", help="候选名（输出用）")
    rep_parser.add_argument("--price", type=float, required=True, help="我方售价（美元）")
    rep_parser.add_argument("--cogs", type=float, required=True,
                            help="他当前的报价（$/件），这一轮的起点")
    rep_parser.add_argument("--duty", type=float, required=True, help="关税率（0–1）")
    rep_parser.add_argument("--box", default="unknown",
                            choices=("yes", "no", "unknown"),
                            help="箱规答复：yes=最短边能做到 8 英寸内；no=改不了；"
                                 "unknown=还没回（默认，会先问不报价）")
    rep_parser.add_argument("--box-blocker", default=None,
                            choices=("paperboard", "tooling", "cushion"),
                            help="只在 --box no 时给：paperboard=纸板规格 / tooling=模具或内衬 / "
                                 "cushion=缓冲结构（ISTA 跌落底线与机身净边之间没余量）。"
                                 "不给则动作为「先问清受限在哪」")
    rep_parser.add_argument("--counter", type=float, default=None,
                            help="他的反报价（$/件）。给了就判签/还价/换杠杆")
    rep_parser.add_argument("--firm", action="store_true",
                            help="他说「就这个价，不再让」。降幅不大但咬死不放时，"
                                 "结论会从「还价」变成「改提售价」")
    rep_parser.add_argument("--fba", type=float, default=None,
                            help="箱规对应档位的 FBA（$/件）。--box unknown 时不需要")
    rep_parser.add_argument("--freight", type=float, default=None,
                            help="头程（$/件）。DDP 报价可不给；FOB 不给会拒绝判价")
    rep_parser.add_argument("--fair", type=float, default=None,
                            help="FOB 公允价（内部锚，只用于压开口价，绝不进话术）")
    rep_parser.add_argument("--referral", type=float, default=None,
                            help="佣金比例。不给则用 0.15（H1 11-23 13:42 已读到 15%%，Home & Kitchen）。"
                                 "后台若读到 8%% 类目，这里必须显式给，否则签字线"
                                 "会比实际低 $3.6/件（咖啡机 B 档 $13.719 → $17.325）")
    rep_parser.add_argument("--sku", default=None,
                            help="从 product_returns 取两档退货率（如 coffee_semi_1613）")
    rep_parser.add_argument("--returns", type=float, default=None,
                            help="基线退货率，覆盖库值")
    rep_parser.add_argument("--stress-returns", type=float, default=None,
                            help="压测档退货率，覆盖库值；不给则不压测")
    rep_parser.add_argument("--incoterm", default="FOB", choices=("FOB", "DDP"))

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

    exp_parser = sub.add_parser(
        "fba-export", help="导出 FBA 费率逐格复核表（CSV，只读库）"
    )
    exp_parser.add_argument("--market", default="US")
    exp_parser.add_argument("--out", default=None,
                            help="默认 data/review/fba_rates_<今天>.csv")

    rev_parser = sub.add_parser(
        "fba-review", help="回收逐格复核表并对账（只读库）"
    )
    rev_parser.add_argument("--in", dest="sheet", required=True,
                            help="填好的复核表路径")
    rev_parser.add_argument("--market", default="US")

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

    if args.command == "transport":
        # 不碰数据库：这是纯规则判定，没有需要核实的数值参与。
        return _dump_transport(args)

    if args.command == "reply":
        if args.sku:
            conn = connect(args.db or resolve_path(None))
            try:
                return _dump_reply(args, conn)
            finally:
                conn.close()
        return _dump_reply(args)

    if args.command == "screen":
        # 同样不碰库：准入只用到传入的四个数与几个口径常数。
        if args.csv and args.price is not None:
            print("[输入不成立] --csv 与 --price 不能同时给：批量与单台是两条路径")
            return 2
        if args.csv:
            return _dump_screen_batch(args)
        if args.price is None:
            print("[输入不成立] 单台模式需要 --price；批量请用 --csv <文件>")
            return 2
        if args.sku:
            # 只有 --sku 这条路径要读库；其余仍是不碰库的纯计算。
            conn = connect(args.db or resolve_path(None))
            try:
                return _dump_screen(args, conn)
            finally:
                conn.close()
        return _dump_screen(args)

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
        if args.command == "returns":
            return _dump_returns(conn, args.market)
        if args.command == "returns-set":
            return _dump_returns_set(conn, args)
        if args.command == "fba-envelope":
            return _dump_envelope(conn, args)
        if args.command == "fba-profile":
            return _dump_profile(conn, args)
        if args.command == "fba-export":
            return _dump_export(conn, args)
        if args.command == "fba-review":
            return _dump_review(conn, args)
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
