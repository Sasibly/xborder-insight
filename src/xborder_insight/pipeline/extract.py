"""结构化抽取：契约解析 + 三级降级 + 并发调度。

可靠性设计（本项目最核心的工程点）：
  第 1 级  正常调用 → 解析 → Pydantic 校验
  第 2 级  把校验错误回灌给模型，要求只修格式不改结论，重试一次
  第 3 级  仍失败则标记 unparsed，计入 parse_success_rate，绝不让整个任务崩掉

不静默丢弃失败样本，是因为「解析成功率」本身就是简历要写的指标。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field

from ..errors import UpstreamError
from ..llm import LLMClient
from ..schemas import Review, ReviewExtraction
from ..taxonomy import TAXONOMY

SYSTEM_PROMPT = (
    "你是跨境电商选品的评论分析师。只能从给定标签闭集里选择标签，"
    "不得自创标签，不得输出解释性文字，只输出 JSON。\n"
    f"标签闭集：{json.dumps(TAXONOMY, ensure_ascii=False)}\n"
    "输出结构："
    '{"review_id": str, "is_relevant": bool, '
    '"points": [{"label": str, "polarity": "negative|positive|neutral", '
    '"evidence": str, "confidence": number}]}'
    "\n规则：evidence 必须是评论原文中的片段，不得改写；一条评论最多 3 个标签；"
    "没有可归类的痛点时 points 返回空数组。"
)

# 提示词版本同样是内容指纹。改一个字（哪怕只是加个空格）版本就变，
# 所以「用同一版本重跑得到同一结果」这句话可以被真正验证，而不依赖人记得改号。
PROMPT_VERSION = "prompt-" + hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


@dataclass
class ExtractStats:
    total: int = 0
    parsed: int = 0
    unparsed: int = 0
    repair_attempts: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 单次调用延迟（毫秒）。留着算 P95，不在这里聚合，避免不同口径混在一起
    latencies_ms: list[int] = field(default_factory=list)

    @property
    def parse_success_rate(self) -> float:
        return self.parsed / self.total if self.total else 0.0


@dataclass
class ExtractOutcome:
    review_id: str
    extraction: ReviewExtraction | None
    error: str | None = None
    attempts: int = 1
    points: list = field(default_factory=list)


def build_user_prompt(review: Review) -> str:
    return (
        f"REVIEW_ID: {review.review_id}\n"
        f"RATING: {review.rating}\n"
        f"LANG: {review.lang}\n"
        f"TITLE: {review.title}\n"
        f"BODY: {review.body}"
    )


def parse_payload(raw: str, review_id: str) -> ReviewExtraction:
    """把模型返回的文本变成受控对象。任何一步不符预期都抛 ValueError。"""
    text = raw.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        fenced = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        else:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise ValueError("响应中找不到 JSON 对象")
            text = text[start : end + 1]
        data = json.loads(text)

    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("顶层结构不是对象")

    data["review_id"] = str(data.get("review_id") or review_id)
    data.setdefault("is_relevant", True)
    data.setdefault("points", [])
    return ReviewExtraction.model_validate(data)


async def extract_one(
    client: LLMClient,
    review: Review,
    stats: ExtractStats,
    sem: asyncio.Semaphore,
    max_repair: int = 1,
) -> ExtractOutcome:
    user = build_user_prompt(review)
    last_error: str | None = None
    attempts = 0

    async with sem:
        for round_idx in range(max_repair + 1):
            attempts += 1
            prompt = user
            if round_idx > 0:
                stats.repair_attempts += 1
                prompt = (
                    f"{user}\n\n上一次输出不满足契约，错误：{last_error}\n"
                    "只修正 JSON 结构或标签取值，不要改变判断结论，重新输出 JSON。"
                )
            try:
                response = await client.complete(SYSTEM_PROMPT, prompt)
                stats.llm_calls += 1
                stats.prompt_tokens += response.prompt_tokens
                stats.completion_tokens += response.completion_tokens
                stats.latencies_ms.append(response.latency_ms)
                extraction = parse_payload(response.content, review.review_id)
                return ExtractOutcome(
                    review_id=review.review_id,
                    extraction=extraction,
                    attempts=attempts,
                    points=extraction.points,
                )
            except UpstreamError as exc:
                return ExtractOutcome(review.review_id, None, f"upstream: {exc.message}", attempts)
            except Exception as exc:  # 解析或校验失败，进入修复轮
                last_error = f"{type(exc).__name__}: {exc}"[:300]

    return ExtractOutcome(review.review_id, None, last_error, attempts)


async def extract_many(
    client: LLMClient,
    reviews: list[Review],
    concurrency: int = 6,
    max_repair: int = 1,
) -> tuple[list[ExtractOutcome], ExtractStats]:
    stats = ExtractStats(total=len(reviews))
    sem = asyncio.Semaphore(concurrency)
    outcomes = await asyncio.gather(
        *(extract_one(client, r, stats, sem, max_repair) for r in reviews)
    )
    result = list(outcomes)
    stats.parsed = sum(1 for o in result if o.extraction is not None)
    stats.unparsed = stats.total - stats.parsed
    return result, stats
