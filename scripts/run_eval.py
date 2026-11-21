"""抽取质量评测：micro Precision / Recall / F1 + 运行成本与延迟。

口径说明（一定要在 README 里写清，否则数字没有意义）：
- 只评估「痛点」抽取，positive / neutral 的 point 不计入分母
- 以 (review_id, label) 为最小单元做 micro 平均，避免单条多标签样本被重复加权
- gold 为人工逐条标注，标签取自同一 taxonomy 闭集
- 成本口径用 token 与调用次数，不折算人民币：单价需要官方定价来源，
  未核实前不写钱数（见 P1 成本参数表）。这是刻意的，不是遗漏。

本脚本同时是门禁的数据源：`scripts/check.py` 直接 import run() 拿指标做断言。
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xborder_insight.config import settings  # noqa: E402
from xborder_insight.llm import build_llm_client  # noqa: E402
from xborder_insight.pipeline.extract import extract_many  # noqa: E402
from xborder_insight.pipeline.ingest import load_reviews  # noqa: E402

REPORT_PATH = ROOT / "reports" / "eval_report.json"


def percentile(values: list[int], p: float) -> float:
    """nearest-rank 百分位，样本少时不插值——插值会掩盖尾部抖动。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return float(ordered[rank - 1])


def load_gold() -> dict[str, set[str]]:
    path = ROOT / "data" / "eval" / "gold_labels.jsonl"
    gold: dict[str, set[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        gold[obj["review_id"]] = set(obj["labels"])
    return gold


def discover_datasets() -> list[str]:
    """自动发现样本集，不写死清单。

    写死清单的失败模式很隐蔽：加了新品类却忘了加进评测，新品类永远不会进指标，
    而指标本身看起来完全正常——覆盖率下降不会让任何断言变红。
    """
    return sorted(p.stem for p in (ROOT / "data" / "samples").glob("*.jsonl"))


def score(outcomes, gold: dict[str, set[str]]) -> dict[str, object]:
    """micro 平均：以 (review_id, label) 为最小单元。"""
    tp = fp = fn = 0
    errors: list[dict[str, object]] = []
    for outcome in outcomes:
        truth = gold.get(outcome.review_id, set())
        predicted: set[str] = set()
        if outcome.extraction is not None:
            predicted = {
                p.label for p in outcome.extraction.points if p.polarity == "negative"
            }
        tp += len(truth & predicted)
        fp += len(predicted - truth)
        fn += len(truth - predicted)
        if predicted != truth:
            errors.append(
                {
                    "review_id": outcome.review_id,
                    "missing": sorted(truth - predicted),
                    "extra": sorted(predicted - truth),
                }
            )

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "reviews": len(outcomes),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "failing_review_ids": sorted(c["review_id"] for c in errors),
        "error_cases": errors,
    }


async def run() -> dict[str, object]:
    started = time.perf_counter()

    gold = load_gold()
    datasets = discover_datasets()
    reviews = []
    owner: dict[str, str] = {}
    for dataset in datasets:
        batch = load_reviews(dataset)
        for review in batch:
            owner[review.review_id] = dataset
        reviews.extend(batch)

    # 样本缺金标准时，「真值」会退化成空集：预测全记 FP 或全记 TP，
    # 指标依然是个合法数字。这种静默错配比崩掉更危险，直接拒绝跑。
    missing_gold = sorted(r.review_id for r in reviews if r.review_id not in gold)
    if missing_gold:
        raise SystemExit(
            f"以下样本没有金标准，拒绝评测：{missing_gold[:10]}"
            f"{' 等 %d 条' % len(missing_gold) if len(missing_gold) > 10 else ''}"
        )

    client = build_llm_client(settings)
    outcomes, stats = await extract_many(client, reviews)
    await client.aclose()

    overall = score(outcomes, gold)
    tp, fp, fn = overall["tp"], overall["fp"], overall["fn"]
    precision, recall, f1 = overall["precision"], overall["recall"], overall["f1"]
    errors = overall["error_cases"]

    by_dataset = {}
    for dataset in datasets:
        subset = [o for o in outcomes if owner[o.review_id] == dataset]
        if subset:
            by_dataset[dataset] = score(subset, gold)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    total_tokens = stats.prompt_tokens + stats.completion_tokens
    per_review = max(1, stats.total)

    result = {
        "run_mode": settings.run_mode,
        "model": settings.llm_model if not settings.is_mock else "mock-rules",
        "embedding": settings.embedding_provider,
        "datasets": datasets,
        "reviews": stats.total,
        "parsed": stats.parsed,
        "parse_success_rate": round(stats.parse_success_rate, 4),
        "parse_failure_rate": round(1 - stats.parse_success_rate, 4),
        "repair_attempts": stats.repair_attempts,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        # 成本口径：token 与调用次数，可复现、不依赖价格假设
        "llm_calls": stats.llm_calls,
        "calls_per_review": round(stats.llm_calls / per_review, 4),
        "prompt_tokens": stats.prompt_tokens,
        "completion_tokens": stats.completion_tokens,
        "tokens_per_review": round(total_tokens / per_review, 2),
        # 延迟口径：单次 LLM 调用，已排除数据加载与聚合开销
        "latency_p50_ms": percentile(stats.latencies_ms, 50),
        "latency_p95_ms": percentile(stats.latencies_ms, 95),
        "latency_max_ms": float(max(stats.latencies_ms)) if stats.latencies_ms else 0.0,
        "elapsed_ms": elapsed_ms,
        "failing_review_ids": sorted(c["review_id"] for c in errors),
        "error_cases": errors,
        # 分品类指标：用来量化「规则基线在单品类能跑、跨品类就塌」这件事。
        # 总量指标会掩盖它——平均值把两类产品的中等表现混成一个看不出问题的数字。
        "by_dataset": by_dataset,
    }

    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = asyncio.run(run())

    print("=" * 62)
    print(f"抽取评测  模式={result['run_mode']}  模型={result['model']}  embedding={result['embedding']}")
    print("=" * 62)
    print(f"样本数           {result['reviews']}")
    print(f"解析成功率       {result['parse_success_rate']:.2%}  (修复轮 {result['repair_attempts']})")
    print(f"TP / FP / FN     {result['tp']} / {result['fp']} / {result['fn']}")
    print(f"Precision        {result['precision']:.4f}")
    print(f"Recall           {result['recall']:.4f}")
    print(f"F1               {result['f1']:.4f}")
    print("-" * 62)
    print(f"LLM 调用         {result['llm_calls']}  ({result['calls_per_review']:.2f} 次/条)")
    print(f"Token 用量       {result['prompt_tokens']} in + {result['completion_tokens']} out"
          f"  ({result['tokens_per_review']:.0f} token/条)")
    print(f"调用延迟         P50 {result['latency_p50_ms']:.0f}ms · "
          f"P95 {result['latency_p95_ms']:.0f}ms · Max {result['latency_max_ms']:.0f}ms")
    print(f"总耗时           {result['elapsed_ms']}ms")
    print("-" * 62)
    print("分品类：")
    for dataset, item in result["by_dataset"].items():
        print(f"  {dataset:<20}{item['reviews']:>4} 条   "
              f"P {item['precision']:.4f}  R {item['recall']:.4f}  F1 {item['f1']:.4f}"
              f"   (TP {item['tp']} / FP {item['fp']} / FN {item['fn']})")
    print("-" * 62)
    print("未完全匹配样本：")
    for case in result["error_cases"]:
        print(f"  {case['review_id']}  漏检={case['missing']}  误检={case['extra']}")
    print("=" * 62)
    print("报告已写入 reports/eval_report.json")


if __name__ == "__main__":
    main()
