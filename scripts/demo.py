"""命令行演示：一次跑完两个品类，打印机会点排序。

用法：
    .venv/Scripts/python.exe scripts/demo.py
    .venv/Scripts/python.exe scripts/demo.py wireless_charger
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xborder_insight.config import settings  # noqa: E402
from xborder_insight.llm import build_llm_client  # noqa: E402
from xborder_insight.pipeline.analyze import analyze_reviews  # noqa: E402
from xborder_insight.pipeline.ingest import load_reviews  # noqa: E402

DEFAULT_DATASETS = ("wireless_charger", "pet_fountain")


async def run_dataset(client, dataset: str, top_k: int = 5) -> None:
    reviews = load_reviews(dataset)
    result = await analyze_reviews(
        reviews=reviews,
        settings=settings,
        client=client,
        dataset=dataset,
        top_k=top_k,
    )
    report = result.report

    print()
    print("=" * 74)
    print(f"数据集 {dataset}   评论 {report.review_count} 条   解析成功率 {report.parse_success_rate:.1%}")
    print(f"耗时 {report.elapsed_ms} ms   LLM 调用 {report.llm_calls} 次   "
          f"tokens {report.prompt_tokens}+{report.completion_tokens}")
    print("=" * 74)
    print(f"{'机会分':<8}{'标签':<16}{'提及':<6}{'负向':<6}{'场景':<6}代表证据")
    print("-" * 74)
    for theme in report.themes:
        print(
            f"{theme.opportunity_score:<8.3f}"
            f"{theme.display_name:<16}"
            f"{theme.mention_count:<6}"
            f"{theme.negative_ratio:<6.0%}"
            f"{theme.scene_count:<6}"
            f"{theme.evidence[0][:28] if theme.evidence else '-'}"
        )
    print("-" * 74)
    for theme in report.themes[:3]:
        print(f"\n[{theme.theme_id}] {theme.display_name}  (机会分 {theme.opportunity_score:.3f})")
        for quote in theme.evidence:
            print(f"    · {quote}")
    if result.unparsed_ids:
        print(f"\n未解析样本 {len(result.unparsed_ids)} 条：{result.unparsed_ids}")


async def main() -> None:
    datasets = tuple(sys.argv[1:]) or DEFAULT_DATASETS
    client = build_llm_client(settings)
    print(f"运行模式: {settings.run_mode}   客户端: {client.name}")
    try:
        for dataset in datasets:
            await run_dataset(client, dataset)
    finally:
        await client.aclose()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
