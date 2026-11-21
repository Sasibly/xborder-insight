"""聚类阈值标定（v2）：在生产分组口径下比较向量与阈值。

## v2 相对 v1 改了什么，为什么

v1 在 51 条 evidence / 2 品类上标定出 0.65。扩到 216 条 / 6 品类后，两个前提塌了：

1. **分组口径**。v1 按 label 分组（跨品类）。但生产 `analyze_reviews` 一次只处理
   一个 dataset，聚类天然限定在单品类内。按 label 跨品类分组会把「无线充充电慢」
   和「咖啡机出水慢」放进同一组——评的是一个不会发生的场景。
   v2 按 (dataset, label, polarity) 分组，与生产逐字一致。

2. **「多品类的 label 语义会漂移」**。同一 label 在不同品类下指不同物理量：
   charging_speed 在无线充上是「充电慢」，在咖啡机上变成「出水慢」，在扫地机上
   变成「吸力弱」。这是分类体系跨品类复用的固有问题，靠分组隔离开，不靠算法解决。

## 三个候选向量

    hash      字符 3-gram，离线占位实现
    v3@512    text-embedding-v3，v1 标定用的就是它
    v4@1024   text-embedding-v4

v4 实测比 v3 更差（同/异主题分离度 -0.241 vs -0.161），留着是为了让这个结论可复现。

## 指标

- **pairwise F1**：同主题即同簇，micro 平均。
- **ARI**：pairwise F1 对「全合并」有系统性偏袒——全合并时 recall 恒为 1.0，
  F1 取决于样本里正例占比。ARI 修正了随机期望，全合并会被判成接近 0。
  在细粒度标注下 ARI 是主指标，F1 只作参考。

用法：
    python scripts/calibrate_cluster.py
    python scripts/calibrate_cluster.py --provider v3 --no-cache
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EVIDENCE = ROOT / "data" / "eval" / "cluster_evidence.jsonl"
PAIRS = ROOT / "data" / "eval" / "cluster_pairs.jsonl"
CACHE_DIR = ROOT / "data" / "eval"
REPORT = ROOT / "reports" / "cluster_calibration.json"

PROVIDERS = {
    "hash": "离线 · 字符 3-gram",
    "v3": "text-embedding-v3 · 512 维",
    "v4": "text-embedding-v4 · 1024 维",
}


# ---------------------------------------------------------------- 向量


def _hash_encode(texts: list[str], dim: int = 512) -> np.ndarray:
    """与 pipeline/cluster.py 的 HashEmbedder 逐位等价。

    刻意复制一份：标定脚本要能在生产实现被改坏时仍给出可对照的尺子。
    """
    import zlib

    matrix = np.zeros((len(texts), dim), dtype=np.float32)
    for row, text in enumerate(texts):
        cleaned = "".join(ch.lower() for ch in text if not ch.isspace())
        for n in (1, 2, 3):
            if len(cleaned) < n:
                continue
            for i in range(len(cleaned) - n + 1):
                matrix[row, zlib.crc32(cleaned[i : i + n].encode()) % dim] += 1.0
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _api_encode(texts: list[str], model: str, dim: int, batch: int = 10) -> np.ndarray:
    import httpx

    key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        raise SystemExit("需要 DASHSCOPE_API_KEY 或 LLM_API_KEY")
    base = os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    out: list[list[float]] = []
    with httpx.Client(timeout=90) as client:
        for start in range(0, len(texts), batch):
            chunk = texts[start : start + batch]
            response = client.post(
                f"{base}/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "input": chunk, "dimensions": dim},
            )
            response.raise_for_status()
            data = response.json()["data"]
            data.sort(key=lambda d: d["index"])  # 官方接口按 index 标明顺序
            out.extend(item["embedding"] for item in data)
    matrix = np.asarray(out, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def get_vectors(provider: str, texts: list[str], no_cache: bool) -> np.ndarray:
    cache = CACHE_DIR / f"vectors_{provider}.npy"
    if cache.exists() and not no_cache:
        cached = np.load(cache)
        if cached.shape[0] == len(texts):
            print(f"  向量 {provider:6s} 用缓存 {cache.name} {cached.shape}")
            return cached
    print(f"  向量 {provider:6s} 现算（{len(texts)} 条）…")
    if provider == "hash":
        matrix = _hash_encode(texts)
    elif provider == "v3":
        matrix = _api_encode(texts, "text-embedding-v3", 512)
    else:
        matrix = _api_encode(texts, "text-embedding-v4", 1024)
    np.save(cache, matrix)
    print(f"  向量 {provider:6s} {matrix.shape} 已缓存")
    return matrix


# ---------------------------------------------------------------- 聚类


def greedy_cluster(vectors: np.ndarray, threshold: float) -> list[int]:
    """与 pipeline/cluster.py 一致的贪心合并（含增量质心更新）。"""
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


# ---------------------------------------------------------------- 指标


def _ari(gold: list[str], pred: list[str]) -> float:
    """调整兰德指数（无 sklearn 实现）。"""
    n = len(gold)
    if n < 2:
        return 1.0

    def _c2(x: int) -> float:
        return x * (x - 1) / 2.0

    from collections import Counter

    table = Counter(zip(gold, pred))
    row = Counter(gold)
    col = Counter(pred)
    sum_ij = sum(_c2(v) for v in table.values())
    sum_i = sum(_c2(v) for v in row.values())
    sum_j = sum(_c2(v) for v in col.values())
    total = _c2(n)
    expected = sum_i * sum_j / total if total else 0.0
    max_index = 0.5 * (sum_i + sum_j)
    if max_index == expected:
        return 1.0
    return (sum_ij - expected) / (max_index - expected)


def load_data():
    evidence = [
        json.loads(line)
        for line in EVIDENCE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pairs = [
        json.loads(line)
        for line in PAIRS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return evidence, pairs


def evaluate(evidence: list[dict], pairs: list[dict], vectors: np.ndarray, threshold: float) -> dict:
    """按 pairs 的 group 字段分组聚类，再统计。

    组的成员下标来自 pair 里出现的 evidence 序号，因此只有「至少产生一对」的
    组会进入评测——单条组派生不出 pair，也就无从评测（它们对 pair F1 也无贡献）。
    """
    group_members: dict[str, set[int]] = {}
    pair_indices: list[tuple[str, int, int, bool]] = []
    for pair in pairs:
        group_members.setdefault(pair["group"], set()).update({pair["a_idx"], pair["b_idx"]})
        pair_indices.append((pair["group"], pair["a_idx"], pair["b_idx"], pair["same"]))

    assign: dict[tuple[str, int], int] = {}
    pred_clusters_total = 0
    for group, members in group_members.items():
        first = next(p for p in pairs if p["group"] == group)
        label = first["label"]
        polarity = first["a_polarity"]
        global_idx = sorted(
            i
            for i, row in enumerate(evidence)
            if row["dataset"] == first["dataset"]
            and row["label"] == label
            and row["polarity"] == polarity
        )
        if len(global_idx) < 2:
            continue
        local = greedy_cluster(vectors[global_idx], threshold)
        pred_clusters_total += max(local) + 1
        for pos, g in enumerate(global_idx):
            assign[(group, pos)] = local[pos]

    tp = fp = fn = 0
    for group, a_idx, b_idx, same in pair_indices:
        pred_same = assign.get((group, a_idx)) == assign.get((group, b_idx))
        if pred_same and same:
            tp += 1
        elif pred_same and not same:
            fp += 1
        elif not pred_same and same:
            fn += 1

    ari_sum = 0.0
    ari_weight = 0
    gold_clusters = 0
    by_group: dict[str, list[dict]] = {}
    for pair in pairs:
        by_group.setdefault(pair["group"], []).append(pair)
    for group, items in by_group.items():
        members = sorted({i for p in items for i in (p["a_idx"], p["b_idx"])})
        if len(members) < 2:
            continue
        themes = {}
        for p in items:
            themes[p["a_idx"]] = p["a_theme"]
            themes[p["b_idx"]] = p["b_theme"]
        gold_local = [themes[i] for i in members]
        pred_local = [assign.get((group, members.index(i)), -1) for i in members]
        gold_clusters += len(set(gold_local))
        ari_sum += _ari(gold_local, [str(x) for x in pred_local]) * len(items)
        ari_weight += len(items)

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "threshold": round(threshold, 3),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "ari": round(ari_sum / ari_weight if ari_weight else 0.0, 4),
        "components": pred_clusters_total,
        "gold_components": gold_clusters,
    }


def separation(pairs: list[dict], vectors: np.ndarray, evidence: list[dict]) -> dict:
    """同主题对 vs 不同主题对的余弦分布——阈值该定在哪的原始依据。"""
    pos_of: dict[tuple[str, int], int] = {}
    for group in {p["group"] for p in pairs}:
        first = next(p for p in pairs if p["group"] == group)
        global_idx = sorted(
            i
            for i, row in enumerate(evidence)
            if row["dataset"] == first["dataset"]
            and row["label"] == first["label"]
            and row["polarity"] == first["a_polarity"]
        )
        for pos, gi in enumerate(global_idx):
            pos_of[(group, pos)] = gi

    same_sims, diff_sims = [], []
    for p in pairs:
        a = pos_of[(p["group"], p["a_idx"])]
        b = pos_of[(p["group"], p["b_idx"])]
        sim = float(vectors[a] @ vectors[b])
        (same_sims if p["same"] else diff_sims).append(sim)

    def q(values: list[float]) -> dict:
        if not values:
            return {}
        arr = np.asarray(values)
        return {
            "n": len(values),
            "p10": round(float(np.percentile(arr, 10)), 4),
            "p50": round(float(np.percentile(arr, 50)), 4),
            "p90": round(float(np.percentile(arr, 90)), 4),
        }

    overlap = None
    if same_sims and diff_sims:
        overlap = round(
            float(np.percentile(same_sims, 10)) - float(np.percentile(diff_sims, 90)), 4
        )
    return {"same": q(same_sims), "diff": q(diff_sims), "separation_at_p10_p90": overlap}


def best_of(rows: list[dict]) -> dict:
    """按 ARI 选阈值，F1 次键，阈值第三键（并列取高：宁可少合并）。"""
    return max(rows, key=lambda r: (r["ari"], r["f1"], r["threshold"]))


def plateau(rows: list[dict], best: dict, tol: float = 0.02) -> tuple[float, float] | None:
    """ARI 在最优值 tol 以内的连续阈值区间。区间宽度就是「能定到几位」的上限。"""
    by_t = sorted(rows, key=lambda r: r["threshold"])
    i = next(k for k, r in enumerate(by_t) if r["threshold"] == best["threshold"])
    lo = hi = best["threshold"]
    k = i
    while k > 0 and by_t[k - 1]["ari"] >= best["ari"] - tol:
        k -= 1
        lo = by_t[k]["threshold"]
    k = i
    while k < len(by_t) - 1 and by_t[k + 1]["ari"] >= best["ari"] - tol:
        k += 1
        hi = by_t[k]["threshold"]
    return (lo, hi)


def main() -> int:
    parser = argparse.ArgumentParser(description="聚类阈值标定 v2")
    parser.add_argument("--provider", default="all", choices=["hash", "v3", "v4", "all"])
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--step", type=float, default=0.02)
    args = parser.parse_args()

    evidence, pairs = load_data()
    texts = [row["evidence"] for row in evidence]
    same_n = sum(1 for p in pairs if p["same"])
    print(f"标注集：{len(evidence)} 条 evidence · {len(pairs)} 对"
          f"（同 {same_n} / 异 {len(pairs) - same_n}）· "
          f"{len({p['group'] for p in pairs})} 组")

    providers = list(PROVIDERS) if args.provider == "all" else [args.provider]
    grid = np.arange(0.10, 0.96, args.step)
    report: dict = {"evidence_count": len(evidence), "pair_count": len(pairs), "runs": {}}

    for provider in providers:
        try:
            vectors = get_vectors(provider, texts, args.no_cache)
        except Exception as exc:
            print(f"  !! {provider} 向量失败：{exc}")
            continue

        sep = separation(pairs, vectors, evidence)
        print(f"\n{'=' * 70}\n向量 {provider}（{PROVIDERS[provider]}）")
        print(f"  同主题   n={sep['same']['n']:3d}  P10 {sep['same']['p10']:.3f}  "
              f"P50 {sep['same']['p50']:.3f}  P90 {sep['same']['p90']:.3f}")
        print(f"  不同主题 n={sep['diff']['n']:3d}  P10 {sep['diff']['p10']:.3f}  "
              f"P50 {sep['diff']['p50']:.3f}  P90 {sep['diff']['p90']:.3f}")
        mark = "← 有重叠，单阈值不可能全对" if sep["separation_at_p10_p90"] < 0 else "← 无重叠"
        print(f"  分离度（同P10 - 异P90）{sep['separation_at_p10_p90']:+.3f}  {mark}")

        rows = [evaluate(evidence, pairs, vectors, float(t)) for t in grid]
        best = best_of(rows)
        band = plateau(rows, best)
        print(f"\n  最优阈值 {best['threshold']:.2f}  ARI {best['ari']:.3f}  "
              f"P {best['precision']:.3f}  R {best['recall']:.3f}  F1 {best['f1']:.3f}")
        print(f"  簇数 预测 {best['components']} vs gold {best['gold_components']}")
        if band:
            print(f"  ARI 平台区 [{band[0]:.2f}, {band[1]:.2f}]（宽 {band[1] - band[0]:.2f}）")

        marks = [0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
        cells = []
        for m in marks:
            hit = min(rows, key=lambda r: abs(r["threshold"] - m))
            cells.append(f"{m:.2f}:{hit['ari']:.2f}/{hit['f1']:.2f}")
        print("  阈值(ARI/F1): " + "  ".join(cells))

        report["runs"][provider] = {"separation": sep, "best": best, "plateau": band, "curve": rows}

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整曲线写入 {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
