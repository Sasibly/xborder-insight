"""聚类阈值标定：在同一份标注集上，比较 embedder 与分组键，扫出阈值。

为什么必须先有标注才能换 embedding：
「把阈值从 0.28 改到 0.6」这句话本身没有信息量——它没说清依据。
标注集把这件事变成一个可证伪的问题：给定同一个分区任务，
哪种向量、哪个阈值、哪种分组键，pairwise F1 最高。

三个变量，各自独立验证：
  向量    hash（字符 3-gram） / dashscope（text-embedding-v3）
  分组键  label / label+polarity
  阈值    0.10 ~ 0.95

口径：
- pairwise 判定，micro 平均。同簇 = 判为同一主题。
- 只在「组内至少 2 条」的组上算，跨组对不参与——生产里聚类就是组内独立的，
  把跨组对算进来等于评一个根本不会发生的场景。
- 向量结果缓存到 data/eval/vectors_<provider>.npy，标定反复跑不重复花钱。

用法：
    python scripts/calibrate_cluster.py                  # 两个向量都跑
    python scripts/calibrate_cluster.py --provider hash  # 只跑离线向量
    python scripts/calibrate_cluster.py --no-cache       # 强制重算向量
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

EVIDENCE = ROOT / "data" / "eval" / "cluster_evidence_live.jsonl"
PAIRS = ROOT / "data" / "eval" / "cluster_pairs.jsonl"
CACHE_DIR = ROOT / "data" / "eval"
REPORT = ROOT / "reports" / "cluster_calibration.json"


# ---------------------------------------------------------------- 向量


def _hash_encode(texts: list[str], dim: int = 512) -> np.ndarray:
    """与 pipeline/cluster.py 的 HashEmbedder 逐位等价，此处复制一份是为了
    标定脚本不依赖 Settings（避免为了跑实验去改环境变量）。"""
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


def _dashscope_encode(texts: list[str], dim: int = 512, batch: int = 10) -> np.ndarray:
    import httpx

    key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        raise SystemExit("需要 DASHSCOPE_API_KEY 或 LLM_API_KEY")
    base = os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model = os.environ.get("EMBEDDING_MODEL", "text-embedding-v3")

    out: list[list[float]] = []
    with httpx.Client(timeout=60) as client:
        for start in range(0, len(texts), batch):
            chunk = texts[start : start + batch]
            response = client.post(
                f"{base}/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "input": chunk, "dimensions": dim},
            )
            response.raise_for_status()
            data = response.json()["data"]
            # 官方接口按 index 字段标明顺序，不能依赖返回顺序
            data.sort(key=lambda d: d["index"])
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
            print(f"  向量 {provider:10s} 用缓存 {cache.name} {cached.shape}")
            return cached
    print(f"  向量 {provider:10s} 现算（{len(texts)} 条）…")
    matrix = _hash_encode(texts) if provider == "hash" else _dashscope_encode(texts)
    np.save(cache, matrix)
    print(f"  向量 {provider:10s} {matrix.shape} 已缓存")
    return matrix


# ---------------------------------------------------------------- 聚类


def greedy_cluster(vectors: np.ndarray, threshold: float) -> list[int]:
    """与 pipeline/cluster.py 完全一致的贪心合并（含增量更新质心）。

    刻意不 import 原函数：标定脚本要能在原函数被改坏时仍然给出可对照的结果，
    相当于给生产实现配了一把独立的尺子。
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


# ---------------------------------------------------------------- 评测


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


def build_groups(evidence: list[dict], key: str) -> dict[str, list[int]]:
    """key = 'label' | 'label+polarity'；值为 evidence 全局下标的列表。"""
    groups: dict[str, list[int]] = {}
    for idx, row in enumerate(evidence):
        gk = row["label"] if key == "label" else f"{row['label']}::{row['polarity']}"
        groups.setdefault(gk, []).append(idx)
    return groups


def _ari(gold: list[str], pred: list[str]) -> float:
    """调整兰德指数，无 sklearn 实现。

    为什么必须有它：pairwise F1 对「每题合并不管」有系统性偏袒——全部合并时
    recall 恒为 1.0，而 precision 只受样本里正对占比影响，F1 会稳定在一个不低的
    水平。ARI 修正了随机分区的期望，全合并会被明确判成接近 0。
    """
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


def _gold_partition(pairs: list[dict], size: int) -> list[str]:
    """同主题标注的连通分量。簇数就是「这段数据本来就该分成几个主题」。"""
    parent = list(range(size))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pair in pairs:
        if pair["same"]:
            ra, rb = find(pair["a_idx"]), find(pair["b_idx"])
            if ra != rb:
                parent[ra] = rb
    return [f"g{find(i)}" for i in range(size)]


def evaluate(
    evidence: list[dict],
    pairs: list[dict],
    vectors: np.ndarray,
    group_key: str,
    threshold: float,
) -> dict:
    groups = build_groups(evidence, group_key)
    # 全局下标 → 簇号（不同组的簇号天然不冲突，因为组内独立）
    assign: dict[int, str] = {}
    n_clusters = 0
    for gk, members in groups.items():
        if len(members) < 2:
            for m in members:
                assign[m] = f"{gk}#solo{m}"
            continue
        local = greedy_cluster(vectors[members], threshold)
        n_clusters += max(local) + 1
        for m, c in zip(members, local):
            assign[m] = f"{gk}#{c}"

    # gold 侧用 (label, 全局下标) 定位；标注是按组内顺序生成的，需还原
    label_pos: dict[str, list[int]] = {}
    for idx, row in enumerate(evidence):
        label_pos.setdefault(row["label"], []).append(idx)
    label_pairs: dict[str, list[dict]] = {}
    for pair in pairs:
        label_pairs.setdefault(pair["label"], []).append(pair)

    tp = fp = fn = 0
    for pair in pairs:
        gi = label_pos[pair["label"]][pair["a_idx"]]
        gj = label_pos[pair["label"]][pair["b_idx"]]
        pred_same = assign[gi] == assign[gj]
        if pred_same and pair["same"]:
            tp += 1
        elif pred_same and not pair["same"]:
            fp += 1
        elif not pred_same and pair["same"]:
            fn += 1

    # ARI 按 label 组独立算再按 pair 数加权：group_key=label+polarity 时，
    # pred 会比 gold 细（正负拆开），而 gold 里跨极性对全是「不同主题」，
    # 所以拆开是对的，ARI 会如实给分而不是惩罚它。
    ari_sum = 0.0
    ari_weight = 0
    gold_clusters = pred_clusters = 0
    for label, members_global in label_pos.items():
        if len(members_global) < 2:
            gold_clusters += 1
            pred_clusters += 1
            continue
        gold_local = _gold_partition(label_pairs.get(label, []), len(members_global))
        pred_local = [assign[m] for m in members_global]
        gold_clusters += len(set(gold_local))
        pred_clusters += len(set(pred_local))
        weight = len(label_pairs.get(label, []))
        ari_sum += _ari(gold_local, pred_local) * weight
        ari_weight += weight
    ari = ari_sum / ari_weight if ari_weight else 0.0

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "group_key": group_key,
        "threshold": round(threshold, 3),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "ari": round(ari, 4),
        "clusters": n_clusters,
        "gold_clusters": gold_clusters,
        "pred_clusters": pred_clusters,
    }


def separation(evidence: list[dict], pairs: list[dict], vectors: np.ndarray) -> dict:
    """同主题对 vs 不同主题对的余弦分布。这是「哪个阈值合理」的原始依据——
    阈值的最优位置就在两个分布的重叠区，F1 只是这件事的汇总。"""
    label_pos: dict[str, list[int]] = {}
    for idx, row in enumerate(evidence):
        label_pos.setdefault(row["label"], []).append(idx)

    same_sims, diff_sims = [], []
    for pair in pairs:
        gi = label_pos[pair["label"]][pair["a_idx"]]
        gj = label_pos[pair["label"]][pair["b_idx"]]
        sim = float(vectors[gi] @ vectors[gj])
        (same_sims if pair["same"] else diff_sims).append(sim)

    def q(values: list[float]) -> dict:
        if not values:
            return {}
        arr = np.asarray(values)
        return {
            "n": len(values),
            "p10": round(float(np.percentile(arr, 10)), 4),
            "p50": round(float(np.percentile(arr, 50)), 4),
            "p90": round(float(np.percentile(arr, 90)), 4),
            "min": round(float(arr.min()), 4),
            "max": round(float(arr.max()), 4),
        }

    # 重叠区：同主题的低分位与不同主题的高分位之差。为正是可分，为负是重叠
    overlap = None
    if same_sims and diff_sims:
        overlap = round(
            float(np.percentile(same_sims, 10)) - float(np.percentile(diff_sims, 90)), 4
        )
    return {"same": q(same_sims), "diff": q(diff_sims), "separation_at_p10_p90": overlap}


def scan(evidence, pairs, vectors, group_key: str, grid: np.ndarray) -> list[dict]:
    return [evaluate(evidence, pairs, vectors, group_key, float(t)) for t in grid]


def best_of(rows: list[dict]) -> dict:
    """按 ARI 选阈值，F1 只作次键。

    不用 F1 做主键的原因在数据里：全合并时 R=1.0，F1 能到 0.87，看起来很好，
    但那时「每个 label 组只出一个主题」——聚类等于没做。ARI 修正了随机分区的
    期望，全合并会被判成接近 0，这个假象就消失了。
    F1 作次键、阈值作第三键（并列取高）：宁可少合并也不错合并。
    """
    return max(rows, key=lambda r: (r["ari"], r["f1"], r["threshold"]))


def plateau(rows: list[dict], best: dict, tol: float = 0.02) -> tuple[float, float] | None:
    """ARI 在最优值 tol 以内的连续阈值区间。

    区间宽 = 阈值不敏感，选起来安全；区间窄 = 这个最优点很可能是标注噪声。
    """
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
    parser = argparse.ArgumentParser(description="聚类阈值标定")
    parser.add_argument("--provider", default="both", choices=["hash", "dashscope", "both"])
    parser.add_argument("--no-cache", action="store_true", help="强制重算向量")
    parser.add_argument("--step", type=float, default=0.02)
    args = parser.parse_args()

    evidence, pairs = load_data()
    texts = [row["evidence"] for row in evidence]
    print(f"标注集：{len(evidence)} 条 evidence · {len(pairs)} 对（同 "
          f"{sum(1 for p in pairs if p['same'])} / 异 {len(pairs) - sum(1 for p in pairs if p['same'])}）")

    providers = ["hash", "dashscope"] if args.provider == "both" else [args.provider]
    grid = np.arange(0.10, 0.96, args.step)

    report: dict = {"evidence_count": len(evidence), "pair_count": len(pairs), "runs": {}}

    for provider in providers:
        try:
            vectors = get_vectors(provider, texts, args.no_cache)
        except Exception as exc:  # 网络/额度问题不该让整个标定崩掉
            print(f"  !! {provider} 向量失败：{exc}")
            continue

        sep = separation(evidence, pairs, vectors)
        print(f"\n{'=' * 66}\n向量 {provider}")
        print(f"  同主题对   n={sep['same']['n']:3d}  P10 {sep['same']['p10']:.3f}  "
              f"P50 {sep['same']['p50']:.3f}  P90 {sep['same']['p90']:.3f}")
        print(f"  不同主题对 n={sep['diff']['n']:3d}  P10 {sep['diff']['p10']:.3f}  "
              f"P50 {sep['diff']['p50']:.3f}  P90 {sep['diff']['p90']:.3f}")
        print(f"  分离度(P10同 - P90异) {sep['separation_at_p10_p90']:+.3f}"
              f"{'  ← 有重叠，单阈值不可能全对' if sep['separation_at_p10_p90'] < 0 else '  ← 无重叠，可完美分开'}")

        for group_key in ("label", "label+polarity"):
            rows = scan(evidence, pairs, vectors, group_key, grid)
            best = best_of(rows)
            band = plateau(rows, best)
            print(f"\n  分组键 {group_key}")
            print(f"    最优阈值 {best['threshold']:.2f}  ARI {best['ari']:.3f}  "
                  f"P {best['precision']:.3f}  R {best['recall']:.3f}  F1 {best['f1']:.3f}")
            print(f"    簇数 预测 {best['pred_clusters']} vs gold {best['gold_clusters']}"
                  f"（差 {best['pred_clusters'] - best['gold_clusters']:+d}）")
            if band:
                print(f"    ARI 平台区 [{band[0]:.2f}, {band[1]:.2f}]"
                      f"（宽 {(band[1] - band[0]):.2f}，越宽越不敏感）")
            # 打印关键阈值附近的 ARI，让人看见曲线形状而不是只看一个点
            marks = [0.20, 0.28, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80]
            cells = []
            for m in marks:
                hit = min(rows, key=lambda r: abs(r["threshold"] - m))
                cells.append(f"{m:.2f}:{hit['ari']:.2f}/{hit['pred_clusters']}")
            print("    阈值:" + "  ".join(cells))
            report["runs"][f"{provider}|{group_key}"] = {
                "separation": sep,
                "best": best,
                "plateau": band,
                "curve": rows,
            }

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整曲线写入 {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
