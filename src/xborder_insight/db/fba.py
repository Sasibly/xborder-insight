"""FBA 费率维表的读取与写入。

分层约定：所有「怎么算」在 `domain/fba.py`（纯函数，不碰库），
这里只做「怎么读」与「读取失败怎么报」。

四件这里是刻意做的、别处容易做错的事：

1. 【读库失败不返回空表】
   把「表里没有行」和「查询出错」都变成 `[]`，是让缺口看起来像
   「这个尺寸段本来就不收配送费」。空表只能由 record 数量证明，
   所以这里对表不存在的情况直接抛 DbNotReady。

2. 【draft 行照读，由 domain 拒绝】
   若这里过滤掉 draft 行，domain 会报「缺这一档」，而真实原因是
   「这一档有值但没核实」。两者要补的材料完全不同（补档位 vs 补来源），
   所以 draft 行必须读出来，让 domain 说出准确的那句话。

3. 【版本必须一起读，不能只读行】
   行上的时段与生效期已经移除（m0010），「哪一期」只能从版本表读。
   若只读行、不读版本，代码就回到了「靠 season 猜」的旧状态：
   2027-12-01 会命中 2026 旺季的行且毫无察觉。

4. 【燃油附加费读不到就不施加，但要在报价里留痕】
   这个参数在 cost_params 里（乘数、全美统一，不随尺寸段变）。
   未核实时返回 None，domain 会按 0 计提并在 assumptions 写明
   「较实际偏低约 3.5%」。不静默、也不因此拒绝出结论——
   因为 FBA 基础费本身就已经是更硬的门槛。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date

from ..domain import fba as fba_domain
from ..domain.fba import FeeRow, FbaQuote, RateVersion, ScheduleAudit
from ..errors import DbNotReady, ParamUnverified
from . import params

TABLE = "fba_fee_schedule"
VERSION_TABLE = "fba_rate_version"
FUEL_SURCHARGE_KEY = "amazon.fba_fuel_surcharge"


def _table_exists(conn: sqlite3.Connection, name: str = TABLE) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _to_row(row: sqlite3.Row) -> FeeRow:
    return FeeRow(
        version_key=row["version_key"],
        size_tier=row["size_tier"],
        product_class=row["product_class"],
        weight_min_oz=float(row["weight_min_oz"]),
        weight_max_oz=None if row["weight_max_oz"] is None else float(row["weight_max_oz"]),
        price_band=row["price_band"],
        base_fee_usd=float(row["base_fee_usd"]),
        per_increment_usd=float(row["per_increment_usd"]),
        increment_oz=None if row["increment_oz"] is None else float(row["increment_oz"]),
        included_oz=None if row["included_oz"] is None else float(row["included_oz"]),
        status=row["status"],
        source_url=row["source_url"],
        note=row["note"],
    )


def _to_version(row: sqlite3.Row) -> RateVersion:
    return RateVersion(
        version_key=row["version_key"],
        marketplace=row["marketplace"],
        season=row["season"],
        effective_from=date.fromisoformat(row["effective_from"]),
        effective_to=date.fromisoformat(row["effective_to"]),
        status=row["status"],
        source_url=row["source_url"],
        verified_at=row["verified_at"],
        note=row["note"],
    )


def list_fee_rows(conn: sqlite3.Connection, marketplace: str) -> list[FeeRow]:
    if not _table_exists(conn):
        raise DbNotReady(
            f"费率表 {TABLE} 不存在",
            detail="请先执行迁移：python scripts/db.py up",
        )
    rows = conn.execute(
        f"SELECT * FROM {TABLE} WHERE marketplace = ? "
        "ORDER BY version_key, size_tier, product_class, price_band, weight_min_oz",
        (marketplace,),
    ).fetchall()
    return [_to_row(row) for row in rows]


def list_rate_versions(conn: sqlite3.Connection, marketplace: str) -> list[RateVersion]:
    if not _table_exists(conn, VERSION_TABLE):
        raise DbNotReady(
            f"费率版本表 {VERSION_TABLE} 不存在",
            detail="请先执行迁移：python scripts/db.py up",
        )
    rows = conn.execute(
        f"SELECT * FROM {VERSION_TABLE} WHERE marketplace = ? ORDER BY effective_from",
        (marketplace,),
    ).fetchall()
    return [_to_version(row) for row in rows]


def fuel_surcharge(conn: sqlite3.Connection, marketplace: str) -> tuple[float | None, str]:
    """返回 (可用费率或 None, 说明)。

    第二项不是日志，是要写进报价 assumptions 的**口径声明**。
    """
    param = params.find(conn, FUEL_SURCHARGE_KEY, marketplace)
    if param is None:
        return None, "参数表未登记燃油附加费，本次未施加"
    if not param.usable:
        return None, f"燃油附加费参数处于 {param.status} 状态，本次未施加"
    return float(param.value), ""


def resolve(
    conn: sqlite3.Connection,
    marketplace: str,
    *,
    size_tier: str,
    product_class: str,
    selling_price: float,
    ship_date: date,
    weight_oz: float | None = None,
    weight_lb: float | None = None,
) -> FbaQuote:
    """一单的 FBA 费：读版本 + 读表 → 定版本 → 查档位 → 附上燃油费。

    `product_class` 没有默认值：官方按非服装 / 服装 / 危险品分三套费率，
    含锂电池的 3C 与小家电属于危险品，按非服装报每件少算约 $0.8。
    `ship_date` 同理必须由调用方给：这不是「现在几点」，而是「这票货哪天发」，
    前者会让历史报表在跨过 10/15 后自动变价。
    """
    rows = list_fee_rows(conn, marketplace)
    versions = list_rate_versions(conn, marketplace)
    rate, why_missing = fuel_surcharge(conn, marketplace)
    quote = fba_domain.quote(
        rows,
        versions,
        size_tier=size_tier,
        product_class=product_class,
        selling_price=selling_price,
        ship_date=ship_date,
        weight_oz=weight_oz,
        weight_lb=weight_lb,
        fuel_surcharge_rate=rate,
    )
    if rate is None and why_missing:
        # domain 已写了一条通用口径说明，这里补上「为什么没施加」的具体原因：
        # 「未登记」和「登记了但没核实」对补数据的人是两个动作。
        quote = dataclasses.replace(quote, assumptions=quote.assumptions + (why_missing,))
    return quote


def envelope(
    conn: sqlite3.Connection,
    marketplace: str,
    *,
    product_class: str,
    selling_price: float,
    ship_date: date,
    size_tiers: tuple[str, ...] | list[str] | None = None,
    weight_range_oz: tuple[float, float] | None = None,
) -> fba_domain.FeeEnvelope:
    """不知道尺寸与重量时的 FBA 费范围：读版本 + 读表 + 读燃油费率 → 算包络。

    与 `resolve()` 的唯一差别是不需要精确的 `size_tier` 与 `weight_oz`。
    代价是结果从「一个数」变成「一个区间」——这个代价是诚实的，
    而不是把一个猜出来的数包装成一个数。

    `size_tiers` / `weight_range_oz` 是部分已知时的收窄条件，透传给 domain。
    """
    rows = list_fee_rows(conn, marketplace)
    versions = list_rate_versions(conn, marketplace)
    rate, why_missing = fuel_surcharge(conn, marketplace)
    result = fba_domain.fee_envelope(
        rows,
        versions,
        product_class=product_class,
        selling_price=selling_price,
        ship_date=ship_date,
        fuel_surcharge_rate=rate,
        size_tiers=size_tiers,
        weight_range_oz=weight_range_oz,
    )
    if rate is None and why_missing:
        result = dataclasses.replace(
            result, assumptions=result.assumptions + (why_missing,)
        )
    return result


def audit(conn: sqlite3.Connection, marketplace: str = "US") -> ScheduleAudit:
    return fba_domain.audit(
        list_fee_rows(conn, marketplace),
        list_rate_versions(conn, marketplace),
        marketplace=marketplace,
    )


def mark_version_verified(
    conn: sqlite3.Connection,
    *,
    version_key: str,
    source_url: str,
    verified_at: str,
) -> None:
    """把某一期费率置为 verified。

    只允许改已存在的版本：凭空造一个版本，会让「这一期存在」与
    「这一期有依据」混成一件事。
    """
    cursor = conn.execute(
        f"UPDATE {VERSION_TABLE} SET status = 'verified', source_url = ?, verified_at = ? "
        "WHERE version_key = ?",
        (source_url, verified_at, version_key),
    )
    if cursor.rowcount == 0:
        raise ParamUnverified(
            f"费率版本不存在：{version_key}",
            detail="无法标记为已核实；请先登记该版本的生效区间，缺口不能靠「标记」消灭",
        )


def mark_verified(
    conn: sqlite3.Connection,
    *,
    marketplace: str,
    version_key: str,
    size_tier: str,
    product_class: str,
    weight_min_oz: float,
    price_band: str,
    source_url: str,
    verified_at: str,
    base_fee_usd: float | None = None,
) -> None:
    """把某一行置为 verified。

    只允许改已存在的行：凭空 `mark_verified` 一个不存在的档位，
    会让「有这一行」和「这一行有依据」两件事被混成一件。
    """
    cursor = conn.execute(
        f"UPDATE {TABLE} SET status = 'verified', source_url = ?, "
        "verified_at = ?, base_fee_usd = COALESCE(?, base_fee_usd) "
        "WHERE marketplace = ? AND version_key = ? AND size_tier = ? "
        "AND product_class = ? AND weight_min_oz = ? AND price_band = ?",
        (
            source_url,
            verified_at,
            base_fee_usd,
            marketplace,
            version_key,
            size_tier,
            product_class,
            float(weight_min_oz),
            price_band,
        ),
    )
    if cursor.rowcount == 0:
        raise ParamUnverified(
            f"费率表缺少该行：{version_key} / {size_tier} / {product_class} / "
            f"{weight_min_oz:g} oz / {price_band}",
            detail="无法标记为已核实；请先录入该档位，缺口不能靠「标记」消灭",
        )
