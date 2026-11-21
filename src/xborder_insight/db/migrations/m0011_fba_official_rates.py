"""0011：把官方费率页的数值落库，并置 verified。

0009 只把 4 档锚点搬进结构（12 行 draft），0010 补了版本与产品类别两维，
但表仍然「只能报错」：任何一次报价都会抛 param_unverified。
这个迁移是把它变成可用的一步，且只做一件事——**把已核实的数值写进去**。

录入范围（378 行，全部来自同一个官方页、同一次抓取）：

| 版本 | 产品类别 | 尺寸段 | 重量档 | 行数 |
| --- | --- | --- | --- | --- |
| us_2026_off_peak | 非服装 / 危险品 | small + large standard | 8 + 13 | 126 |
| us_2026_peak | 非服装 / 危险品 | small + large standard | 8 + 13 | 126 |
| us_2027_off_peak | 非服装 / 危险品 | small + large standard | 8 + 13 | 126 |

**故意不录**：服装（apparel）、大件与超大件（large_bulky / extra_large）、
2027 旺季（官方未发布）。这三块的缺口写在 docs/04 第 9 节，
`python scripts/db.py fba` 会把它们显示成覆盖率为 0 的角，
而不是让它们悄悄用非服装或上一期的费率顶上。

为什么把 12 行旧种子删掉再全量插入，而不是 UPDATE：
旧种子里的 12 个值恰好与新数据一致，但它们是「按 docs/04 二手锚点抄的、
未逐档复核」的行。留着它们、只把 status 改成 verified，等于把
「数值碰巧相同」当成「核对过」——两件事必须分开。全量替换之后，
每一行的来源都是这一次对官方页的抓取，note 里写的是同一句话。

燃油附加费（3.5%）一并置 verified：它是同一页上的同一条事实
（2026-04-17 起、作用于配送费而非售价、旺季同样适用），
分开放在另一个迁移里只会让「这张表能不能算钱」这个问题拆成两半。
2027 是否延续官方页没写终止日期，这一点记在 note 与 docs/04 里，
而不是当成已知。
"""

from __future__ import annotations

import sqlite3

from .fba_us_rates import ROW_NOTE, SOURCE_URL, VERIFIED_AT, fee_rows
from .m0009_fba_schedule import ANCHOR_NOTE, CREATED_AT, _DOCS_ANCHORS

MARKET = "US"
FUEL_SURCHARGE_KEY = "amazon.fba_fuel_surcharge"
FUEL_EFFECTIVE_DATE = "2026-04-17"
FUEL_RATE = 0.035

VERSION_KEYS = ("us_2026_off_peak", "us_2026_peak", "us_2027_off_peak")

INSERT = (
    "INSERT INTO fba_fee_schedule "
    "(version_key, marketplace, size_tier, product_class, weight_min_oz, weight_max_oz, "
    " price_band, base_fee_usd, per_increment_usd, increment_oz, status, "
    " source_url, verified_at, note, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?, ?)"
)


def up(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "UPDATE fba_rate_version SET status = 'verified', source_url = ?, verified_at = ? "
        "WHERE version_key = ?",
        [(SOURCE_URL, VERIFIED_AT, key) for key in VERSION_KEYS],
    )

    # 旧的 12 行是「按二手锚点抄的 draft」，与这次全量录入同键；先清空，
    # 让每一行的出处都统一到这一次抓取上（见模块说明）。
    conn.execute("DELETE FROM fba_fee_schedule")
    conn.executemany(
        INSERT,
        [
            (version, MARKET, tier, product_class, lo, hi, band, fee, per_inc, inc,
             SOURCE_URL, VERIFIED_AT, ROW_NOTE, CREATED_AT)
            for version, product_class, tier, lo, hi, band, fee, per_inc, inc in fee_rows()
        ],
    )

    cursor = conn.execute(
        "UPDATE cost_params SET value = ?, status = 'verified', source_url = ?, "
        "effective_date = ?, verified_at = ? WHERE param_key = ? AND marketplace = ?",
        (FUEL_RATE, SOURCE_URL, FUEL_EFFECTIVE_DATE, VERIFIED_AT,
         FUEL_SURCHARGE_KEY, MARKET),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(
            f"{FUEL_SURCHARGE_KEY} 应恰好有 1 行，实际 {cursor.rowcount} 行；"
            "缺失说明 m0009 未执行，不能假装附加费已核实"
        )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM fba_fee_schedule")
    # 还原 m0009 的 12 行 draft 种子。这里直接引用 m0009 的常量，
    # 而不是在本文件再抄一遍：m0009 已冻结，引用它才能保证「回滚后与从未升级一致」。
    conn.executemany(
        "INSERT INTO fba_fee_schedule "
        "(version_key, marketplace, size_tier, product_class, weight_min_oz, weight_max_oz, "
        " price_band, base_fee_usd, per_increment_usd, increment_oz, status, note, created_at) "
        "VALUES ('us_2026_off_peak', ?, ?, 'non_apparel', ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
        [
            (MARKET, tier, lo, hi, band, base, per_inc, inc, ANCHOR_NOTE, CREATED_AT)
            for tier, lo, hi, band, base, per_inc, inc in _DOCS_ANCHORS
        ],
    )
    conn.executemany(
        "UPDATE fba_rate_version SET status = 'draft', source_url = NULL, verified_at = NULL "
        "WHERE version_key = ?",
        [(key,) for key in VERSION_KEYS],
    )
    conn.execute(
        "UPDATE cost_params SET value = NULL, status = 'draft', source_url = NULL, "
        "effective_date = NULL, verified_at = NULL WHERE param_key = ? AND marketplace = ?",
        (FUEL_SURCHARGE_KEY, MARKET),
    )
