"""契约层回归测试：把「模型输出不可信」这条前提钉死。

锁三件事：
1. 格式漂移（围栏、前后废话、顶层数组）能被救回来
2. 语义越界（标签走出闭集、多出字段）必须被拦住，不许静默流进下游
3. 修不好时走降级，标记 unparsed 并计次——而不是抛异常把整批任务带崩

第 3 条是本项目最核心的工程点，所以单独用假客户端模拟「连续两次坏输出」。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from xborder_insight.llm import LLMResponse
from xborder_insight.pipeline.extract import (
    ExtractStats,
    extract_one,
    parse_payload,
)
from xborder_insight.schemas import Review


def make_review(**overrides) -> Review:
    payload = {
        "review_id": "wc-001",
        "asin": "B0WCHG001",
        "rating": 2,
        "title": "Painfully slow",
        "body": "Takes forever to charge my phone.",
        "lang": "en",
    }
    payload.update(overrides)
    return Review.model_validate(payload)


GOOD = {
    "review_id": "wc-001",
    "is_relevant": True,
    "points": [
        {
            "label": "charging_speed",
            "polarity": "negative",
            "evidence": "Takes forever to charge my phone.",
            "confidence": 0.9,
        }
    ],
}


# ---------- 1. 格式漂移：能救则救 ----------


def test_parses_bare_json():
    result = parse_payload(json.dumps(GOOD), "wc-001")
    assert result.review_id == "wc-001"
    assert result.points[0].label == "charging_speed"


def test_parses_code_fenced_json():
    raw = "```json\n" + json.dumps(GOOD) + "\n```"
    assert parse_payload(raw, "wc-001").points[0].label == "charging_speed"


def test_parses_json_buried_in_prose():
    raw = "分析结果如下：\n" + json.dumps(GOOD) + "\n以上。"
    assert parse_payload(raw, "wc-001").points[0].label == "charging_speed"


def test_parses_top_level_array():
    raw = json.dumps([GOOD])
    assert parse_payload(raw, "wc-001").points[0].label == "charging_speed"


def test_missing_review_id_falls_back_to_argument():
    payload = dict(GOOD)
    payload.pop("review_id")
    assert parse_payload(json.dumps(payload), "wc-999").review_id == "wc-999"


def test_no_json_at_all_raises():
    with pytest.raises(ValueError, match="JSON"):
        parse_payload("抱歉，我无法完成该任务。", "wc-001")


# ---------- 2. 语义越界：必须拦住 ----------


def test_label_outside_taxonomy_rejected():
    payload = {
        "review_id": "wc-001",
        "points": [{"label": "battery_swelling", "evidence": "pad swelled up"}],
    }
    with pytest.raises(ValidationError, match="闭集"):
        parse_payload(json.dumps(payload), "wc-001")


def test_extra_field_rejected():
    """模型多吐一个 reasoning 字段也算违约——否则下游会拿到未声明的数据。"""
    payload = dict(GOOD)
    payload["reasoning"] = "用户明显不满"
    with pytest.raises(ValidationError):
        parse_payload(json.dumps(payload), "wc-001")


def test_confidence_out_of_range_rejected():
    payload = {
        "review_id": "wc-001",
        "points": [{"label": "heat", "evidence": "hot", "confidence": 1.4}],
    }
    with pytest.raises(ValidationError):
        parse_payload(json.dumps(payload), "wc-001")


def test_duplicate_labels_deduped():
    payload = {
        "review_id": "wc-001",
        "points": [
            {"label": "heat", "evidence": "hot to the touch", "confidence": 0.9},
            {"label": "heat", "evidence": "gets warm", "confidence": 0.7},
        ],
    }
    result = parse_payload(json.dumps(payload), "wc-001")
    assert len(result.points) == 1
    assert result.points[0].confidence == 0.9  # 保留首个，顺序稳定


# ---------- 3. 三级降级：坏输出不拖垮整批 ----------


class ScriptedClient:
    """按脚本返回固定内容，第 n 次调用取第 n 条，用尽后重复最后一条。"""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls = 0

    async def complete(self, system: str, user: str) -> LLMResponse:
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return LLMResponse(content=reply, prompt_tokens=10, completion_tokens=5)

    async def aclose(self) -> None:
        return None


def _run_extract(client: ScriptedClient, max_repair: int = 1):
    async def scenario():
        stats = ExtractStats(total=1)
        sem = asyncio.Semaphore(2)
        outcome = await extract_one(client, make_review(), stats, sem, max_repair)
        return outcome, stats

    return asyncio.run(scenario())


def test_repair_round_recovers_bad_output():
    """第 2 级：把校验错误回灌给模型，第二次返回合法则视为成功。"""
    client = ScriptedClient(["这不是 JSON", json.dumps(GOOD)])
    outcome, stats = _run_extract(client)

    assert outcome.extraction is not None
    assert outcome.attempts == 2
    assert stats.repair_attempts == 1


def test_repair_round_can_fix_taxonomy_violation():
    bad = json.dumps(
        {"review_id": "wc-001", "points": [{"label": "made_up", "evidence": "x"}]}
    )
    client = ScriptedClient([bad, json.dumps(GOOD)])
    outcome, _ = _run_extract(client)

    assert outcome.extraction is not None
    assert outcome.extraction.points[0].label == "charging_speed"


def test_persistent_failure_marks_unparsed_not_crash():
    """第 3 级：仍失败则标记 unparsed 并保留错误原因，供评测计入分母。"""
    client = ScriptedClient(["坏输出", "还是坏输出"])
    outcome, stats = _run_extract(client)

    assert outcome.extraction is None
    assert outcome.attempts == 2
    assert stats.repair_attempts == 1
    assert outcome.error and "ValueError" in outcome.error


def test_no_repair_budget_still_terminates():
    client = ScriptedClient(["坏输出"])
    outcome, stats = _run_extract(client, max_repair=0)

    assert outcome.extraction is None
    assert outcome.attempts == 1
    assert stats.repair_attempts == 0
    assert client.calls == 1
