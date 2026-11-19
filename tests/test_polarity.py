"""极性判定回归测试：把已经踩过的坑钉死。

背景：首版只看句内情绪词，于是
    "Takes forever"    → 没有 negative 词 → neutral
    "does not seal"    → 没有 negative 词 → neutral
这类点被标成中性后，评测按「只统计 negative」的口径把它们过滤掉，
表现为大面积漏检，F1 卡在 0.625 上不去。

修法是加星级兜底：词表无信号时，低星评论本身就算负面证据。
本文件锁的就是这条兜底逻辑——它一旦被删，召回率会立刻掉 24 个点。
"""

from __future__ import annotations

import pytest

from xborder_insight.llm import _polarity


# ---------- 词表有信号：按词表判 ----------


def test_negative_cue_wins():
    assert _polarity("It gets hot to the touch", 5) == "negative"


def test_positive_cue_wins():
    assert _polarity("I love this thing, works well", 5) == "positive"


def test_mixed_cues_treated_as_negative():
    """褒贬同句时按负面处理：抱怨才是改进信号。"""
    assert _polarity("It looks great but it broke after a week", 5) == "negative"
    assert _polarity("Love the design, though it gets hot", 4) == "negative"


@pytest.mark.xfail(strict=True, reason="已知缺口：'died' 在 LABEL_HINTS 里却不在 NEGATIVE_CUES，"
                                      "五星评论里的隐性故障会被判成正面。补词表需重拍基线。")
def test_known_gap_died_is_not_a_negative_cue():
    assert _polarity("The pump died after three weeks", 5) == "negative"


# ---------- 词表无信号：星级兜底（曾经的真 bug） ----------


@pytest.mark.parametrize(
    "sentence",
    [
        "Takes forever to charge my phone",  # 无 negative 词
        "The lid does not seal properly",     # 否定句，词表里没有 not
        "Twenty minutes and it is still warm",  # "warm" 不在负面词表
        "Stopped after three weeks",          # "stopped" 在 hint 表但不在 cue 表
    ],
)
def test_low_rating_without_cues_is_negative(sentence: str):
    assert _polarity(sentence, 2) == "negative"
    assert _polarity(sentence, 1) == "negative"


def test_mid_rating_without_cues_is_neutral():
    """三星且无情绪词：不确定就说不确定，不要硬判。"""
    assert _polarity("It arrived on Tuesday", 3) == "neutral"


def test_high_rating_without_cues_is_positive():
    assert _polarity("It arrived on Tuesday", 5) == "positive"


def test_star_fallback_does_not_override_explicit_cues():
    """星级只是兜底，不能盖过明确的负面词。"""
    assert _polarity("it broke immediately", 5) == "negative"
    assert _polarity("works well", 1) == "positive"
