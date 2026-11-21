"""参数访问层：让「没有出处的数字」在代码里用不起来。

核心是 `require()`：业务代码想说「佣金率是 X」，就必须走这里；
走这里就必然面对「这个参数核实过吗」。没核实就抛 ParamUnverified，
而不是返回一个默认值。

为什么不用 `get(key, default=...)`：
default 的存在会让「参数缺失」变成静默降级。利润模型拿到 0.15 的默认佣金率
算出来的毛利看起来完全正常——这就是 ②类错误。宁可不给结论。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..errors import ParamOutOfRange, ParamUnverified

VERIFIED = "verified"
DRAFT = "draft"


@dataclass(frozen=True)
class CostParam:
    param_key: str
    marketplace: str
    category: str | None
    unit: str
    value: float | None
    status: str
    source_url: str | None
    effective_date: str | None
    verified_at: str | None
    note: str | None
    applies_min: float | None = None
    applies_max: float | None = None

    @property
    def usable(self) -> bool:
        return self.status == VERIFIED and self.value is not None

    @property
    def has_range(self) -> bool:
        """是否声明了适用区间。NULL / NULL 表示「该参数与取值无关」。"""
        return self.applies_min is not None or self.applies_max is not None

    @property
    def range_label(self) -> str:
        low, high = self.applies_min, self.applies_max
        if low is None and high is None:
            return "全区间"
        if low is None:
            return f"≤ {high:g}"
        if high is None:
            return f"≥ {low:g}"
        return f"{low:g} – {high:g}"

    def covers(self, value: float) -> bool:
        if self.applies_min is not None and value < self.applies_min:
            return False
        return not (self.applies_max is not None and value > self.applies_max)


def _to_param(row: sqlite3.Row) -> CostParam:
    # 0007 之前的库没有这两列。连接层会先跑迁移，但读取函数不该假设
    # 「迁移一定已执行」——否则一个只读的运维脚本会在旧库上直接 KeyError。
    keys = row.keys()
    return CostParam(
        param_key=row["param_key"],
        marketplace=row["marketplace"],
        category=row["category"],
        unit=row["unit"],
        value=row["value"],
        status=row["status"],
        source_url=row["source_url"],
        effective_date=row["effective_date"],
        verified_at=row["verified_at"],
        note=row["note"],
        applies_min=row["applies_min"] if "applies_min" in keys else None,
        applies_max=row["applies_max"] if "applies_max" in keys else None,
    )


def list_params(conn: sqlite3.Connection, marketplace: str | None = None) -> list[CostParam]:
    if marketplace:
        rows = conn.execute(
            "SELECT * FROM cost_params WHERE marketplace = ? ORDER BY param_key",
            (marketplace,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM cost_params ORDER BY marketplace, param_key").fetchall()
    return [_to_param(row) for row in rows]


def find(conn: sqlite3.Connection, key: str, marketplace: str) -> CostParam | None:
    row = conn.execute(
        "SELECT * FROM cost_params WHERE param_key = ? AND marketplace = ?",
        (key, marketplace),
    ).fetchone()
    return None if row is None else _to_param(row)


def require(
    conn: sqlite3.Connection,
    key: str,
    marketplace: str,
    *,
    applied_to: float | None = None,
) -> CostParam:
    """取一个已核实的参数；若它声明了适用区间，校验本次取值是否落在区间内。

    `applied_to` 是「这次要拿这个参数去评估的量」，最常见是售价。

    为什么必须校验区间：
        费率是函数不是常数。佣金 15% 只对前 $100 成立、退款管理费有 $5 上限、
        FBA 费随尺寸重量跳档。区间只写在 note 里时，售价 $150 的商品会静默
        套用「$100 以下」的费率——不报错、不警告，结论看起来完全正常。
        把取值传进来，越界就能当场失败，而不是等到复盘时才发现。

    为什么「有区间但没传 applied_to」也报错：
        「不知道是否适用但照样用」与「确认不适用仍然用」在后果上没有区别，
        只是前者更难被发现。宁可让调用方多传一个数。
    """
    param = find(conn, key, marketplace)
    if param is None:
        raise ParamUnverified(
            f"缺少参数: {key}（市场 {marketplace}）",
            detail="请先在 cost_params 中登记并核实该参数",
        )
    if not param.usable:
        raise ParamUnverified(
            f"参数未核实: {key}（市场 {marketplace}）",
            detail=param.note or "该参数处于 draft 状态，缺少官方来源或生效日期",
        )
    if not param.has_range:
        return param
    if applied_to is None:
        raise ParamOutOfRange(
            f"参数 {key} 声明了适用区间 {param.range_label}，但未提供被评估的取值",
            detail="无法确认本次取值是否落在区间内；请传入 applied_to（通常是售价）",
        )
    if not param.covers(float(applied_to)):
        raise ParamOutOfRange(
            f"参数 {key} 的适用区间为 {param.range_label}，本次取值 {float(applied_to):g} 越界",
            detail=(
                f"该值仅在 {param.range_label} 内成立。请补充对应区间的参数，"
                f"或改用在区间内的口径。生效日期: {param.effective_date}"
            ),
        )
    return param


def snapshot(conn: sqlite3.Connection, marketplace: str) -> dict[str, float]:
    """当前可用参数的快照，写入 runs.params_snapshot 供事后审计。"""
    return {
        p.param_key: float(p.value)
        for p in list_params(conn, marketplace)
        if p.usable and p.value is not None
    }


def coverage(conn: sqlite3.Connection, marketplace: str | None = None) -> dict[str, object]:
    """核实进度。用于 README 如实交代「参数表里有多少条真有出处」。"""
    params = list_params(conn, marketplace)
    verified = [p for p in params if p.usable]
    return {
        "marketplace": marketplace or "all",
        "total": len(params),
        "verified": len(verified),
        "draft": len(params) - len(verified),
        "verified_keys": sorted(p.param_key for p in verified),
        "missing_keys": sorted(p.param_key for p in params if not p.usable),
    }


def mark_verified(
    conn: sqlite3.Connection,
    key: str,
    marketplace: str,
    *,
    value: float,
    source_url: str,
    effective_date: str,
    verified_at: str,
    applies_min: float | None = None,
    applies_max: float | None = None,
) -> None:
    """把参数置为已核实。参数化校验交给数据库的 CHECK 约束，这里只负责写。

    不做「先查再写」的应用层校验：约束写在表上，任何写入路径都逃不掉，
    包括以后有人直接用 sqlite3 命令行改库。

    `applies_min` / `applies_max` 省略表示**保持原值**（COALESCE 语义），
    不是「设为 NULL」。一次核实通常同时确定值与区间，分两步写会留下
    「值核实了、区间忘了」的中间态，而那正是要防的错。
    要清空已有区间用 `set_applies_range(conn, key, market, applies_min=None, applies_max=None)`。
    """
    cursor = conn.execute(
        "UPDATE cost_params SET value = ?, source_url = ?, effective_date = ?, "
        "verified_at = ?, status = 'verified', "
        "applies_min = COALESCE(?, applies_min), "
        "applies_max = COALESCE(?, applies_max) "
        "WHERE param_key = ? AND marketplace = ?",
        (value, source_url, effective_date, verified_at, applies_min, applies_max, key, marketplace),
    )
    if cursor.rowcount == 0:
        raise ParamUnverified(
            f"缺少参数: {key}（市场 {marketplace}）",
            detail="无法标记为已核实；请先登记该参数",
        )


def set_applies_range(
    conn: sqlite3.Connection,
    key: str,
    marketplace: str,
    *,
    applies_min: float | None,
    applies_max: float | None,
) -> None:
    """声明（或清空）参数的适用区间。

    区间写反由表级 CHECK（applies_min <= applies_max）拒绝，这里不重复校验——
    两处都写会让「哪一处才是真的」变得含糊。
    两个都传 None 表示「该参数与取值无关」，清空区间。
    """
    cursor = conn.execute(
        "UPDATE cost_params SET applies_min = ?, applies_max = ? "
        "WHERE param_key = ? AND marketplace = ?",
        (applies_min, applies_max, key, marketplace),
    )
    if cursor.rowcount == 0:
        raise ParamUnverified(
            f"缺少参数: {key}（市场 {marketplace}）",
            detail="无法设置适用区间；请先登记该参数",
        )


def compliance_rules(
    conn: sqlite3.Connection, category: str, marketplace: str, include_draft: bool = False
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM compliance_rules WHERE category = ? AND marketplace = ?"
    params: list[object] = [category, marketplace]
    if not include_draft:
        sql += " AND status = 'verified'"
    sql += " ORDER BY severity DESC, rule_key"
    return conn.execute(sql, params).fetchall()
