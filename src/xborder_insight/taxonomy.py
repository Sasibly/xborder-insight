"""痛点标签体系（taxonomy）。

固定闭集是有意设计：模型只能在闭集内选择，越界即视为契约违规并触发降级重试。
这比「让模型自由发挥」更可控，也让评测的金标准可以对齐。
"""

from __future__ import annotations

import hashlib
import json

TAXONOMY: dict[str, str] = {
    "charging_speed": "充电/出水速度慢",
    "heat": "发热发烫",
    "compatibility": "兼容性问题",
    "build_quality": "做工与耐用性差",
    "noise": "噪音大",
    "leakage": "漏水/渗液",
    "cleaning": "清洁困难",
    "pump_life": "核心部件寿命短",
    "size_fit": "尺寸/适配不符",
    "value": "性价比不符预期",
}

LABEL_KEYS: frozenset[str] = frozenset(TAXONOMY)

# 关键词提示表：mock 模式据此模拟抽取，也用于 live 模式的兜底校验
LABEL_HINTS: dict[str, tuple[str, ...]] = {
    "charging_speed": ("slow", "slowly", "takes forever", "speed", "fast charge", "慢", "充电慢", "充不满"),
    "heat": ("hot", "overheat", "warm", "heat", "烫", "发热", "过热"),
    "compatibility": ("compatible", "case", "iphone", "android", "doesn't fit", "兼容", "不适配", "机型"),
    "build_quality": ("cheap", "flimsy", "broke", "cracked", "plastic", "quality", "做工", "坏了", "裂"),
    "noise": ("noisy", "loud", "buzzing", "rattle", "吵", "噪音", "嗡嗡"),
    "leakage": ("leak", "leaking", "spill", "water everywhere", "漏", "渗水", "漏水"),
    "cleaning": ("clean", "hard to clean", "mold", "slime", "清洁", "难洗", "发霉", "水垢"),
    "pump_life": ("pump", "stopped working", "died", "dead after", "泵", "不出水", "停止工作"),
    "size_fit": ("too big", "too small", "size", "larger", "smaller", "太大", "太小", "尺寸"),
    "value": ("not worth", "overpriced", "waste of money", "returned", "不值", "太贵", "退货"),
}

NEGATIVE_CUES: tuple[str, ...] = (
    "slow", "hot", "cheap", "broke", "noisy", "leak", "hard", "stopped", "not worth",
    "waste", "returned", "disappointed", "worse", "failed", "useless",
    "慢", "烫", "贵", "坏", "吵", "漏", "难", "不值", "退货", "失望", "垃圾", "差",
)
POSITIVE_CUES: tuple[str, ...] = (
    "love", "great", "perfect", "excellent", "works well", "recommend", "happy",
    "喜欢", "很好", "完美", "推荐", "满意", "不错",
)

# 标签体系版本 = 内容指纹，不是手写的 "v1.2"。
# 手写版本号一定会忘记改：加了一个标签、忘了升版本，历史结果就再也对不上。
# 用内容哈希，改一个字版本就变，这是「可复现」能被验证的前提。
TAXONOMY_VERSION = "tax-" + hashlib.sha256(
    json.dumps({"labels": TAXONOMY, "hints": LABEL_HINTS}, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()[:12]
