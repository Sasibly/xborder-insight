"""聚类评测的守门测试（v2）。

这些用例守的是已经被数据确认过的结论。任何一条被改回去，这里必须红——
否则「阈值为什么是这个数」会退化成一句传说。

    A. hash 向量没有语义结构（同/异主题分布几乎重合）
    B. 极性必须在标注层隔离（跨极性同簇数必须为 0）
    C. 生产阈值必须落在标定平台区，且必须优于 0.65
    D. text-embedding-v4 不比 v3 好（换大模型不是解法）
    E. 标注资产内部自洽（覆盖完整、无重复、pair 数对得上）

口径说明：v2 在 216 条 evidence / 6 品类 / 354 对上标定，按生产口径
(dataset, label, polarity) 分组。标注粒度是「部件/环节级」（77 个主题簇），
不是 v1 的粗簇——粗簇下聚类只要按极性分两组就满分，评测对语义理解零区分力。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
CALIBRATE = ROOT / "scripts" / "calibrate_cluster.py"
THEMES_V2 = ROOT / "scripts" / "cluster_themes_v2.py"

# 标定用的 provider 名 -> 生产配置里的 embedding_provider 名
PROD_KEY = {"hash": "hash", "v3": "dashscope", "v4": "dashscope"}


def _load_module(path: Path, name: str):
    """按路径加载脚本。

    scripts/ 不在包内，用模块名加载会污染 sys.modules 且依赖工作目录。
    按文件路径加载最稳。
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cal = _load_module(CALIBRATE, "calibrate_cluster_under_test")
themes = _load_module(THEMES_V2, "cluster_themes_v2_under_test")


@pytest.fixture(scope="module")
def data():
    return cal.load_data()


@pytest.fixture(scope="module")
def vectors(data):
    evidence, _ = data
    cache = ROOT / "data" / "eval" / "vectors_v3.npy"
    if not cache.exists():
        pytest.skip("缺少 v3 向量缓存，先跑一次 scripts/calibrate_cluster.py --provider v3")
    return cal.get_vectors("v3", [row["evidence"] for row in evidence], no_cache=False)


def _curve(evidence, pairs, vec, step=0.02):
    grid = np.arange(0.10, 0.96, step)
    return [cal.evaluate(evidence, pairs, vec, float(t)) for t in grid]


# ------------------------------------------------------------------ A


def test_hash_embedder_matches_production():
    """标定脚本里复制的 hash 实现必须与生产逐位一致。

    两处实现是有意分开的（生产侧可改，标定侧要能给出「改坏了」的证据），
    但分开的前提是此刻一致——不一致的话，标定结论描述的是一个不存在的系统。
    """
    from xborder_insight.pipeline.cluster import HashEmbedder

    texts = ["充电慢还发热", "charges slowly and gets hot", "noise from the pump", "x"]
    expected = HashEmbedder(dim=512).encode(texts)
    actual = cal._hash_encode(texts, dim=512)
    assert np.allclose(expected, actual, atol=1e-6), "标定用的 hash 实现已与生产偏离"


def test_hash_has_no_semantic_structure(data):
    """hash 向量下同/异主题的相似度分布几乎重合，说明它没有可用的排序结构。

    它的定位是「离线可跑的占位实现」，不是可上线方案。
    """
    evidence, pairs = data
    vec = cal.get_vectors("hash", [r["evidence"] for r in evidence], no_cache=False)
    sep = cal.separation(pairs, vec, evidence)
    gap = sep["same"]["p50"] - sep["diff"]["p50"]
    assert abs(gap) < 0.12, (
        f"hash 的同/异主题中位相似度差 {gap:+.3f}，明显变大意味着 hash 被换成了真正的"
        "语义向量，需要重新标定并更新这里的预期"
    )
    assert sep["separation_at_p10_p90"] < 0, "同主题 P10 高于不同主题 P90，与既有结论不符"


# ------------------------------------------------------------------ B


def test_no_cross_polarity_theme_in_evidence():
    """标注层不能出现跨极性同簇。

    正负混在一个机会点里，输出的是「既要保持又要改进」的建议，无法执行。
    这条在标注层守住，pairs 就不可能出现跨极性同主题对。
    """
    evidence = themes.load_evidence()
    pol: dict[tuple[str, str], set[str]] = {}
    for row in evidence:
        pol.setdefault((row["label"], row["theme"]), set()).add(row["polarity"])
    bad = {k: v for k, v in pol.items() if len(v) > 1}
    assert not bad, f"存在跨极性同簇：{list(bad)[:3]}"


def test_pairs_never_contain_cross_polarity(data):
    """pairs 里不该出现跨极性对——极性已进分组键，它们永远不会在同一组相遇。

    若出现，说明分组键丢了 polarity，生产会把「充电快」和「充电慢」聚成一个主题。
    """
    _, pairs = data
    cross = [p for p in pairs if p["a_polarity"] != p["b_polarity"]]
    assert not cross, f"pairs 含 {len(cross)} 个跨极性对，分组键可能丢了 polarity"


def test_analyze_outputs_never_mix_polarity(monkeypatch):
    """端到端守住分组键：真实管道输出里不能出现极性混合的主题。

    上面几条测标注与标定脚本，这条测 analyze.py 本身。
    """
    import asyncio

    from xborder_insight.config import load_settings
    from xborder_insight.llm import build_llm_client
    from xborder_insight.pipeline.analyze import analyze_reviews
    from xborder_insight.pipeline.ingest import load_reviews

    monkeypatch.setenv("RUN_MODE", "mock")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    settings = load_settings()
    assert settings.is_mock, "这条用例必须跑在 mock 下，否则会真的调用外部模型"

    reviews = load_reviews("wireless_charger")
    client = build_llm_client(settings)
    result = asyncio.run(analyze_reviews(reviews, settings, client, "wireless_charger"))

    assert result.themes, "没有产出任何主题，管道可能提前返回"
    for theme in result.themes:
        assert theme.polarity in {"positive", "negative", "neutral"}, (
            f"主题 {theme.theme_id} 的极性是 {theme.polarity!r}，说明分组键里丢了 polarity"
        )
    keys = [(t.label, t.polarity) for t in result.themes]
    assert len(keys) == len(set(keys)), f"同一 (label, polarity) 出现了多个主题：{keys}"


# ------------------------------------------------------------------ C


def test_production_threshold_beats_legacy(data, vectors):
    """生产阈值必须显著优于旧的 0.65。

    这是本次重标定的核心结论：0.65 在 6 品类数据上 ARI 0.24，0.72 是 0.357。
    旧值是在 2 品类 / 120 对上标定的，跨品类后同 label 内语义多样性大增，
    最优切点右移。如果哪天有人把配置改回 0.65，这条会立刻红。
    """
    from xborder_insight.config import CLUSTER_THRESHOLD_BY_PROVIDER

    evidence, pairs = data
    shown = CLUSTER_THRESHOLD_BY_PROVIDER["dashscope"]
    at_shown = cal.evaluate(evidence, pairs, vectors, shown)
    at_legacy = cal.evaluate(evidence, pairs, vectors, 0.65)

    assert at_shown["ari"] >= at_legacy["ari"] + 0.05, (
        f"当前阈值 {shown} 的 ARI {at_shown['ari']:.3f} 未明显优于 0.65 的 "
        f"{at_legacy['ari']:.3f}，重标定的结论可能已失效"
    )


def test_production_threshold_is_on_plateau(data, vectors):
    """生产阈值必须落在 ARI 平台区内（不低于最优值 0.06 以上）。"""
    from xborder_insight.config import CLUSTER_THRESHOLD_BY_PROVIDER

    evidence, pairs = data
    rows = _curve(evidence, pairs, vectors)
    best = cal.best_of(rows)
    chosen = CLUSTER_THRESHOLD_BY_PROVIDER["dashscope"]
    hit = min(rows, key=lambda r: abs(r["threshold"] - chosen))

    assert hit["ari"] >= best["ari"] - 0.06, (
        f"默认阈值 {chosen} 不在平台区：ARI {hit['ari']:.3f} vs 最优 {best['ari']:.3f}"
        f"（阈值 {best['threshold']}）"
    )


# ------------------------------------------------------------------ D


def test_v4_is_not_better_than_v3(data):
    """text-embedding-v4 不比 v3 好——「换个更大的模型」不是这个问题的解法。

    实测分离度 v4 -0.137 vs v3 -0.104，v4 的相似度整体更窄、区分力更低。
    保留这条是为了让「为什么不换 v4」有据可查，而不是靠记忆。
    """
    evidence, pairs = data
    cache_v3 = ROOT / "data" / "eval" / "vectors_v3.npy"
    cache_v4 = ROOT / "data" / "eval" / "vectors_v4.npy"
    if not (cache_v3.exists() and cache_v4.exists()):
        pytest.skip("缺少 v3/v4 向量缓存")

    v3 = np.load(cache_v3)
    v4 = np.load(cache_v4)
    sep3 = cal.separation(pairs, v3, evidence)["separation_at_p10_p90"]
    sep4 = cal.separation(pairs, v4, evidence)["separation_at_p10_p90"]
    assert sep4 <= sep3 + 0.05, (
        f"v4 的分离度 {sep4:+.3f} 明显优于 v3 的 {sep3:+.3f}，"
        "结论已变，应重新评估是否切换默认向量模型"
    )


# ------------------------------------------------------------------ E


def test_theme_assignment_is_complete():
    """每条 evidence 恰好归属一个簇，且簇名在 THEME_SPEC 里。"""
    evidence = themes.load_evidence()
    problems = themes.validate(evidence)
    assert not problems, "标注结构有问题：" + "; ".join(problems[:3])


def test_pairs_match_group_combinations(data):
    """pair 数必须等于每组组内组合之和——不多不少。

    少了对说明有 evidence 被漏评；多了说明派生逻辑与分组不一致。
    """
    evidence, pairs = data
    by_group: dict[str, int] = {}
    for row in evidence:
        key = f"{row['dataset']}::{row['label']}::{row['polarity']}"
        by_group[key] = by_group.get(key, 0) + 1
    expected = sum(n * (n - 1) // 2 for n in by_group.values())
    assert len(pairs) == expected, f"pair 数 {len(pairs)} 与组内组合应得的 {expected} 不符"


def test_pair_label_distribution_is_balanced(data):
    """同/异主题比例不能失衡到让 pairwise 指标失真。"""
    _, pairs = data
    same = sum(1 for p in pairs if p["same"])
    ratio = same / len(pairs)
    assert 0.20 <= ratio <= 0.60, (
        f"正例占比 {ratio:.1%}（同 {same} / 总 {len(pairs)}），pairwise 指标会失真"
    )
