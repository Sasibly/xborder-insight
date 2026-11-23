"""逐 SKU 退货率访问层（`product_returns`，m0014）。

这一层的职责只有一个：把「这一台的两档退货率」变成可查询的事实，
让 `screen` / API / `make check` 取到同一个数。它**不做**判定——
「两档都为正才值得谈」是 `domain.admission.stress_test()` 的规则，
放在这里会让同一套规则在 db 与 domain 各有一份，迟早各改各的。

为什么 `require()` 在 draft 行上也返回而不是抛错：
    退货率的两次全部是 draft（净化器是类目阈值、咖啡机是假设），
    若按 `params.require()` 的口径只放 verified 行，这张表会永远查不出东西，
    压测只能回到命令行传数——那正是本表要消灭的状态。
    正确做法是把「证据强弱」随结论一起带出去（`usable` / `evidence_label`），
    让调用方在输出里写清「按假设 8% 算」，而不是拒答。
    拒答只发生在**没有登记**这一台时：那是「不知道」，不是「知道但不牢」。

与 `domain` 的边界：
    本模块不 import `domain.admission`，也不构造 `Candidate`。
    组装（把两档塞进 Candidate、跑 stress_test）属于调用方，
    这样 db 层不会因为 domain 改字段而连带失败，反之亦然。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..errors import ReturnRateMissing

VERIFIED = "verified"
DRAFT = "draft"

#: 证据类型。前两档可以升 verified，后两档永远 draft（表级 CHECK 钉住）。
#:
#: `category_threshold` 是实测逼出来的一档：净化器的 12.6% 确实从后台读来，
#: 但读到的是类目阈值，不是这一台的实际退货率。只标 `backend_read` 会让一行
#: draft 在台账上显示「证据齐全」——待办列表空着，却什么都没落实。
EVIDENCE_KINDS = ("backend_read", "official_page", "category_threshold", "estimate")

#: 可升 verified 的证据类型。`category_threshold` 描述的不是这一台，不能算。
VERIFIABLE_KINDS = ("backend_read", "official_page")

EVIDENCE_LABELS = {
    "backend_read": "后台读数（本 SKU）",
    "official_page": "官方页面",
    "category_threshold": "类目阈值",
    "estimate": "假设",
}

DEFAULT_MARKET = "US"


@dataclass(frozen=True)
class ProductReturn:
    sku_key: str
    marketplace: str
    product_name: str
    category: str | None
    asin: str | None
    baseline_rate: float
    stress_rate: float
    evidence_kind: str
    source_note: str | None
    effective_date: str | None
    verified_at: str | None
    status: str
    note: str | None

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED

    @property
    def is_estimate(self) -> bool:
        return self.evidence_kind == "estimate"

    @property
    def usable(self) -> bool:
        """基线是否**就是这一台**的实测/官方读数（可对外称「读到的」）。"""
        return self.verified and self.evidence_kind in VERIFIABLE_KINDS

    @property
    def evidence_label(self) -> str:
        label = EVIDENCE_LABELS.get(self.evidence_kind, self.evidence_kind)
        return label if self.verified else f"{label}（未核实）"

    @property
    def rate_gap_pts(self) -> float:
        """两档相差几个百分点。差得越大，压测越关键（咖啡机 7 vs 净化器 4.4）。"""
        return (self.stress_rate - self.baseline_rate) * 100

    @property
    def missing_evidence(self) -> tuple[str, ...]:
        """离「这个数就是这一台的实测值」还差什么。

        draft 行必须能列出一条可执行的下一步，否则「待补」等于没说。
        `category_threshold` 专门给一句：别人看到 12.6% 会以为已经读到了，
        实际上读的是类目阈值——这两件事在报告里的可信度差一档。
        """
        if self.usable:
            return ()
        missing: list[str] = []
        if self.status != VERIFIED:
            if self.evidence_kind == "category_threshold":
                missing.append(
                    "该 ASIN 的实际退货率（现在借用类目阈值，不是这一台的数）"
                )
            elif self.evidence_kind == "estimate":
                missing.append("换成真实读数（现在整行是假设）")
        if self.source_note is None:
            missing.append("来源（后台路径 + 读到的是什么）")
        if self.effective_date is None:
            missing.append("读数日期")
        if self.verified_at is None:
            missing.append("核实日期")
        return tuple(missing)


def _to_row(row: sqlite3.Row) -> ProductReturn:
    return ProductReturn(
        sku_key=row["sku_key"],
        marketplace=row["marketplace"],
        product_name=row["product_name"],
        category=row["category"],
        asin=row["asin"],
        baseline_rate=float(row["baseline_rate"]),
        stress_rate=float(row["stress_rate"]),
        evidence_kind=row["evidence_kind"],
        source_note=row["source_note"],
        effective_date=row["effective_date"],
        verified_at=row["verified_at"],
        status=row["status"],
        note=row["note"],
    )


def list_all(conn: sqlite3.Connection, marketplace: str | None = None) -> list[ProductReturn]:
    if marketplace:
        rows = conn.execute(
            "SELECT * FROM product_returns WHERE marketplace = ? ORDER BY sku_key",
            (marketplace,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM product_returns ORDER BY marketplace, sku_key"
        ).fetchall()
    return [_to_row(row) for row in rows]


def find(
    conn: sqlite3.Connection, sku_key: str, marketplace: str = DEFAULT_MARKET
) -> ProductReturn | None:
    row = conn.execute(
        "SELECT * FROM product_returns WHERE sku_key = ? AND marketplace = ?",
        (sku_key, marketplace),
    ).fetchone()
    return None if row is None else _to_row(row)


def require(
    conn: sqlite3.Connection, sku_key: str, marketplace: str = DEFAULT_MARKET
) -> ProductReturn:
    """取一台的两档。**没有登记**才报错——draft 行照常返回（见模块注释）。"""
    record = find(conn, sku_key, marketplace)
    if record is None:
        known = [r.sku_key for r in list_all(conn, marketplace)]
        raise ReturnRateMissing(
            f"没有登记退货率：{sku_key}（市场 {marketplace}）",
            detail=(
                "可用 sku_key：" + ("、".join(known) if known else "（表为空）")
                + "。先跑 `python scripts/db.py returns-set --sku ...` 登记两档；"
                "不登记就不压测，也不拿默认值替它猜"
            ),
        )
    return record


def coverage(conn: sqlite3.Connection, marketplace: str | None = None) -> dict[str, object]:
    """登记进度。对外交代「有几台这个数真是这一台的、几台还是借的或猜的」。"""
    records = list_all(conn, marketplace)
    return {
        "marketplace": marketplace or "all",
        "total": len(records),
        "verified": sum(1 for r in records if r.verified),
        "usable": sum(1 for r in records if r.usable),
        "threshold": sum(1 for r in records if r.evidence_kind == "category_threshold"),
        "estimate": sum(1 for r in records if r.is_estimate),
        "pending_keys": sorted(r.sku_key for r in records if not r.usable),
    }


def _check_kind(evidence_kind: str) -> None:
    if evidence_kind not in EVIDENCE_KINDS:
        raise ValueError(
            f"evidence_kind 只能是 {'/'.join(EVIDENCE_KINDS)}，收到 {evidence_kind!r}"
        )


def upsert(
    conn: sqlite3.Connection,
    *,
    sku_key: str,
    product_name: str,
    baseline_rate: float,
    stress_rate: float,
    category: str | None = None,
    asin: str | None = None,
    evidence_kind: str = "estimate",
    source_note: str | None = None,
    effective_date: str | None = None,
    verified_at: str | None = None,
    status: str = DRAFT,
    note: str | None = None,
    marketplace: str = DEFAULT_MARKET,
) -> ProductReturn:
    """写入或更新一台。表级 CHECK 是唯一防线，这里只负责给出可读的报错。

    为什么应用层也校验一遍「压力档必须高于基线」：
        不是为了防止绕过（CHECK 已经防住了），而是为了让失败信息能说人话——
        `IntegrityError: CHECK constraint failed: product_returns` 不会告诉
        用户「你把两个参数写倒了」。真正的守门仍在表上。
    """
    _check_kind(evidence_kind)
    if status not in (DRAFT, VERIFIED):
        raise ValueError(f"status 只能是 draft/verified，收到 {status!r}")
    if not 0 <= baseline_rate < 1:
        raise ValueError(f"基线退货率应在 [0, 1) 之间，收到 {baseline_rate!r}")
    if not baseline_rate < stress_rate < 1:
        raise ValueError(
            f"压力档 {stress_rate:.2%} 必须严格高于基线 {baseline_rate:.2%} 且小于 100%"
            "（两个参数可能写倒了；写倒的后果是永远为正的压测结论）"
        )
    if status == VERIFIED and evidence_kind not in VERIFIABLE_KINDS:
        raise ValueError(
            f"{evidence_kind} 不能升 verified：它描述的不是这一台的实际退货率。"
            "先拿到本 SKU 的读数或官方页面，并把 evidence_kind 改成 "
            f"{'/'.join(VERIFIABLE_KINDS)}"
        )

    conn.execute(
        "INSERT INTO product_returns "
        "(marketplace, sku_key, product_name, category, asin, baseline_rate, "
        " stress_rate, evidence_kind, source_note, effective_date, verified_at, "
        " status, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (marketplace, sku_key) DO UPDATE SET "
        " product_name = excluded.product_name, "
        " category = excluded.category, "
        " asin = excluded.asin, "
        " baseline_rate = excluded.baseline_rate, "
        " stress_rate = excluded.stress_rate, "
        " evidence_kind = excluded.evidence_kind, "
        " source_note = excluded.source_note, "
        " effective_date = excluded.effective_date, "
        " verified_at = excluded.verified_at, "
        " status = excluded.status, "
        " note = excluded.note",
        (
            marketplace,
            sku_key,
            product_name,
            category,
            asin,
            baseline_rate,
            stress_rate,
            evidence_kind,
            source_note,
            effective_date,
            verified_at,
            status,
            note,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    record = find(conn, sku_key, marketplace)
    assert record is not None  # 刚写完必然存在；断言失败说明 UNIQUE 没按预期工作
    return record
