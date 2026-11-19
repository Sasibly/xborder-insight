"""聚类评测的守门测试。

这些用例守的不是「代码能跑」，而是三个已经被数据确认过的结论。
任何一条被改回去，这里必须红——否则「阈值为什么是这个数」会退化成一句传说。

    A. hash 向量没有语义结构（同主题对的相似度并不高于不同主题对）
    B. 极性必须作为分组键（43 对跨极性样本，同主题 0 对）
    C. 生产默认阈值必须落在标定平台区（手改阈值要能说清代价）

标注集只有 51 条 evidence / 120 对，噪声不小，所以断言用的是「区间」和「相对比较」，
不是精确数值。测试的目的是防退化，不是把一次实验的结论焊死。
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


def _load_calibrator():
    """按路径加载标定脚本。

    不 import 成模块名，是因为 scripts/ 不在包内；用模块名加载会污染 sys.modules
    并且依赖当时的工作目录。按文件路径加载最稳，也不影响其他测试。
    """
    spec = importlib.util.spec_from_file_location("calibrate_cluster_under_test", CALIBRATE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cal = _load_calibrator()


@pytest.fixture(scope="module")
def data():
    evidence, pairs = cal.load_data()
    return evidence, pairs


@pytest.fixture(scope="module")
def hash_vectors(data):
    evidence, _ = data
    return cal.get_vectors("hash", [row["evidence"] for row in evidence], no_cache=False)


# ------------------------------------------------------------------ A


def test_calibrator_hash_matches_production_embedder():
    """标定脚本里复制的 hash 实现必须与生产逐位一致。

    两处实现是有意分开的：生产侧可以改，标定侧要能给出「改坏了」的证据。
    但分开的前提是此刻一致——不一致的话，标定结论描述的是一个不存在的系统。
    """
    from xborder_insight.pipeline.cluster import HashEmbedder

    texts = ["充电慢还发热", "charges slowly and gets hot", "noise from the pump", "x"]
    expected = HashEmbedder(dim=512).encode(texts)
    actual = cal._hash_encode(texts, dim=512)
    assert np.allclose(expected, actual, atol=1e-6), "标定用的 hash 实现已与生产偏离"


def test_hash_embedder_has_no_semantic_structure(data, hash_vectors):
    """hash 向量下，同主题对与不同主题对的相似度分布几乎重合。

    这不是「效果差一点」，而是「没有可用的排序结构」：两个分布的中位数差不到 0.02，
    且不同主题反而略高。任何单阈值在这个向量上都只是近似随机切分。
    实测这条证据后，hash 的定位就明确为「离线可跑的占位实现」，不再是可上线方案。
    """
    evidence, pairs = data
    sep = cal.separation(evidence, pairs, hash_vectors)
    gap = sep["same"]["p50"] - sep["diff"]["p50"]
    assert abs(gap) < 0.10, (
        f"hash 的同/异主题中位相似度差 {gap:+.3f}，"
        "如果这个值明显变大，说明 hash 实现被改成了真正的语义向量，需要重新标定"
    )
    assert sep["separation_at_p10_p90"] < 0, "同主题 P10 高于不同主题 P90，与既有结论不符"


# ------------------------------------------------------------------ B


def test_polarity_grouping_is_mandatory(data, hash_vectors):
    """把极性加进分组键，ARI 必须有数量级的提升。

    这是本次标定最强的单点结论：同一批向量、同一阈值，只是分组键多了 polarity，
    ARI 从 0.11 量级升到 0.53 量级（hash）／0.20 → 0.77（dashscope）。
    如果哪天有人「简化」了 analyze.py 的分组逻辑，这条会立刻红。
    """
    evidence, pairs = data
    only_label = cal.evaluate(evidence, pairs, hash_vectors, "label", 0.12)
    with_polarity = cal.evaluate(evidence, pairs, hash_vectors, "label+polarity", 0.12)

    assert with_polarity["ari"] > only_label["ari"] + 0.20, (
        f"极性分组没有带来预期收益：{only_label['ari']:.3f} → {with_polarity['ari']:.3f}"
    )
    assert only_label["ari"] < 0.35, (
        f"仅按 label 分组的 ARI 达 {only_label['ari']:.3f}，"
        "与「标签内语义混杂」的既有结论不符，需要重新核查标注"
    )


def test_no_cross_polarity_pair_is_same_theme(data):
    """标注集里跨极性同主题的对必须是 0。

    正负极性混在一个机会点里，输出的是「既要保持又要改进」的建议，无法执行。
    这一条把业务判断固化下来：跨极性不存在「应该合并」的情形。
    """
    _, pairs = data
    cross = [p for p in pairs if p["a_polarity"] != p["b_polarity"]]
    assert len(cross) >= 30, "跨极性样本太少，这条结论不成立"
    assert not any(p["same"] for p in cross), (
        "存在被判为同主题的跨极性对：" + str([p["a"] for p in cross if p["same"]][:3])
    )


# ------------------------------------------------------------------ C


@pytest.mark.parametrize("provider", ["hash", "dashscope"])
def test_production_threshold_is_on_the_curve(data, provider):
    """生产默认阈值必须落在标定平台区内（ARI 不低于最优值 0.06 以上）。

    dashscope 的向量走缓存文件；没有缓存就跳过——在离线环境强行断言会变成
    「测试依赖网络」，比不测更糟。跳过时必须说清原因，不能静默通过。
    """
    from xborder_insight.config import CLUSTER_THRESHOLD_BY_PROVIDER

    evidence, pairs = data
    cache = ROOT / "data" / "eval" / f"vectors_{provider}.npy"
    if provider == "dashscope" and not cache.exists():
        pytest.skip("缺少 dashscope 向量缓存，先跑一次 scripts/calibrate_cluster.py")

    vectors = cal.get_vectors(provider, [row["evidence"] for row in evidence], no_cache=False)
    rows = cal.scan(evidence, pairs, vectors, "label+polarity", np.arange(0.10, 0.96, 0.02))
    best = cal.best_of(rows)

    chosen = CLUSTER_THRESHOLD_BY_PROVIDER[provider]
    hit = min(rows, key=lambda r: abs(r["threshold"] - chosen))
    assert hit["ari"] >= best["ari"] - 0.06, (
        f"{provider} 的默认阈值 {chosen} 不在平台区："
        f"ARI {hit['ari']:.3f} vs 最优 {best['ari']:.3f}（阈值 {best['threshold']}）"
    )


def test_analyze_outputs_never_mix_polarity(monkeypatch):
    """端到端守住分组键：真实管道输出里不能出现极性混合的主题。

    上面那条测的是标定脚本的评测函数，这条测的是 analyze.py 本身——
    如果有人把 grouped 的键改回只有 label，build_theme 拿不到 polarity 就会
    落回默认值 "mixed"，这里立刻红。两条缺一不可：一条守标定方法，一条守生产实现。
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
            f"主题 {theme.theme_id} 的极性是 {theme.polarity!r}，"
            "说明聚类分组键里丢了 polarity"
        )
    keys = [(t.label, t.polarity) for t in result.themes]
    assert len(keys) == len(set(keys)), f"同一 (label, polarity) 出现了多个主题：{keys}"


def test_calibration_assets_are_consistent():
    """标注产物内部自洽：pair 数与 evidence 条数、同异分布都要对得上。"""
    evidence_path = ROOT / "data" / "eval" / "cluster_evidence_live.jsonl"
    pairs_path = ROOT / "data" / "eval" / "cluster_pairs.jsonl"
    evidence = [json.loads(x) for x in evidence_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    pairs = [json.loads(x) for x in pairs_path.read_text(encoding="utf-8").splitlines() if x.strip()]

    by_label: dict[str, int] = {}
    for row in evidence:
        by_label[row["label"]] = by_label.get(row["label"], 0) + 1
    expected = sum(n * (n - 1) // 2 for n in by_label.values())
    assert len(pairs) == expected, f"pair 数 {len(pairs)} 与 evidence 结构应得的 {expected} 不符"

    same = sum(1 for p in pairs if p["same"])
    assert 40 <= same <= len(pairs) - 40, (
        f"同/异分布失衡（同 {same} / 总 {len(pairs)}），pairwise 指标会失真"
    )
