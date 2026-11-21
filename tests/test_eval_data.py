"""评测数据的一致性守卫。

为什么单独守这几个不变量：
1. 样本漏加金标准 → 「真值」退化成空集，预测全记 FP 或全记 TP，
   指标依然是 0~1 之间的合法数字，没有任何断言会红。这种静默错配比崩掉危险得多。
2. 评测脚本写死数据集清单 → 新品类永远不进指标，覆盖率下降也不会有任何信号。
3. 品类样本太少 → 单品类指标抖动大到无法解释，比不报更糟。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from xborder_insight.taxonomy import LABEL_KEYS

ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = ROOT / "data" / "samples"
GOLD_PATH = ROOT / "data" / "eval" / "gold_labels.jsonl"

# 单品类低于这个条数，指标的标准误大到没有解释价值
MIN_PER_DATASET = 20


def _gold() -> dict[str, list[str]]:
    gold: dict[str, list[str]] = {}
    for line in GOLD_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        gold[obj["review_id"]] = obj["labels"]
    return gold


def _samples() -> dict[str, list[dict]]:
    by_dataset: dict[str, list[dict]] = {}
    for path in sorted(SAMPLES_DIR.glob("*.jsonl")):
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        by_dataset[path.stem] = rows
    return by_dataset


def test_every_sample_has_gold_label() -> None:
    gold = _gold()
    missing = [
        row["review_id"]
        for rows in _samples().values()
        for row in rows
        if row["review_id"] not in gold
    ]
    assert not missing, f"这些样本没有金标准，评测会把真值当空集：{missing[:10]}"


def test_no_orphan_gold_label() -> None:
    sample_ids = {row["review_id"] for rows in _samples().values() for row in rows}
    orphans = sorted(set(_gold()) - sample_ids)
    assert not orphans, f"这些金标准找不到对应样本：{orphans[:10]}"


def test_review_ids_are_globally_unique() -> None:
    ids = [row["review_id"] for rows in _samples().values() for row in rows]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicated, f"review_id 跨品类重复，金标准会被覆盖：{duplicated}"


def test_gold_labels_stay_inside_taxonomy() -> None:
    offenders = {
        review_id: sorted(set(labels) - LABEL_KEYS)
        for review_id, labels in _gold().items()
        if set(labels) - LABEL_KEYS
    }
    assert not offenders, f"金标准出现闭集外的标签：{offenders}"


@pytest.mark.parametrize("dataset", sorted(_samples()))
def test_dataset_has_enough_samples(dataset: str) -> None:
    count = len(_samples()[dataset])
    assert count >= MIN_PER_DATASET, (
        f"{dataset} 只有 {count} 条，低于 {MIN_PER_DATASET} 条时单品类指标不可解释"
    )


def test_eval_discovers_every_dataset_on_disk() -> None:
    """评测脚本必须自动发现样本集，而不是维护一份手写清单。"""
    spec = importlib.util.spec_from_file_location("run_eval_probe", ROOT / "scripts" / "run_eval.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert set(module.discover_datasets()) == set(_samples())


def test_legacy_subset_metrics_are_pinned() -> None:
    """42 条旧样本上的 TP/FP/FN 是简历里引用的数字，扩品类不得改动它。

    扩集时最容易被顺手「优化」的就是旧样本（改个措辞让规则命中、换个标签让它对上）。
    一旦改了，0.8108 仍然算得出来，但已经不是同一批数据上的数字了——
    这种「数字没变、含义变了」是评测里最难发现的一类漂移。
    """
    import asyncio

    from xborder_insight.llm import MockLLMClient
    from xborder_insight.pipeline.extract import extract_many
    from xborder_insight.pipeline.ingest import load_reviews

    gold = _gold()
    reviews = []
    for dataset in ("wireless_charger", "pet_fountain"):
        reviews.extend(load_reviews(dataset))

    outcomes, _ = asyncio.run(extract_many(MockLLMClient(), reviews))

    tp = fp = fn = 0
    for outcome in outcomes:
        truth = set(gold[outcome.review_id])
        predicted = (
            {p.label for p in outcome.extraction.points if p.polarity == "negative"}
            if outcome.extraction is not None
            else set()
        )
        tp += len(truth & predicted)
        fp += len(predicted - truth)
        fn += len(truth - predicted)

    assert len(reviews) == 42
    assert (tp, fp, fn) == (30, 2, 12), (
        "旧 42 条子集的指标漂移了：F1 0.8108 是基于 (30, 2, 12) 算出来的，改动旧样本前请先想清楚"
    )
