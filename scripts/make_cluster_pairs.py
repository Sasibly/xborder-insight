"""生成聚类评测的 pairwise 标注集（一次性，产物入库为准）。

标注标准（唯一一条，必须能复述）：
    「同一条改进行动，能同时解决这两条 evidence 反映的问题吗？」

为什么用「改进行动」而不是「意思接近」：
聚类的用途是输出可执行的机会点（"夏天车内暴晒后充电中断"），不是做同义句归并。
用语义接近做标准，会把「充电慢」和「充电快」判成同类——它们在字面上最接近，
而在经营上完全相反。用行动做标准，这个错误会被自动排除。

三个由这条标准推出的判定：
1. 正负极性必然不同组。「充电快」要维持，「充电慢」要提升，不是同一个行动。
   → 这条直接指向 analyze.py 的一个缺陷：分组键只有 label，没带 polarity。
2. 同一部件、同一失效方式 → 同组（泵坏 vs 电机死）。
3. 不同部件、不同失效方式 → 不同组（线圈烧毁 vs 滤芯堵塞），哪怕 taxonomy 标签相同。

产物：data/eval/cluster_pairs.jsonl，每行一对，含两段 evidence 原文以便复核。
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "eval" / "cluster_evidence_live.jsonl"
OUT = ROOT / "data" / "eval" / "cluster_pairs.jsonl"

# 每个 label 组内，判为「同一主题」的下标对。下标 = 该组内的出现顺序（0 起）。
# 未列出的对一律判为「不同主题」——显式列出同组对，是为了让漏标的对表现为
# 「判成不同」而不是「悄悄变成同类」，后者会让指标虚高且看不出来。
SAME_PAIRS: dict[str, list[tuple[int, int]]] = {
    # 6 条负向全是「充得慢」，正负 18 对必然不同
    "charging_speed": list(itertools.combinations([0, 2, 4, 5, 6, 7], 2))
    + list(itertools.combinations([1, 3, 8], 2)),
    # 负向 5 条都是用料/工艺，正向 2 条互为一组
    "build_quality": list(itertools.combinations([0, 2, 4, 5, 6], 2))
    + list(itertools.combinations([1, 3], 2)),
    # 负向 5 条都是清洁维护负担（难洗、难拆、积毛絮）
    "cleaning": list(itertools.combinations([0, 1, 2, 3, 4], 2)),
    # 负向 4 条都是发热，程度与后果不同但同一个散热改进行动
    "heat": list(itertools.combinations([0, 2, 3, 4], 2)),
    # 负向 4 条都是噪音
    "noise": list(itertools.combinations([0, 1, 2, 3], 2)),
    # 只有「泵/电机早期失效」三条同组；线圈烧毁与滤芯堵塞各自独立
    "pump_life": list(itertools.combinations([1, 2, 4], 2)),
    # 「太大」两条同组；「太小」与「带壳不兼容」各自独立
    "size_fit": [(0, 1)],
    # 「底部渗漏」三条同组（含接缝不密封）；溢出是水位控制问题，独立
    "leakage": list(itertools.combinations([0, 1, 3], 2)),
    # 带壳不兼容、接口松动、正向兼容：三者互不相同
    "compatibility": [],
    # 「整体定价高」两条同组；耗材 TCO 是另一个改进行动（降滤芯单价），独立
    "value": [(0, 1)],
}


def main() -> None:
    rows = [json.loads(line) for line in EVIDENCE.read_text(encoding="utf-8").splitlines() if line.strip()]

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["label"], []).append(row)

    out: list[dict] = []
    for label, members in grouped.items():
        same_set = {tuple(sorted(p)) for p in SAME_PAIRS.get(label, [])}
        for i, j in itertools.combinations(range(len(members)), 2):
            a, b = members[i], members[j]
            out.append(
                {
                    "label": label,
                    "a_idx": i,
                    "b_idx": j,
                    "a": a["evidence"],
                    "b": b["evidence"],
                    "a_polarity": a["polarity"],
                    "b_polarity": b["polarity"],
                    "same": (i, j) in same_set,
                }
            )

    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n", encoding="utf-8")

    total = len(out)
    same = sum(1 for r in out if r["same"])
    print(f"写出 {OUT.relative_to(ROOT)}")
    print(f"  共 {total} 对：同一主题 {same} · 不同主题 {total - same}")
    by_label: dict[str, list[int]] = {}
    for r in out:
        stat = by_label.setdefault(r["label"], [0, 0])
        stat[0 if r["same"] else 1] += 1
    for label, (s, d) in sorted(by_label.items(), key=lambda x: -(x[1][0] + x[1][1])):
        print(f"  {label:18s} 同 {s:2d} · 异 {d:2d}")
    # 极性混杂是 analyze.py 分组的已知缺陷，这里量化它
    cross = sum(1 for r in out if r["a_polarity"] != r["b_polarity"])
    print(f"  跨极性对 {cross} 对（当前实现会把这些一起聚，其中判为同主题的 "
          f"{sum(1 for r in out if r['a_polarity'] != r['b_polarity'] and r['same'])} 对）")


if __name__ == "__main__":
    main()
