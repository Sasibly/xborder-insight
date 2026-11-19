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


async def run() -> dict[str, object]:
    started = time.perf_counter()

    gold = load_gold()
    reviews = []
    for dataset in ("wireless_charger", "pet_fountain"):
        reviews.extend(load_reviews(dataset))

    client = build_llm_client(settings)
    outcomes, stats = await extract_many(client, reviews)
    await client.aclose()

    tp = fp = fn = 0
    errors: list[dict[str, object]] = []
    for outcome in outcomes:
        truth = gold.get(outcome.review_id, set())
        predicted: set[str] = set()
        if outcome.extraction is not None:
            predicted = {
                p.label for p in outcome.extraction.points if p.polarity == "negative"
            }
        hit = truth & predicted
        tp += len(hit)
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

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    total_tokens = stats.prompt_tokens + stats.completion_tokens
    per_review = max(1, stats.total)

    result = {
        "run_mode": settings.run_mode,
        "model": settings.llm_model if not settings.is_mock else "mock-rules",
        "embedding": settings.embedding_provider,
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
    print("未完全匹配样本：")
    for case in result["error_cases"]:
        print(f"  {case['review_id']}  漏检={case['missing']}  误检={case['extra']}")
    print("=" * 62)
    print("报告已写入 reports/eval_report.json")


if __name__ == "__main__":
    main()
