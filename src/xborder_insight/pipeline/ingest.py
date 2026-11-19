"""数据入口：载入、清洗、去重、语言识别。

首版数据为人工构造的合成样本，不来自任何平台抓取，规避合规风险。
真实接入时只需替换 load_reviews 的来源，下游保持不变。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..config import ROOT_DIR
from ..errors import InputInvalid
from ..schemas import Review

DATA_DIR = ROOT_DIR / "data" / "samples"

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _clean(text: str) -> str:
    text = _HTML_TAG.sub(" ", text)
    text = text.replace("\u200b", " ")
    return _WHITESPACE.sub(" ", text).strip()


def detect_lang(text: str) -> str:
    if not text:
        return "unknown"
    cjk = len(_CJK.findall(text))
    total = len(re.sub(r"\s", "", text)) or 1
    if cjk / total > 0.3:
        return "zh"
    return "en"


def load_reviews(dataset: str) -> list[Review]:
    path = DATA_DIR / f"{dataset}.jsonl"
    if not path.exists():
        available = sorted(p.stem for p in DATA_DIR.glob("*.jsonl"))
        raise InputInvalid(f"数据集不存在: {dataset}", detail=f"可用数据集: {available}")
    return parse_reviews(path.read_text(encoding="utf-8"))


def parse_reviews(raw: str) -> list[Review]:
    """解析 JSONL，逐行容错：坏行跳过而不是整批失败。"""
    reviews: list[Review] = []
    seen: set[str] = set()
    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputInvalid(f"第 {line_no} 行不是合法 JSON") from exc

        obj["title"] = _clean(str(obj.get("title", "")))
        obj["body"] = _clean(str(obj.get("body", "")))
        if not obj.get("body"):
            continue
        obj.setdefault("lang", detect_lang(obj["body"]))

        review = Review.model_validate(obj)
        fingerprint = hashlib.sha1(review.body.lower().encode("utf-8")).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        reviews.append(review)
    return reviews
