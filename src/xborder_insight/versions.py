"""版本三元组：`prompt_version / model / taxonomy_version`。

为什么必须记录三元组而不是只记 model：
    结果变了，可能不是模型换了，而是提示词改了、标签体系加了。只记 model 时，
    两次结果不一致永远查不出原因——这是「可复现」最常失效的地方。

为什么版本号是内容哈希而不是手写：
    手写的 "v1.2" 一定会在某次改动后忘记升。忘了升的版本号比没有版本号更糟，
    因为它看起来是可信的。哈希由内容决定，改了必然变，不需要谁记得。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import settings
from .pipeline.extract import PROMPT_VERSION
from .taxonomy import TAXONOMY_VERSION


@dataclass(frozen=True)
class VersionTriple:
    prompt_version: str
    model: str
    taxonomy_version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "prompt_version": self.prompt_version,
            "model": self.model,
            "taxonomy_version": self.taxonomy_version,
        }

    def describe(self) -> str:
        return f"{self.model}/{self.prompt_version}/{self.taxonomy_version}"


def current(model: str | None = None) -> VersionTriple:
    """当前代码对应的三元组。model 默认取配置，mock 模式下也如实记为配置值。"""
    return VersionTriple(
        prompt_version=PROMPT_VERSION,
        model=model or settings.llm_model,
        taxonomy_version=TAXONOMY_VERSION,
    )
