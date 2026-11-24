"""0017：咖啡机退货率口径裁决——15% 确定，不再与 11-23 的 10% 并行。

**数值不动**（仍是 15% / 20%，m0016 已经写进去）。这条迁移只改 `note`：

    m0016 的 note 结尾写着「与 11-23 20:45 指定的 10%/14% 冲突……不在这里判谁对」。
    那是 11-24 11:40 当时的正确状态——两个数并存，谁也不敢替用户裁。
    11-24 13:44 用户明确「退货率确定 15%」，冲突就到此为止：再留着「不判谁对」
    的措辞，下一个人读到会以为还有个待他确认的悬案，而结论其实已经定了。

为什么改 note 也要发迁移：`note` 是库里的内容，只在工作区的 sqlite 上改，
干净克隆会读到另一段话（`docs/03` §8.1 ⑤）。数字一样不代表记录一样——
这段 note 正是「这个数是谁定的、凭什么」的唯一载体。

**裁决不等于核实**：用户拍板（decision）和后台读数（measurement）是两件事。
`evidence_kind` 仍是 `estimate`、`status` 仍是 `draft`，CHECK 挡着不许升 verified；
要升还是那一条路——Reports → Fulfillment → FBA Returns 导该 ASIN 近 90 天的
return rate（`H3`）。读数与 15% 不同时，按同一节发下一条迁移，不就地改。
"""

from __future__ import annotations

import sqlite3

from .m0016_coffee_returns_15 import NEW_NOTE as OLD_NOTE

SKU = "coffee_semi_1613"

BASELINE = 0.15
STRESS = 0.20

NEW_NOTE = (
    "基线 15%、压力档 20%：**用户 2026-11-24 11:40 指定、13:44 明确「确定 15%」**，"
    "口径已裁决，不再与 11-23 20:45 的 10%/14% 并行（差 5 个点 ≈ 5.85 个毛利点，"
    "当时正是「能谈」与「毙掉」的分界，见 docs/04 §37.1）。"
    "**但裁决不等于核实**：这是用户口头拍板、不是后台读数，"
    "evidence_kind 仍是 estimate、status 仍是 draft，CHECK 挡着不许升 verified；"
    "要升必须先拿到 Reports → Fulfillment → FBA Returns 里该 ASIN 近 90 天的读数（H3），"
    "读数与 15% 不同时发新迁移改，不就地改。"
    "该口径下的结论：A 档（改箱，FBA $11.85）15% 线采购上限 $12.813、"
    "B 档（现箱，FBA $14.40）$10.824，他报的 $18.50 分别要降 30.7% / 41.5% → 两档都封死；"
    "20% 压力档下连 $11.85 都只有 11.17%。"
    "所以「箱规改到 8 英寸」这一问在 15% 口径下不值钱（只把上限从 $10.82 抬到 $12.81，"
    "距他的 $18.50 仍差 $5.69），那版箱规话术不发。详见 docs/04 §37。"
)


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE product_returns "
        "SET baseline_rate = ?, stress_rate = ?, note = ? "
        "WHERE marketplace = 'US' AND sku_key = ?",
        (BASELINE, STRESS, NEW_NOTE, SKU),
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE product_returns "
        "SET baseline_rate = ?, stress_rate = ?, note = ? "
        "WHERE marketplace = 'US' AND sku_key = ?",
        (BASELINE, STRESS, OLD_NOTE, SKU),
    )
