"""机会分回归测试。

为什么值得单独锁这个公式：它是整个项目对外输出的唯一排序依据。
一旦有人「顺手」调权重或改压缩函数，机会点顺序就会变，而这种变化
在页面上看不出来（分数从 0.66 变 0.61 没人会觉得异常），只有硬断言能发现。

测试里既硬编码一个手算期望值（防止权重漂移），也做结构断言（防止逻辑反转）。
"""

from __future__ import annotations

import math

import pytest

from xborder_insight.pipeline.score import (
    SATURATION,
    W_MENTION,
    W_NEGATIVE,
    W_SEVERITY,
    ClusterMember,
    build_theme,
    mention_score,
)
from xborder_insight.schemas import ExtractedPoint


def member(review_id: str, polarity: str = "negative", confidence: float = 0.8, evidence: str = "") -> ClusterMember:
    return ClusterMember(
        review_id=review_id,
        point=ExtractedPoint(
            label="charging_speed",
            polarity=polarity,
            evidence=evidence or f"evidence-{review_id}",
            confidence=confidence,
        ),
    )


# ---------- 权重本身：改动即报警 ----------


def test_weights_sum_to_one():
    assert W_MENTION + W_NEGATIVE + W_SEVERITY == pytest.approx(1.0)


def test_weights_are_frozen():
    """权重是业务决策，不是可随手调的参数。变了就该改文档而不是悄悄变。"""
    assert (W_MENTION, W_NEGATIVE, W_SEVERITY) == (0.55, 0.35, 0.10)


# ---------- 提及数饱和 ----------


def test_mention_score_is_monotonic_and_capped():
    scores = [mention_score(n) for n in range(0, 15)]
    assert scores[0] == 0.0
    assert scores == sorted(scores)  # 单调不减
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert mention_score(int(SATURATION)) == pytest.approx(1.0)
    assert mention_score(1000) == 1.0  # 头部主题不许吃掉全部区分度


def test_mention_score_uses_log_compression():
    """线性会给头部主题过高权重；这里锁住对数形态。"""
    assert mention_score(2) == pytest.approx(math.log(3) / math.log(1 + SATURATION))


# ---------- 端到端手算 ----------


def test_opportunity_score_matches_hand_calculation():
    """3 条提及（2 负 1 正，置信度 0.9 / 0.8 / 0.7）的手算期望值。"""
    members = [
        member("r1", "negative", 0.9, "takes forever to charge"),
        member("r2", "negative", 0.8, "20 minutes and still hot"),
        member("r3", "positive", 0.7, "works fine for me"),
    ]
    theme = build_theme("T01", "charging_speed", members, scenes=[members])

    expected = (
        0.55 * (math.log(4) / math.log(1 + SATURATION))
        + 0.35 * (2 / 3)
        + 0.10 * 0.8
    )
    assert theme.opportunity_score == pytest.approx(expected, abs=1e-4)
    assert theme.opportunity_score == pytest.approx(0.6603, abs=1e-4)  # 硬编码护栏
    assert theme.mention_count == 3
    assert theme.negative_ratio == pytest.approx(0.6667, abs=1e-4)
    assert theme.severity == pytest.approx(0.8)


def test_mention_count_dedupes_same_review():
    """一条评论在同一标签下出现两次，只算一次提及——否则刷单式刷分。"""
    members = [
        member("r1", "negative", 0.9),
        member("r1", "negative", 0.8),
        member("r2", "negative", 0.7),
    ]
    theme = build_theme("T01", "charging_speed", members, scenes=[members])
    assert theme.mention_count == 2


def test_all_positive_theme_scores_lower():
    """全是好评的标签不该被当成选品机会。"""
    negative_case = build_theme("T01", "heat", [member("r1", "negative", 0.9)], [[member("r1", "negative", 0.9)]])
    positive_case = build_theme("T02", "heat", [member("r1", "positive", 0.9)], [[member("r1", "positive", 0.9)]])
    assert positive_case.opportunity_score < negative_case.opportunity_score
    assert positive_case.negative_ratio == 0.0


# ---------- 证据选取 ----------


def test_evidence_takes_best_per_scene_and_dedupes():
    s1 = [member("r1", "negative", 0.6, "same text"), member("r2", "negative", 0.9, "same text")]
    s2 = [member("r3", "negative", 0.5, "other text")]
    theme = build_theme("T01", "heat", s1 + s2, [s1, s2])

    assert theme.scene_count == 2
    assert theme.evidence == ["same text", "other text"]  # 去重后只留一条


def test_evidence_capped_at_three():
    scenes = [
        [member(f"r{i}", "negative", 0.9, f"evidence {i}")] for i in range(6)
    ]
    flat = [m for scene in scenes for m in scene]
    theme = build_theme("T01", "heat", flat, scenes)
    assert len(theme.evidence) == 3


def test_empty_members_do_not_crash():
    theme = build_theme("T01", "heat", [], [])
    assert theme.mention_count == 0
    assert theme.opportunity_score == 0.0
    assert theme.evidence == []
