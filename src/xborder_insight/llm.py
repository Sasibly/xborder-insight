"""LLM 客户端：统一接口 + 两种实现（离线规则 / 在线百炼）。

设计要点：
1. 接口只暴露「拿到一段文本 + token 用量」，解析与校验放在上层，
   这样 mock 与真实模型走完全相同的下游链路，基线才可比。
2. 在线实现负责网络层重试（超时/5xx/429），业务层重试（格式不符）在上层。
   两层重试职责分离，避免指数级放大调用量。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .config import Settings
from .errors import UpstreamError
from .logging import log_event
from .taxonomy import LABEL_HINTS, NEGATIVE_CUES, POSITIVE_CUES

import logging

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class LLMClient(Protocol):
    name: str

    async def complete(self, system: str, user: str) -> LLMResponse: ...

    async def aclose(self) -> None: ...


class MockLLMClient:
    """关键词规则抽取，完全离线。

    它同时承担两个角色：
    - 让项目在没有 API Key 时也能跑通全链路
    - 作为抽取质量的下限基线，与真实模型对比
    """

    name = "mock-rules"

    async def complete(self, system: str, user: str) -> LLMResponse:
        start = time.perf_counter()
        review_id = _field(user, "REVIEW_ID") or "unknown"
        rating_raw = _field(user, "RATING")
        body = _field(user, "BODY") or ""
        title = _field(user, "TITLE") or ""
        text = f"{title}. {body}"
        lower = text.lower()

        rating_raw = _field(user, "RATING")
        rating = int(rating_raw) if rating_raw and rating_raw.isdigit() else 3

        points: list[dict[str, object]] = []
        for label, hints in LABEL_HINTS.items():
            matched = [h for h in hints if h in lower]
            if not matched:
                continue
            sentence = _best_sentence(body or title, matched)
            polarity = _polarity(sentence, rating)
            confidence = min(0.95, 0.62 + 0.09 * len(matched))
            points.append(
                {
                    "label": label,
                    "polarity": polarity,
                    "evidence": sentence[:200],
                    "confidence": round(confidence, 2),
                }
            )

        if not points and rating >= 4:
            payload = {"review_id": review_id, "is_relevant": False, "points": []}
        else:
            payload = {"review_id": review_id, "is_relevant": bool(points), "points": points}

        # 模拟一点网络延迟，让并发调度的效果在本地可见
        await asyncio.sleep(0.01)
        return LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=max(1, len(user) // 4),
            completion_tokens=max(1, len(json.dumps(payload, ensure_ascii=False)) // 4),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )

    async def aclose(self) -> None:
        return None


class DashScopeClient:
    """百炼 OpenAI 兼容协议客户端。"""

    name = "dashscope"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def complete(self, system: str, user: str) -> LLMResponse:
        payload: dict[str, object] = {
            "model": self._settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        start = time.perf_counter()
        last_error: Exception | None = None

        for attempt in range(self._settings.llm_max_retries + 1):
            try:
                response = await self._client.post(
                    f"{self._settings.llm_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
                    json=payload,
                    timeout=self._settings.llm_timeout,
                )
                if response.status_code == 400 and "response_format" in response.text:
                    # 部分模型不支持 JSON 模式，降级为纯文本约束后重试
                    log_event(logger, "llm.response_format_unsupported")
                    payload.pop("response_format", None)
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    raise UpstreamError(
                        f"LLM 上游返回 {response.status_code}",
                        detail=response.text[:300],
                    )
                response.raise_for_status()
                data = response.json()
                choice = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                return LLMResponse(
                    content=choice,
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    latency_ms=int((time.perf_counter() - start) * 1000),
                )
            except (httpx.TimeoutException, httpx.TransportError, UpstreamError) as exc:
                last_error = exc
                if attempt >= self._settings.llm_max_retries:
                    break
                backoff = 0.6 * (2**attempt)
                log_event(logger, "llm.retry", attempt=attempt + 1, backoff_s=backoff, err=str(exc))
                await asyncio.sleep(backoff)

        raise UpstreamError("LLM 调用失败（已重试）", detail=str(last_error))

    async def aclose(self) -> None:
        await self._client.aclose()


def _field(text: str, key: str) -> str | None:
    match = re.search(rf"^{key}:\s*(.*)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _best_sentence(text: str, hints: list[str]) -> str:
    sentences = [s.strip() for s in re.split(r"[.。!！?？;\n]+", text) if s.strip()]
    for sentence in sentences:
        low = sentence.lower()
        if any(h in low for h in hints):
            return sentence
    return text[:200]


def _polarity(sentence: str, rating: int) -> str:
    """句内情绪词优先，词表无信号时用星级兜底。

    单看词表会把 "Takes forever"、"does not seal" 判成中性，从而漏掉真实痛点；
    而低星评论里的抱怨本身就是负面证据。这是规则基线必须补的一环。
    """
    low = sentence.lower()
    has_neg = any(c in low for c in NEGATIVE_CUES)
    has_pos = any(c in low for c in POSITIVE_CUES)
    if has_neg and not has_pos:
        return "negative"
    if has_pos and not has_neg:
        return "positive"
    if has_neg and has_pos:
        # 褒贬同句时按负面处理：抱怨通常才是改进信号
        return "negative"
    if rating <= 2:
        return "negative"
    if rating >= 4:
        return "positive"
    return "neutral"


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.is_mock:
        return MockLLMClient()
    client = httpx.AsyncClient()
    return DashScopeClient(settings, client)
