"""机会打分。

结构约定：
  主主题 = taxonomy 标签（可靠、可解释、跨批次可比）
  子场景 = 同一标签内的语义簇（用于挑代表证据、判断痛点有几个不同场景）

刻意不用「让模型直接打 0-100 分」：不可复现、无法解释、面试必被追问。
这里用三项绝对值加权，公式写在代码里，任何人拿同样输入都能算出同样结果。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..schemas import ExtractedPoint, Theme, display_name

W_MENTION = 0.55
W_NEGATIVE = 0.35
W_SEVERITY = 0.10

SATURATION = 8.0  # 提及数达到该值时需求广度视为饱和


@dataclass
class ClusterMember:
    review_id: str
    point: ExtractedPoint


def mention_score(mention_count: int) -> float:
    """对数压缩：避免头部主题把机会分全部吃掉。"""
    return min(1.0, math.log1p(mention_count) / math.log1p(SATURATION))


def build_theme(
    theme_id: str,
    label: str,
    members: list[ClusterMember],
    scenes: list[list[ClusterMember]],
    polarity: str = "mixed",
) -> Theme:
    review_ids = {m.review_id for m in members}
    mention_count = len(review_ids)

    negative = sum(1 for m in members if m.point.polarity == "negative")
    negative_ratio = negative / len(members) if members else 0.0

    severity = sum(m.point.confidence for m in members) / len(members) if members else 0.0

    opportunity = (
        W_MENTION * mention_score(mention_count)
        + W_NEGATIVE * negative_ratio
        + W_SEVERITY * severity
    )

    # 每个子场景只取置信度最高的一条作为代表证据，避免同一段抱怨刷屏
    evidence: list[str] = []
    for scene in scenes:
        if not scene:
            continue
        best = max(scene, key=lambda m: m.point.confidence)
        text = best.point.evidence.strip()
        if text and text not in evidence:
            evidence.append(text)

    return Theme(
        theme_id=theme_id,
        label=label,
        polarity=polarity,
        display_name=display_name(label),
        mention_count=mention_count,
        negative_ratio=round(negative_ratio, 4),
        severity=round(severity, 4),
        opportunity_score=round(opportunity, 4),
        scene_count=len(scenes),
        evidence=evidence[:3],
    )
