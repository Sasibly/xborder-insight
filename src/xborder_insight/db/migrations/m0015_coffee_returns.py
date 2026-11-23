"""0015：咖啡机退货率 8% → 10%，压力档 15% → 14%。

为什么这是一条迁移，而不是一次 `db.py returns-set`：

    改退货率不是「更新一行数据」，是**改一条业务结论的输入**。10% 与 8%
    差 2 个点，落在 $63.99 上是 **2.3 个点毛利**——`docs/04` §27 那四条价格线
    （$16.50 / $16.90 / $14.50 / $14.89）全部跌破 15%，签字线整体下移约 $1.2。
    这种改动若只发生在某台机器的 `data/app.sqlite3` 里，干净克隆仍是 8%：
    同一句 `screen` 在两台机器上给出不同的签字线，而差异不在代码里。

    项目对这条的既有态度写在 `docs/03` §13：「别人能跑」只能空目录 + 新 venv 真跑
    一次。数据也一样——**结论的输入必须随版本库走**。所以用迁移，不用 `returns-set`。

两个数字的来源：

    - 基线 10%：用户 2026-11-23 20:45 直接指定（「用于基线计算」）。
      它不是后台读数，`evidence_kind` 保持 `estimate`、`status` 保持 `draft`——
      CHECK 会挡住任何把它刷成 verified 的尝试。真值仍要等 H3 的
      Reports → FBA Returns 报表。
    - 压力档 14%：同一条指示（「压力测试备用值为 14%」），比基线高 4 个点。

    注意压力档从 15% **降**到 14%：两档差从 7 个点收窄到 4 个点。这不是放宽，
    是随基线一起平移——原来的 15% 只是「8% + 7」这条规则的产物，不是独立读数。

`down()` 必须逐字还原 `m0014` 的那一行（连 note 一起）。迁移往返的指纹比对
是全表内容比，note 差一个字就红。所以旧文案从 `m0014_product_returns` 直接引用，
不重抄——重抄是笔误的入口。
"""

from __future__ import annotations

import sqlite3

from .m0014_product_returns import COFFEE_NOTE as OLD_NOTE

SKU = "coffee_semi_1613"

OLD_BASELINE = 0.08
OLD_STRESS = 0.15
NEW_BASELINE = 0.10
NEW_STRESS = 0.14

NEW_NOTE = (
    "基线 10% 与压力档 14% 是**用户 2026-11-23 20:45 直接指定**的口径，"
    "不是后台读数：evidence_kind 仍是 estimate、status 仍是 draft，"
    "CHECK 挡着不许升 verified（要升必须先拿到 Reports → FBA Returns 的该 ASIN 读数，H3）。"
    "10% 落在这台机器的四条价格线上，结论与 8% 完全相反："
    "$16.50 / $16.90（改箱）与 $14.50 / $14.89（不改箱）全部跌破 15%，"
    "签字线分别下移到 $15.70（15.06%）与 $13.70（15.04%）。"
    "压力档 14% 比基线高 4 个点，两档差从 7 收窄到 4——随基线平移，不是放宽。"
    "详见 docs/04 §33。"
)


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE product_returns "
        "SET baseline_rate = ?, stress_rate = ?, note = ? "
        "WHERE marketplace = 'US' AND sku_key = ?",
        (NEW_BASELINE, NEW_STRESS, NEW_NOTE, SKU),
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE product_returns "
        "SET baseline_rate = ?, stress_rate = ?, note = ? "
        "WHERE marketplace = 'US' AND sku_key = ?",
        (OLD_BASELINE, OLD_STRESS, OLD_NOTE, SKU),
    )
