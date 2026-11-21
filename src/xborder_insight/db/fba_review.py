"""402 格费率表的逐格复核工作台：只导出与回收，**不写库**。

为什么需要它：`fba_fee_schedule` 的 402 行全部是 `verified`，但 `verified` 的
CHECK 只证明了「来源 + 生效日期 + 核实日期齐全」，证明不了「有人拿官方页逐格对过」。
`docs/07` H12 说的就是这件事。复核要能进行，先得有一张能填的表——一格里五维
（版本 / 尺寸段 / 类别 / 重量档 / 价格带），人不可能对着屏幕逐条拼。

三个设计决定：

1. 【导出必须可重复，且导出即身份】
   `row_id` 由五维键的 sha1 前 8 位决定，不含时间、不含行号。同一库导出两次逐字节
   相同；复核表填完再导入，能对回原格。若将来重录导致某格重量区间变化，`row_id`
   会变——那时旧记录的复核结论确实不该沿用，因为那是另一格。

2. 【复核结果不写回数据库】
   往 `fba_fee_schedule` 加 `reviewed_by` 会诱使 `status` 变成「有人说他看过」，
   而库里的 CHECK 只能校验字段齐不齐，校验不了人。402 格还没核之前先改表结构，
   等于给一个尚未发生的复核预留了「已复核」的位置。所以结论留在 CSV 里：
   对外表述用「覆盖率 + mismatch 数」说话，而不是库里一个布尔。

3. 【空 ≠ 通过】
   `verdict` 留空 = 未复核，会被统计成 `pending` 并让退出码为 3（还不能定论）。
   只接受 `ok` / `mismatch` 两个结论（可写 `对` / `错`），中间态一律按格式错处理——
   「大概对」在复核里等于没核。`mismatch` 还必须写 `note`：说不出错在哪的 mismatch
   无法修，只能制造一次返工。
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# 复核表列名（中文，Excel 里直接看得懂；导出与回收共用这一份，避免两边漂移）
COL_ID = "行号"
COL_VERSION = "费率版本"
COL_SEASON = "时段"
COL_FROM = "生效起"
COL_TO = "生效止"
COL_TIER = "尺寸段"
COL_CLASS = "产品类别"
COL_WMIN = "重量起(oz)"
COL_WMAX = "重量止(oz)"
COL_BAND = "价格带"
COL_BASE = "基础费($)"
COL_STEP = "续重费($)"
COL_INCLUDED = "含首重(oz)"
COL_INCREMENT = "续重单位(oz)"
COL_STATUS = "状态"
COL_SOURCE = "官方来源"
COL_VERDICT = "复核结论"
COL_REVIEWER = "复核人"
COL_DATE = "复核日期"
COL_NOTE = "备注"

COLUMNS: tuple[str, ...] = (
    COL_ID, COL_VERSION, COL_SEASON, COL_FROM, COL_TO, COL_TIER, COL_CLASS,
    COL_WMIN, COL_WMAX, COL_BAND, COL_BASE, COL_STEP, COL_INCLUDED, COL_INCREMENT,
    COL_STATUS, COL_SOURCE, COL_VERDICT, COL_REVIEWER, COL_DATE, COL_NOTE,
)

OK = "ok"
MISMATCH = "mismatch"
PENDING = ""

# 只收这两组写法：英文原词 + 中文单字。再多就开始猜意图了。
_ALIASES = {
    "ok": OK, "对": OK, "一致": OK,
    "mismatch": MISMATCH, "错": MISMATCH, "不一致": MISMATCH,
}


@dataclass(frozen=True)
class RateCell:
    """费率表的一格。字段与库列一一对应，没有派生值。"""

    row_id: str
    version_key: str
    season: str
    effective_from: str
    effective_to: str
    size_tier: str
    product_class: str
    weight_min_oz: float
    weight_max_oz: float | None
    price_band: str
    base_fee_usd: float
    per_increment_usd: float
    included_oz: float | None
    increment_oz: float | None
    status: str
    source_url: str | None


@dataclass(frozen=True)
class SheetRow:
    """回收的一行。数值不参与判定——复核的是「金额对不对」，不是「金额是多少」。"""

    row_id: str
    verdict: str
    reviewer: str
    reviewed_at: str
    note: str


@dataclass(frozen=True)
class ReviewSummary:
    total_cells: int
    ok: int
    mismatch: int
    pending: int
    unknown_ids: tuple[str, ...]
    problems: tuple[str, ...]
    mismatches: tuple[SheetRow, ...]
    pending_ids: tuple[str, ...] = ()

    @property
    def reviewed(self) -> int:
        return self.ok + self.mismatch

    @property
    def coverage(self) -> float:
        return 0.0 if self.total_cells == 0 else self.reviewed / self.total_cells

    @property
    def exit_code(self) -> int:
        """0 = 全部复核且一致；1 = 有错或格式问题；3 = 还没核完。"""
        if self.problems or self.mismatch:
            return 1
        if self.pending:
            return 3
        return 0

    def as_dict(self) -> dict[str, object]:
        return {
            "total_cells": self.total_cells,
            "reviewed": self.reviewed,
            "ok": self.ok,
            "mismatch": self.mismatch,
            "pending": self.pending,
            "coverage": round(self.coverage, 4),
            "unknown_ids": list(self.unknown_ids),
            "problems": list(self.problems),
            "exit_code": self.exit_code,
        }


def cell_id(
    version_key: str,
    size_tier: str,
    product_class: str,
    price_band: str,
    weight_min_oz: float,
    weight_max_oz: float | None,
) -> str:
    """五维键 → 稳定短 ID。刻意不含金额：改金额是同一次复核的结论，不该换身份。"""
    key = "|".join(
        (
            version_key,
            size_tier,
            product_class,
            price_band,
            f"{weight_min_oz:g}",
            "" if weight_max_oz is None else f"{weight_max_oz:g}",
        )
    )
    return "R" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:7].upper()


# 排序与官方 Product size tiers 页的表格结构对齐：一张表 = 一个尺寸段 × 一个类别，
# 表内行是重量档、列是价格带。所以列序必须是「期 → 尺寸段 → 类别 → 重量 → 价格带」，
# 且价格带按金额从低到高（不能按文本：'10_to_50' < 'over_50' < 'under_10' 是字母序）。
# 排成这样，人才可以从上往下逐行核，而不是在三张表之间来回跳。
_TIER_ORDER = ("small_standard", "large_standard", "small_bulky", "large_bulky")
_CLASS_ORDER = ("non_apparel", "dangerous_goods")
_BAND_ORDER = ("under_10", "10_to_50", "over_50")


def _case(column: str, values: tuple[str, ...]) -> str:
    """生成 ORDER BY 用的 CASE 表达式。未知值排最后，不抛错——新增档位不该让导出失败。"""
    whens = " ".join(f"WHEN '{value}' THEN {index}" for index, value in enumerate(values))
    return f"CASE {column} {whens} ELSE {len(values)} END"


def load_cells(conn: sqlite3.Connection, marketplace: str = "US") -> tuple[RateCell, ...]:
    """按官方页的表格顺序导出，保证同一份库导出逐字节可重复。"""
    rows = conn.execute(
        f"""
        SELECT f.version_key, v.season, v.effective_from, v.effective_to,
               f.size_tier, f.product_class, f.weight_min_oz, f.weight_max_oz,
               f.price_band, f.base_fee_usd, f.per_increment_usd,
               f.included_oz, f.increment_oz, f.status,
               COALESCE(f.source_url, v.source_url) AS source_url
          FROM fba_fee_schedule AS f
          JOIN fba_rate_version AS v ON v.version_key = f.version_key
         WHERE f.marketplace = ?
         ORDER BY v.effective_from,
                  {_case('f.size_tier', _TIER_ORDER)},
                  {_case('f.product_class', _CLASS_ORDER)},
                  f.weight_min_oz,
                  {_case('f.price_band', _BAND_ORDER)}
        """,
        (marketplace,),
    ).fetchall()
    return tuple(
        RateCell(
            row_id=cell_id(
                row["version_key"],
                row["size_tier"],
                row["product_class"],
                row["price_band"],
                row["weight_min_oz"],
                row["weight_max_oz"],
            ),
            version_key=row["version_key"],
            season=row["season"],
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            size_tier=row["size_tier"],
            product_class=row["product_class"],
            weight_min_oz=row["weight_min_oz"],
            weight_max_oz=row["weight_max_oz"],
            price_band=row["price_band"],
            base_fee_usd=row["base_fee_usd"],
            per_increment_usd=row["per_increment_usd"],
            included_oz=row["included_oz"],
            increment_oz=row["increment_oz"],
            status=row["status"],
            source_url=row["source_url"],
        )
        for row in rows
    )


def _num(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def _money(value: float) -> str:
    return f"{value:.2f}"


def write_sheet(path: Path, cells: tuple[RateCell, ...]) -> None:
    """写复核表。结论三列留空由人填；其余列是待核对的快照。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        for cell in cells:
            writer.writerow(
                (
                    cell.row_id,
                    cell.version_key,
                    cell.season,
                    cell.effective_from,
                    cell.effective_to,
                    cell.size_tier,
                    cell.product_class,
                    _num(cell.weight_min_oz),
                    _num(cell.weight_max_oz),
                    cell.price_band,
                    _money(cell.base_fee_usd),
                    _money(cell.per_increment_usd),
                    _num(cell.included_oz),
                    _num(cell.increment_oz),
                    cell.status,
                    cell.source_url or "",
                    "",
                    "",
                    "",
                    "",
                )
            )


def _parse_verdict(raw: str, where: str) -> tuple[str, str | None]:
    text = raw.strip()
    if not text:
        return PENDING, None
    mapped = _ALIASES.get(text.lower(), _ALIASES.get(text))
    if mapped is None:
        return PENDING, f"{where}：复核结论只接受 ok / mismatch（或 对 / 错），收到 {raw!r}"
    return mapped, None


def summarize(cells: tuple[RateCell, ...], rows: list[dict[str, str]]) -> ReviewSummary:
    """把回收的表与库里的格对账。返回的每一类问题都要能定位到具体一行。

    `pending` 由**库里缺结论的格**倒推，而不是数表里空了几行：
    表被截断（只填了前 200 行）时，数空行会得到 0 个未复核，
    于是「402 格全部通过」——这是这张表最容易造成的假结论。
    """
    by_id = {cell.row_id: cell for cell in cells}
    problems: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    ok = mismatch = 0
    mismatches: list[SheetRow] = []
    ok_ids: set[str] = set()
    mismatch_ids: set[str] = set()

    for index, raw in enumerate(rows, start=2):  # 第 1 行是表头
        row_id = (raw.get(COL_ID) or "").strip()
        if not row_id:
            problems.append(f"第 {index} 行：行号为空")
            continue
        if row_id not in by_id:
            unknown.append(row_id)
            problems.append(f"第 {index} 行：行号 {row_id} 在库里不存在（表与库不是同一次导出？）")
            continue
        if row_id in seen:
            problems.append(f"第 {index} 行：行号 {row_id} 重复")
            continue
        seen.add(row_id)

        verdict, error = _parse_verdict(raw.get(COL_VERDICT) or "", f"第 {index} 行")
        if error:
            problems.append(error)
            continue

        reviewer = (raw.get(COL_REVIEWER) or "").strip()
        reviewed_at = (raw.get(COL_DATE) or "").strip()
        note = (raw.get(COL_NOTE) or "").strip()

        if verdict is PENDING:
            if reviewer or reviewed_at or note:
                problems.append(
                    f"第 {index} 行：填了复核人/日期/备注却没写结论——空结论一律视为未复核，"
                    "请补 `ok` 或 `mismatch`"
                )
            continue

        # 结论必须能追溯到人，否则「402 格已复核」这句话无法验证
        if not reviewer or not reviewed_at:
            problems.append(f"第 {index} 行：给了结论但缺复核人或复核日期")
            continue
        try:
            date.fromisoformat(reviewed_at)
        except ValueError:
            problems.append(f"第 {index} 行：复核日期应为 YYYY-MM-DD，收到 {reviewed_at!r}")
            continue
        if verdict == MISMATCH and not note:
            problems.append(f"第 {index} 行：mismatch 必须写备注——说不出错在哪就没法修")
            continue

        if verdict == MISMATCH:
            mismatch += 1
            mismatch_ids.add(row_id)
            mismatches.append(SheetRow(row_id, verdict, reviewer, reviewed_at, note))
        else:
            ok += 1
            ok_ids.add(row_id)

    decided = ok_ids | mismatch_ids
    pending_ids = tuple(cell.row_id for cell in cells if cell.row_id not in decided)

    return ReviewSummary(
        total_cells=len(cells),
        ok=ok,
        mismatch=mismatch,
        pending=len(pending_ids),
        unknown_ids=tuple(unknown),
        problems=tuple(problems),
        mismatches=tuple(mismatches),
        pending_ids=pending_ids,
    )


def read_sheet(path: Path) -> list[dict[str, str]]:
    """读回复核表。缺列直接报错——少一列会让「未复核」被读成「已通过」。"""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = tuple(reader.fieldnames or ())
        missing = [name for name in (COL_ID, COL_VERDICT, COL_REVIEWER, COL_DATE, COL_NOTE)
                   if name not in fields]
        if missing:
            raise ValueError(f"复核表缺少列：{'、'.join(missing)}（请用 fba-export 重新导出）")
        return [dict(row) for row in reader]


def mismatched_cells(
    cells: tuple[RateCell, ...], summary: ReviewSummary
) -> tuple[tuple[RateCell, SheetRow], ...]:
    """把 mismatch 对回具体的格，供修数据时直接定位。"""
    by_id = {cell.row_id: cell for cell in cells}
    return tuple(
        (by_id[row.row_id], row) for row in summary.mismatches if row.row_id in by_id
    )
