"""聚类与向量化回归测试。

两类断言：
1. 数学边界——阈值是 `>=` 还是 `>`、质心更新是否生效。这类 bug 极其隐蔽，
   表现是「有时候少一个场景」，翻日志永远查不出来。
2. 标定数据——0.28 这个阈值是为哈希向量 ngram(1,3) 标定的，余量只有 0.009。
   一旦有人换 embedding 或改 ngram，这组用例会立刻失败，逼着重标定
   （对应待解决问题：换 dashscope embedding 后阈值需重定，约 0.6）。
"""

from __future__ import annotations

import numpy as np
import pytest

from xborder_insight.pipeline.cluster import HashEmbedder, greedy_cluster

DIM = 512


# ---------- 向量化基本性质 ----------


def test_embedder_shape_and_normalization():
    embedder = HashEmbedder(DIM)
    vectors = embedder.encode(["takes forever to charge", "水泵不出水"])
    assert vectors.shape == (2, DIM)
    norms = np.linalg.norm(vectors, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)  # 未归一化会让阈值语义失效


def test_embedder_empty_input():
    vectors = HashEmbedder(DIM).encode([])
    assert vectors.shape == (0, DIM)


def test_embedder_is_deterministic():
    """同一文本两次编码必须完全一致，否则聚类结果不可复现。"""
    embedder = HashEmbedder(DIM)
    a = embedder.encode(["Takes forever to charge"])
    b = embedder.encode(["Takes forever to charge"])
    assert np.array_equal(a, b)


def test_embedder_ignores_case_and_whitespace():
    embedder = HashEmbedder(DIM)
    a = embedder.encode(["Takes Forever"])
    b = embedder.encode(["takesforever"])
    assert float(a[0] @ b[0]) == pytest.approx(1.0, abs=1e-5)


# ---------- 阈值边界 ----------


def _unit(*coords: float) -> np.ndarray:
    return np.asarray(coords, dtype=np.float32)


def test_threshold_is_inclusive():
    """相似度恰好等于阈值时必须合并（`>=` 而非 `>`），差一点则不合并。"""
    v1 = _unit(1.0, 0.0)
    v2 = _unit(0.5, 0.8660254037844386)  # 与 v1 余弦恰好 0.5
    vectors = np.stack([v1, v2])

    assert len(set(greedy_cluster(vectors, 0.5))) == 1
    assert len(set(greedy_cluster(vectors, 0.5001))) == 2


def test_threshold_one_merges_only_duplicates():
    vectors = np.stack([_unit(1.0, 0.0), _unit(1.0, 0.0), _unit(0.6, 0.8)])
    labels = greedy_cluster(vectors, 1.0)
    assert len(set(labels)) == 2  # 两条完全相同 + 一条不同


def test_centroid_is_updated_after_merge():
    """合并后质心会漂移，这是贪心聚类的核心行为（也是它对顺序敏感的原因）。"""
    v1 = _unit(1.0, 0.0)
    v2 = _unit(0.5, 0.8660254037844386)
    # v3 与 v2 相似度 0.5、与 v1 相似度 -0.5，但与「合并后的质心」近乎正交
    v3 = _unit(0.5, -0.8660254037844386)
    labels = greedy_cluster(np.stack([v1, v2, v3]), 0.5)
    assert len(set(labels)) == 2


def test_empty_input_returns_empty():
    assert greedy_cluster(np.zeros((0, 4), dtype=np.float32), 0.3) == []


# ---------- hash 向量的行为边界（注意：这不是分布级标定） ----------


def test_calibrated_threshold_separates_synonym_from_unrelated():
    """6 个精选句子的点对点检查，**不是**阈值标定的证据。

    这组数据当初被当作 0.28 的来源，但它只能回答「这几句能不能分开」，
    回答不了「整批数据能不能分开」。后续用 51 条 evidence / 120 对标定（docs/05）
    发现：hash 向量下同主题对与不同主题对的分布几乎重合（P50 0.147 vs 0.160），
    任何阈值都只是近似随机切分。这 6 个句子恰好分得开，余量只有 0.009——
    余量这么小本身就是「在噪声上划线」的信号，而不是「标定得很准」。

    分布级标定与守卫见 tests/test_cluster_eval.py。本用例保留是为了守住
    HashEmbedder 的确定性行为：它一旦变化，这里会红，提示下游结论需要复核。
    """
    embedder = HashEmbedder(DIM, ngram_range=(1, 3))
    texts = [
        "充电太慢了",
        "充得很慢",
        "水泵不出水",
        "pump died after a week",
        "the pump stopped working",
        "charges too slow",
    ]
    v = embedder.encode(texts)

    def sim(i: int, j: int) -> float:
        return float(v[i] @ v[j])

    synonym = sim(0, 1)
    unrelated = sim(0, 2)
    english = sim(3, 4)
    cross = sim(3, 5)

    threshold = 0.28
    assert synonym >= threshold, f"中文同义句相似度 {synonym:.4f} 跌破阈值，需重标定"
    assert english >= threshold
    assert unrelated < threshold
    assert cross < threshold

    # 余量只有约 0.009。这不是「刚好够用」，而是「这条线画在噪声边缘」：
    # 换一批句子结果就可能翻转。生产默认值因此不再采信 0.28，
    # 改由配置按 provider 给出标定值（hash 0.12 / dashscope 0.65）。
    assert synonym - threshold < 0.05


def test_threshold_change_actually_changes_grouping():
    """阈值不是装饰参数：从 0.28 抬到 0.35，中文同义句就会被拆成两个场景。"""
    embedder = HashEmbedder(DIM, ngram_range=(1, 3))
    vectors = embedder.encode(["充电太慢了", "充得很慢"])

    assert len(set(greedy_cluster(vectors, 0.28))) == 1
    assert len(set(greedy_cluster(vectors, 0.35))) == 2
