"""扩充聚类评测的 evidence 集：对尚未覆盖的评论跑一次 live 抽取。

为什么单独写脚本而不是塞进 run_eval：
run_eval 的产物是指标；这里要的是**中间产物**（逐条 evidence）。混在一起会让
「重跑评测」意外改写标注语料——标注语料是基线的一部分，必须显式操作。

三条硬规则：
1. **只追加**：已覆盖的 review_id 一律跳过，旧 evidence 一个字都不改。
   这是「同品类 0.8108 可复现」的同一条原则——扩数据不能污染旧口径。
2. **只收 negative / positive**：neutral 既不构成机会点也不构成「需要维持」的改进项，
   收进来会让簇划分出现无法定义的空洞。
3. **先写临时文件**：theme 需要人工判定，未经标注的 evidence 不进正式语料。

用法：
    RUN_MODE=live LLM_API_KEY=... python scripts/extend_cluster_evidence.py --dry-run
    RUN_MODE=live LLM_API_KEY=... python scripts/extend_cluster_evidence.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from cluster_themes_v2 import EVIDENCE, load_evidence  # noqa: E402
from xborder_insight.config import settings  # noqa: E402
from xborder_insight.llm import build_llm_client  # noqa: E402
from xborder_insight.pipeline.extract import extract_many  # noqa: E402
from xborder_insight.pipeline.ingest import load_reviews  # noqa: E402

STAGING = ROOT / "data" / "eval" / "_evidence_new.jsonl"
KEEP_POLARITY = {"negative", "positive"}


def discover_datasets() -> list[str]:
    return sorted(p.stem for p in (ROOT / "data" / "samples").glob("*.jsonl"))


async def run(limit: int | None, dry_run: bool) -> int:
    if settings.is_mock:
        raise SystemExit("必须 RUN_MODE=live：mock 是关键词规则，产出的 evidence 不是模型输出")

    existing = load_evidence()
    covered = {row["review_id"] for row in existing}
    print(f"已有 evidence {len(existing)} 条，覆盖 {len(covered)} 条评论")

    reviews = []
    for dataset in discover_datasets():
        for review in load_reviews(dataset):
            if review.review_id not in covered:
                reviews.append(review)
    if limit:
        reviews = reviews[:limit]
    print(f"待抽取 {len(reviews)} 条评论（模型 {settings.llm_model}）")
    if dry_run:
        print("dry-run：不调用模型，退出")
        return 0
    if not reviews:
        print("没有需要抽取的评论")
        return 0

    client = build_llm_client(settings)
    outcomes, stats = await extract_many(client, reviews)
    await client.aclose()

    rows: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    failed: list[str] = []
    for outcome in outcomes:
        if outcome.extraction is None:
            failed.append(outcome.review_id)
            continue
        for point in outcome.extraction.points:
            if point.polarity not in KEEP_POLARITY:
                continue
            evidence_text = (point.evidence or "").strip()
            if not evidence_text:
                continue
            key = (outcome.review_id, point.label, point.polarity, evidence_text)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "review_id": outcome.review_id,
                    "label": point.label,
                    "polarity": point.polarity,
                    "evidence": evidence_text,
                    "confidence": round(float(point.confidence), 2),
                }
            )

    STAGING.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )

    print(f"\n解析成功 {stats.parsed}/{stats.total}，调用 {stats.llm_calls} 次，"
          f"token {stats.prompt_tokens + stats.completion_tokens}")
    print(f"抽出 evidence {len(rows)} 条 → {STAGING.relative_to(ROOT)}")
    if failed:
        print(f"抽取失败 {len(failed)} 条：{failed}")

    by_label: dict[str, int] = {}
    for row in rows:
        by_label[row["label"]] = by_label.get(row["label"], 0) + 1
    print("\n新 evidence 的 label 分布（待打 theme）：")
    for label, count in sorted(by_label.items(), key=lambda x: -x[1]):
        print(f"  {label:16s} {count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="扩充聚类 evidence 集")
    parser.add_argument("--limit", type=int, default=None, help="只抽前 N 条，用于小样本验证")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.limit, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
