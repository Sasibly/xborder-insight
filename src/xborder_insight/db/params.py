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

from ..errors import ParamUnverified

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

    @property
    def usable(self) -> bool:
        return self.status == VERIFIED and self.value is not None


def _to_param(row: sqlite3.Row) -> CostParam:
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


def require(conn: sqlite3.Connection, key: str, marketplace: str) -> CostParam:
    """取一个已核实的参数。取不到就抛错，绝不回落默认值。"""
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
) -> None:
    """把参数置为已核实。参数化校验交给数据库的 CHECK 约束，这里只负责写。

    不做「先查再写」的应用层校验：约束写在表上，任何写入路径都逃不掉，
    包括以后有人直接用 sqlite3 命令行改库。
    """
    conn.execute(
        "UPDATE cost_params SET value = ?, source_url = ?, effective_date = ?, "
        "verified_at = ?, status = 'verified' "
        "WHERE param_key = ? AND marketplace = ?",
        (value, source_url, effective_date, verified_at, key, marketplace),
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
