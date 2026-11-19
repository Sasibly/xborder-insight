"""语义聚类：把散落的证据短语归并成细分机会点。

为什么自己实现而不引入 sklearn / faiss：
数据量在千级以内，暴力余弦足够；且自实现让「相似度阈值」成为可解释、可调参的
业务杠杆——面试时能说清阈值怎么定，比说「我调了个 DBSCAN」有价值得多。

聚类发生在二级：一级是 taxonomy 标签（闭集），二级是同一标签下的语义子场景。
这样输出的不是「发热」这种笼统标签，而是「夏天车内暴晒后充电中断」这种可执行机会。
"""

from __future__ import annotations

import zlib

import numpy as np

from ..config import Settings
from ..errors import UpstreamError


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class HashEmbedder:
    """字符 n-gram 哈希向量，确定性、零依赖、可离线。

    它对中英混合短文本的相关性判断能力有限，但足以让聚类链路跑通，
    并在 README 中作为「可替换实现」被真实 embedding 替代。
    """

    def __init__(self, dim: int = 512, ngram_range: tuple[int, int] = (1, 3)) -> None:
        self.dim = dim
        self.ngram_range = ngram_range

    def encode(self, texts: list[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            tokens = self._ngrams(text)
            for token in tokens:
                idx = zlib.crc32(token.encode("utf-8")) % self.dim
                matrix[row, idx] += 1.0
        return _normalize(matrix)

    def _ngrams(self, text: str) -> list[str]:
        cleaned = "".join(ch.lower() for ch in text if not ch.isspace())
        out: list[str] = []
        for n in range(self.ngram_range[0], self.ngram_range[1] + 1):
            if len(cleaned) < n:
                continue
            out.extend(cleaned[i : i + n] for i in range(len(cleaned) - n + 1))
        return out


class DashScopeEmbedder:
    """真实语义向量。与 HashEmbedder 同接口，可无缝替换。"""

    def __init__(self, settings: Settings) -> None:
        import httpx

        self.dim = settings.embedding_dim
        self._model = settings.embedding_model
        self._client = httpx.Client(timeout=settings.llm_timeout)
        self._url = f"{settings.llm_base_url}/embeddings"
        self._headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        try:
            response = self._client.post(
                self._url,
                headers=self._headers,
                json={"model": self._model, "input": texts, "dimensions": self.dim},
            )
            response.raise_for_status()
        except Exception as exc:
            raise UpstreamError("Embedding 调用失败", detail=str(exc)) from exc
        vectors = [item["embedding"] for item in response.json()["data"]]
        return _normalize(np.asarray(vectors, dtype=np.float32))

    def aclose(self) -> None:
        self._client.close()


def build_embedder(settings: Settings):
    if settings.embedding_provider == "dashscope":
        return DashScopeEmbedder(settings)
    return HashEmbedder(settings.embedding_dim)


def greedy_cluster(vectors: np.ndarray, threshold: float) -> list[int]:
    """按序贪心合并：与任一簇质心相似度达标即并入，否则新建簇。

    复杂度 O(n·k)，n 为证据条数、k 为簇数，千级数据在毫秒量级。
    """
    if len(vectors) == 0:
        return []
    centroids: list[np.ndarray] = []
    counts: list[int] = []
    assignments: list[int] = []

    for vector in vectors:
        if centroids:
            sims = np.asarray([float(vector @ c) for c in centroids])
            best = int(np.argmax(sims))
            if sims[best] >= threshold:
                counts[best] += 1
                merged = (centroids[best] * (counts[best] - 1) + vector) / counts[best]
                norm = np.linalg.norm(merged) or 1.0
                centroids[best] = merged / norm
                assignments.append(best)
                continue
        centroids.append(vector.copy())
        counts.append(1)
        assignments.append(len(centroids) - 1)

    return assignments
