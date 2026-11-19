"""0003：把退货损耗拆成四段归集。

为什么不保留单一的 `returns.rate` 就够：
    单一比率只能回答「退货率多少」，回答不了「退货一次要花多少」。
    而这两件事在业务上的杠杆完全不同——
    - 退货率高可能是品类特性，改产品也压不下去；
    - 处理费高是运营问题，换退货仓、改包装就能压。
    合成一个「退货损耗 = 售价 × 8%」之后，这两个杠杆被抹平成同一个数，
    算出来的毛利率看似精确，实际上没法指导任何决策。

四段的基数各不相同，这是刻意的：
    处理费      基数 = 每件退货（固定成本，与货值无关）
    退款管理费  基数 = 原佣金（平台退佣金时扣留的那一部分）
    商品损失    基数 = 采购价（不可再售的部分，全损）
    头程损失    基数 = 头程运费（退货后已付运费沉没）

    「退款管理费占原佣金」而不是「占售价」这一点尤其容易搞错：
    平台的退款管理费是在退佣金时按佣金比例扣留的，若按售价取比例，
    金额会偏大一个数量级，毛利被系统性低估。

与 0002 的关系：
    0002 建立了「没出处不许入库」的机制，本迁移沿用同一套 CHECK 约束，
    因此这四段同样必须核实来源后才能被业务代码使用。
"""

from __future__ import annotations

import sqlite3

CREATED_AT = "2026-11-17T00:00:00+08:00"  # 迁移内不使用 now()，保证可重复执行

# (param_key, marketplace, category, unit, note)
# 仍只声明「要核实什么」，不预置任何数值
SEED_RETURNS_DETAIL: tuple[tuple[str, str, str | None, str, str], ...] = (
    (
        "returns.handling_fee",
        "US",
        "wireless_charger",
        "usd_per_unit",
        "每件退货的处理成本（美元/退货件）。基数=退货件数；模型内部乘退货率折算到单件。"
        "需核实平台退货处理费规则与实际退货仓作业成本，两者可能重复计费",
    ),
    (
        "returns.refund_admin_rate",
        "US",
        "wireless_charger",
        "ratio",
        "退款管理费占【原佣金】的比例。基数=原佣金，不是售价——"
        "按售价取比例会把金额放大一个数量级。需核实平台的退款管理费计费规则",
    ),
    (
        "returns.goods_loss_ratio",
        "US",
        "wireless_charger",
        "ratio",
        "退货商品中不可再售的比例（占采购价）。基数=采购价，不含头程与关税。"
        "需以实际退货处置数据为准；无数据时按假设值使用并在结论中标注",
    ),
    (
        "returns.inbound_loss_ratio",
        "US",
        "wireless_charger",
        "ratio",
        "退货导致头程运费不可回收的比例（占头程运费）。基数=头程运费。"
        "能再售的退货仍需二次头程，因此该项通常不为 0；需按实际处置方式估算",
    ),
)

KEYS = tuple(row[0] for row in SEED_RETURNS_DETAIL)


def up(conn: sqlite3.Connection) -> None:
    # 复用 0002 的 cost_params 表结构与 CHECK 约束，不新建表：
    # 退货四段在语义上就是成本参数，另建表只会让「核实」这件事有两套规则。
    conn.executemany(
        "INSERT INTO cost_params "
        "(param_key, marketplace, category, unit, value, source_url, effective_date, "
        " verified_at, status, note, created_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, 'draft', ?, ?)",
        [(key, market, category, unit, note, CREATED_AT)
         for key, market, category, unit, note in SEED_RETURNS_DETAIL],
    )


def down(conn: sqlite3.Connection) -> None:
    # 只删本迁移插入的行。不用「DELETE FROM cost_params」——
    # 那样会把 0002 的种子一起带走，down 就不再是 up 的逆操作。
    placeholders = ", ".join("?" for _ in KEYS)
    conn.execute(
        f"DELETE FROM cost_params WHERE param_key IN ({placeholders})",
        KEYS,
    )
