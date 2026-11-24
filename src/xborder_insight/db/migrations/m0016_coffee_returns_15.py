"""0016：咖啡机退货率 10% → 15%，压力档 14% → 20%。

为什么是迁移而不是 `returns-set`：这是同一台机器第二次改口径（m0015 已经把
8% 改到 10%），而每次改都重画全部价格线——

    10%：A 档（改箱）签字线 $15.732、B 档（现箱）$13.719，A 档"开口 $15.00"成立
    15%：A 档 $12.813、B 档 $10.824，**两档都封死**（他报 $18.50，要降 30.8% / 41.5%）

    这台机器因此在 15% 口径下从「B 档先放、A 档可谈」变成「任何现实价位都不成立」。
    这种结论只能随版本库走：只在某台机器上改，干净克隆会给出另一套签字线，
    而差异不在代码里（`docs/03` §8.1 ⑤）。

两个数字的来源：

    - 基线 15%、压力档 20%：用户 2026-11-24 11:40 直接指定（「直接填入系统」）。
      仍不是后台读数：`evidence_kind` 保持 `estimate`、`status` 保持 `draft`，
      CHECK 挡着不许升 verified。真值在 Reports → FBA Returns（`H3`）。
    - **与 11-23 20:45 的 10%/14% 直接冲突**。差 5 个点 = 约 5.85 个毛利点，
      正好落在「还能谈」与「直接毙」的分界上。这个冲突记在 `docs/04` §37，
      不在这里判谁对：口径由用户定，判断由数字做。

`down()` 逐字还原 m0015 的那一行（连 note 一起）。迁移往返是整表指纹比对，
note 差一个字就红，所以旧文案从 m0015 直接引用，不重抄。
"""

from __future__ import annotations

import sqlite3

from .m0015_coffee_returns import NEW_NOTE as OLD_NOTE

SKU = "coffee_semi_1613"

OLD_BASELINE = 0.10
OLD_STRESS = 0.14
NEW_BASELINE = 0.15
NEW_STRESS = 0.20

NEW_NOTE = (
    "基线 15% 与压力档 20% 是**用户 2026-11-24 11:40 直接指定**的口径，"
    "不是后台读数：evidence_kind 仍是 estimate、status 仍是 draft，"
    "CHECK 挡着不许升 verified（要升必须先拿到 Reports → FBA Returns 的该 ASIN 读数，H3）。"
    "与 11-23 20:45 指定的 10%/14% 冲突，差 5 个点 ≈ 5.85 个毛利点，"
    "结论因此反转：A 档（改箱，FBA $11.85）15% 线采购上限 $12.813、"
    "B 档（现箱，FBA $14.40）$10.824，他报的 $18.50 分别要降 30.8% / 41.5%，"
    "两档都封死；20% 压力档下连 $11.85 都只有 11.17%。"
    "所以「箱规改到 8 英寸」这一问在 15% 口径下不值钱（只把上限从 $10.82 抬到 $12.81，"
    "距他的 $18.50 仍差 $5.69）。详见 docs/04 §37。"
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
