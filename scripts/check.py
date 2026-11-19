"""质量门禁：用一条命令回答「这次改动会不会变差」。

这是本项目唯一能证伪 LLM 改动的手段。LLM 应用的改动（换模型、调 prompt、
改切片）没有编译错误，只能靠「同一份标注集上指标是否退化」来判断。

三层执行：
  1. 单元/契约测试   —— 数学与契约是否还成立
  2. 迁移可逆性      —— up → down → up 后 schema 是否原样（临时库，不碰真库）
  3. 评测复跑        —— P/R/F1、成本、延迟
  4. 与基线快照 diff —— 退化是否超出阈值

用法：
    python scripts/check.py                 # 全量门禁
    python scripts/check.py --tests-only    # 只跑测试
    python scripts/check.py --db-only       # 只跑迁移校验
    python scripts/check.py --eval-only     # 只跑评测 + 基线对比
    python scripts/check.py --update-baseline
    python scripts/check.py --show          # 只打印当前指标 vs 基线

退出码：0 全部通过；1 有硬门禁未通过；2 环境/配置错误。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
BASELINE_PATH = ROOT / "tests" / "baseline.json"
GATES_PATH = ROOT / "tests" / "gates.json"

BASELINE_SCHEMA = 1

# 进入基线的指标。刻意不含 elapsed_ms / 时间戳这类会自然漂移的字段，
# 否则每次 diff 都是噪声，人就会开始忽略告警。
SNAPSHOT_FIELDS = (
    "run_mode",
    "model",
    "embedding",
    "reviews",
    "precision",
    "recall",
    "f1",
    "tp",
    "fp",
    "fn",
    "parse_success_rate",
    "llm_calls",
    "calls_per_review",
    "prompt_tokens",
    "completion_tokens",
    "tokens_per_review",
    "latency_p50_ms",
    "latency_p95_ms",
)


def _echo(text: str = "") -> None:
    print(text, flush=True)


def _rule(char: str = "-", width: int = 74) -> None:
    _echo(char * width)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_gates() -> dict:
    if not GATES_PATH.exists():
        _echo(f"[FATAL] 缺少门禁配置: {GATES_PATH}")
        raise SystemExit(2)
    return load_json(GATES_PATH)["hard"]


def load_eval_module():
    """按路径加载 scripts/run_eval.py，复用同一份评测逻辑，不复制粘贴。"""
    for path in (str(SRC), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location("run_eval", SCRIPTS / "run_eval.py")
    if spec is None or spec.loader is None:
        raise SystemExit("[FATAL] 无法加载 scripts/run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------- 第 1 层：测试 ----------


def run_tests() -> bool:
    _echo("【1/4】契约与单元测试")
    _rule()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(ROOT),
    )
    if proc.returncode == 0:
        _echo("测试全部通过")
        return True
    _echo("测试失败——后续门禁不再有意义，先修测试")
    return False


# ---------- 第 2 层：迁移可逆性 ----------


def run_migrations() -> bool:
    """在临时库跑 up → down → up，比对 schema 指纹。

    刻意不碰 data/app.sqlite3：门禁不该有副作用，否则「跑一下检查」
    这件事本身就需要勇气，人就会不去跑。
    """
    _echo("【2/4】迁移可逆性")
    _rule()
    for path in (str(SRC), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)

    from xborder_insight.db import migrate
    from xborder_insight.db.connection import connect

    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "gate.sqlite3")
        try:
            result = migrate.roundtrip(conn)
        except Exception as exc:  # 迁移自己炸了：给出可读诊断，而不是甩一坨 traceback
            _echo(f"迁移执行失败：{type(exc).__name__}: {exc}")
            return False
        finally:
            conn.close()

    if result["ok"]:
        _echo(f"往返通过  版本={result['version']}  表={', '.join(result['tables'])}")
        return True

    _echo("迁移往返失败：")
    if result.get("reason"):
        _echo(f"  原因：{result['reason']}")
    if result["leftover_after_down"]:
        _echo(f"  down(0) 后残留表：{result['leftover_after_down']}（down() 删漏了）")
    if result["schema_drift"]:
        _echo(f"  schema 漂移：{result['schema_drift']}（重建后结构不一致）")
    return False


# ---------- 第 2 层：评测 ----------


def run_eval() -> dict:
    _echo("【3/4】评测复跑")
    _rule()
    module = load_eval_module()
    result = asyncio.run(module.run())
    _echo(
        f"F1 {result['f1']:.4f}  P {result['precision']:.4f}  R {result['recall']:.4f}  "
        f"解析失败率 {result['parse_failure_rate']:.2%}  "
        f"{result['tokens_per_review']:.0f} token/条  "
        f"P95 {result['latency_p95_ms']:.0f}ms"
    )
    return result


# ---------- 第 3 层：基线 diff ----------


def to_snapshot(result: dict) -> dict:
    snapshot = {field: result[field] for field in SNAPSHOT_FIELDS}
    snapshot["failing_review_ids"] = result["failing_review_ids"]
    snapshot["captured_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snapshot["schema_version"] = BASELINE_SCHEMA
    snapshot["python"] = sys.version.split()[0]
    return snapshot


def save_baseline(snapshot: dict) -> None:
    BASELINE_PATH.parent.mkdir(exist_ok=True)
    payload = {
        "_comment": "基线快照。由 `python scripts/check.py --update-baseline` 生成，"
                    "手工修改会让后续所有「没变差」的证明失去参照物。",
        **snapshot,
    }
    BASELINE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _echo(f"基线已写入 {BASELINE_PATH.relative_to(ROOT)}")


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}" if abs(value) < 100 else f"{value:.0f}"
    return str(value)


def _width(text: str) -> int:
    """中文与全角字符占两列，按显示宽度算才能在终端里对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - _width(text))
    return gap + text if right else text + gap


def compare(current: dict, baseline: dict, gates: dict) -> list[tuple[str, str, str, str, str]]:
    """返回 (指标, 基线, 当前, 变化, 判定)。判定为 PASS / FAIL / WARN。"""
    rows: list[tuple[str, str, str, str, str]] = []

    def add(name: str, key: str, *, drop_key: str | None = None, warn_only: bool = False) -> None:
        base = float(baseline[key])
        cur = float(current[key])
        delta = cur - base
        verdict = "PASS"
        if drop_key and (base - cur) > gates[drop_key]:
            verdict = "WARN" if warn_only else "FAIL"
        rows.append((name, _fmt(base), _fmt(cur), f"{delta:+.4f}", verdict))

    add("Precision", "precision", drop_key="precision_drop_max")
    add("Recall", "recall", drop_key="recall_drop_max")
    add("F1", "f1", drop_key="f1_drop_max")

    failure_rate = float(current["parse_failure_rate"])
    rows.append(
        (
            "解析失败率",
            "—",
            f"{failure_rate:.2%}",
            "—",
            "FAIL" if failure_rate > gates["parse_failure_rate_max"] else "PASS",
        )
    )

    calls = float(current["calls_per_review"])
    rows.append(
        (
            "调用次数/条",
            _fmt(float(baseline["calls_per_review"])),
            _fmt(calls),
            f"{calls - float(baseline['calls_per_review']):+.4f}",
            "FAIL" if calls > gates["calls_per_review_max"] else "PASS",
        )
    )

    tokens = float(current["tokens_per_review"])
    rows.append(
        (
            "token/条",
            _fmt(float(baseline["tokens_per_review"])),
            _fmt(tokens),
            f"{tokens - float(baseline['tokens_per_review']):+.2f}",
            "FAIL" if tokens > gates["tokens_per_review_max"] else "PASS",
        )
    )

    p95 = float(current["latency_p95_ms"])
    base_p95 = float(baseline["latency_p95_ms"]) or 1.0
    drift = p95 / base_p95
    verdict = "PASS"
    if drift > gates["latency_p95_drift_factor_max"]:
        verdict = "FAIL"
    if current["run_mode"] != "mock" and p95 > gates["latency_p95_ms_max_live"]:
        verdict = "FAIL"
    rows.append(("P95 延迟", f"{base_p95:.0f}ms", f"{p95:.0f}ms", f"×{drift:.2f}", verdict))

    # 失败样本集合变化：F1 没掉但「错的地方」变了，说明修一个换一个，
    # 属于典型指标假性稳定。F1 有提升时不算问题，只提示。
    base_failing = set(baseline.get("failing_review_ids", []))
    cur_failing = set(current["failing_review_ids"])
    if base_failing != cur_failing:
        improved = float(current["f1"]) > float(baseline["f1"])
        rows.append(
            (
                "失败样本集合",
                f"{len(base_failing)} 条",
                f"{len(cur_failing)} 条",
                f"+{len(cur_failing - base_failing)}/-{len(base_failing - cur_failing)}",
                "WARN" if improved else "FAIL",
            )
        )
    else:
        rows.append(("失败样本集合", f"{len(base_failing)} 条", f"{len(cur_failing)} 条", "无变化", "PASS"))

    return rows


def print_table(rows: list[tuple[str, str, str, str, str]]) -> None:
    _echo(f"{_pad('指标', 16)}{_pad('基线', 12, right=True)}{_pad('当前', 12, right=True)}"
          f"{_pad('变化', 12, right=True)}  判定")
    _rule()
    for name, base, cur, delta, verdict in rows:
        _echo(f"{_pad(name, 16)}{_pad(base, 12, right=True)}{_pad(cur, 12, right=True)}"
              f"{_pad(delta, 12, right=True)}  {verdict}")


# ---------- 主流程 ----------


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="质量门禁")
    parser.add_argument("--tests-only", action="store_true", help="只跑测试")
    parser.add_argument("--db-only", action="store_true", help="只跑迁移可逆性校验")
    parser.add_argument("--eval-only", action="store_true", help="只跑评测与基线对比")
    parser.add_argument("--update-baseline", action="store_true", help="把当前指标写成新基线")
    parser.add_argument("--show", action="store_true", help="只打印指标，不跑任何东西")
    args = parser.parse_args(argv)

    partial = [args.tests_only, args.db_only, args.eval_only]
    if sum(partial) > 1:
        _echo("--tests-only / --db-only / --eval-only 只能三选一")
        return 2

    if args.show:
        if not BASELINE_PATH.exists():
            _echo("还没有基线快照，先跑一次 python scripts/check.py --update-baseline")
            return 2
        baseline = load_json(BASELINE_PATH)
        _rule("=")
        _echo(f"当前基线  {baseline['captured_at']}  模式={baseline['run_mode']}  样本={baseline['reviews']}")
        _rule("=")
        for field in ("precision", "recall", "f1", "parse_success_rate",
                      "calls_per_review", "tokens_per_review", "latency_p95_ms"):
            _echo(f"  {field:<20}{_fmt(baseline[field])}")
        _echo(f"  失败样本 {len(baseline.get('failing_review_ids', []))} 条")
        return 0

    gates = load_gates()

    run_layer_tests = not (args.db_only or args.eval_only)
    run_layer_db = not (args.tests_only or args.eval_only)
    run_layer_eval = not (args.tests_only or args.db_only)

    if run_layer_tests and not run_tests():
        return 1
    if run_layer_db and not run_migrations():
        return 1
    if not run_layer_eval:
        _echo()
        _echo("门禁子集通过。")
        return 0

    current = run_eval()
    snapshot = to_snapshot(current)

    if args.update_baseline:
        save_baseline(snapshot)
        _echo("基线已刷新。注意：这等于宣布「今后的比较都以此为参照」，"
              "如果是为了让失败变绿而刷新，那门禁就失去意义了。")
        return 0

    if not BASELINE_PATH.exists():
        save_baseline(snapshot)
        _echo("首次运行：已用当前指标建立基线。再跑一次即可开始做退化比对。")
        return 0

    baseline = load_json(BASELINE_PATH)

    _echo("【4/4】与基线对比")
    _rule()
    if baseline.get("run_mode") != current["run_mode"] or baseline.get("model") != current["model"]:
        _echo(
            f"[WARN] 口径不一致：基线与当前为 {baseline.get('run_mode')}/{baseline.get('model')} "
            f"vs {current['run_mode']}/{current['model']}，数字不可直接比较。"
        )
        _echo("       换口径请显式刷新基线：python scripts/check.py --update-baseline")
        return 1
    if baseline.get("reviews") != current["reviews"]:
        _echo(
            f"[WARN] 样本数变化：{baseline.get('reviews')} → {current['reviews']}，"
            "指标不再可比，需先刷新基线。"
        )
        return 1

    rows = compare(current, baseline, gates)
    print_table(rows)
    _rule()

    failures = [row for row in rows if row[4] == "FAIL"]
    warnings = [row for row in rows if row[4] == "WARN"]

    if failures:
        _echo(f"门禁未通过：{len(failures)} 项")
        for name, base, cur, delta, _ in failures:
            _echo(f"  · {name}: 基线 {base} → 当前 {cur} ({delta})")
        _echo("如果这次变化是预期的（例如换了模型或扩了标注集），"
              "请先更新 README 与 docs，再显式刷新基线：python scripts/check.py --update-baseline")
        return 1

    if warnings:
        _echo(f"通过，但有 {len(warnings)} 项提示：")
        for name, base, cur, delta, _ in warnings:
            _echo(f"  · {name}: 基线 {base} → 当前 {cur} ({delta})")
        return 0

    _echo("全部通过：指标未退化，成本与延迟在预算内。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
