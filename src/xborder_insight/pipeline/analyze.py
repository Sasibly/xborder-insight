"""管道编排：把抽取、聚类、打分串成一次可复现的分析。

层级设计：
  L1  taxonomy 标签（闭集，来自模型抽取）
  L2  同标签内的语义子簇（来自证据短语向量聚类）
输出取 L2，因为它才是可执行的选品机会，而不是笼统的品类标签。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from ..config import Settings
from ..llm import LLMClient
from ..logging import log_event
from ..schemas import AnalysisReport, Review, Theme
from .cluster import build_embedder, greedy_cluster
from .extract import extract_many
from .score import ClusterMember, build_theme

import logging

logger = logging.getLogger(__name__)


@dataclass
class AnalyzeResult:
    report: AnalysisReport
    themes: list[Theme]
    unparsed_ids: list[str]
    repair_attempts: int


async def analyze_reviews(
    reviews: list[Review],
    settings: Settings,
    client: LLMClient,
    dataset: str,
    top_k: int = 5,
    concurrency: int = 6,
) -> AnalyzeResult:
    started = time.perf_counter()

    outcomes, stats = await extract_many(client, reviews, concurrency=concurrency)

    # 按 (taxonomy 标签, 极性) 分组，组内再做语义聚类。
    #
    # 极性必须是分组键，不能只当组内属性：同一标签下的「充电快」和「充电慢」在向量
    # 空间里几乎重合（措辞同源），聚类必然把它们并成一簇，于是输出一个既想保持又想
    # 改进、无法执行的机会点。标定集里 43 对跨极性样本，判为同主题的是 0 对。
    # 实测 ARI：仅按标签分组 0.195 → 加上极性 0.767（同一批向量、同一阈值）。
    grouped: dict[tuple[str, str], list[ClusterMember]] = {}
    unparsed_ids: list[str] = []
    for outcome in outcomes:
        if outcome.extraction is None:
            unparsed_ids.append(outcome.review_id)
            continue
        if not outcome.extraction.is_relevant:
            continue
        for point in outcome.extraction.points:
            grouped.setdefault((point.label, point.polarity), []).append(
                ClusterMember(review_id=outcome.review_id, point=point)
            )

    embedder = build_embedder(settings)
    themes: list[Theme] = []
    for (label, polarity), members in grouped.items():
        evidences = [m.point.evidence for m in members]
        vectors = embedder.encode(evidences)
        assignments = greedy_cluster(np.asarray(vectors), settings.cluster_threshold)

        buckets: dict[int, list[ClusterMember]] = {}
        for member, cluster_id in zip(members, assignments):
            buckets.setdefault(int(cluster_id), []).append(member)

        scenes = sorted(
            buckets.values(),
            key=lambda s: max(x.point.confidence for x in s),
            reverse=True,
        )
        themes.append(
            build_theme(
                theme_id="pending",
                label=label,
                members=members,
                scenes=scenes,
                polarity=polarity,
            )
        )

    if hasattr(embedder, "aclose"):
        embedder.aclose()

    themes.sort(key=lambda t: t.opportunity_score, reverse=True)
    themes = themes[:top_k]
    for idx, theme in enumerate(themes, start=1):
        theme.theme_id = f"T{idx:02d}"

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    report = AnalysisReport(
        dataset=dataset,
        review_count=stats.total,
        parsed_count=stats.parsed,
        parse_success_rate=round(stats.parse_success_rate, 4),
        themes=themes,
        llm_calls=stats.llm_calls,
        prompt_tokens=stats.prompt_tokens,
        completion_tokens=stats.completion_tokens,
        elapsed_ms=elapsed_ms,
    )

    log_event(
        logger,
        "analyze.done",
        dataset=dataset,
        reviews=stats.total,
        parsed=stats.parsed,
        themes=len(themes),
        elapsed_ms=elapsed_ms,
        repair_attempts=stats.repair_attempts,
    )
    return AnalyzeResult(
        report=report,
        themes=themes,
        unparsed_ids=unparsed_ids,
        repair_attempts=stats.repair_attempts,
    )
