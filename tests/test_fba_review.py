"""逐格复核工作台：导出可重复、空结论 ≠ 通过、复核人可追溯。

这组用的库是迁移后的真表（402 行全 verified），所以断言里的数量会随费率录入变化——
因此除「402」这一条锚点外，其余断言都写成相对关系（pending = 总数 − 已给结论），
不写死具体行数。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from xborder_insight.db import fba_review as rev
from xborder_insight.db.connection import schema_fingerprint

MARKET = "US"


@pytest.fixture
def cells(conn):
    return rev.load_cells(conn, MARKET)


def _write(path: Path, cells, *, verdict: str = "", reviewer: str = "", day: str = "",
           note: str = "", limit: int | None = None, row_id_override=None) -> Path:
    """按导出的列顺序写一张填好的表。limit 用于模拟「只填了一半」。"""
    chosen = cells if limit is None else cells[:limit]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(rev.COLUMNS)
        for cell in chosen:
            writer.writerow((
                row_id_override or cell.row_id,
                cell.version_key, cell.season, cell.effective_from, cell.effective_to,
                cell.size_tier, cell.product_class,
                f"{cell.weight_min_oz:g}",
                "" if cell.weight_max_oz is None else f"{cell.weight_max_oz:g}",
                cell.price_band, f"{cell.base_fee_usd:.2f}",
                f"{cell.per_increment_usd:.2f}",
                "" if cell.included_oz is None else f"{cell.included_oz:g}",
                "" if cell.increment_oz is None else f"{cell.increment_oz:g}",
                cell.status, cell.source_url or "",
                verdict, reviewer, day, note,
            ))
    return path


# ---------------------------------------------------------------- 导出


def test_export_covers_every_cell_and_is_byte_stable(cells, tmp_path):
    """402 格全在里面；同一次查询导出两次逐字节相同（行号不能是随机或序号的）。"""
    assert len(cells) == 402
    first = tmp_path / "a.csv"
    second = tmp_path / "b.csv"
    rev.write_sheet(first, cells)
    rev.write_sheet(second, cells)
    assert first.read_bytes() == second.read_bytes()


def test_row_ids_are_unique_and_independent_of_the_fee(cells):
    """改金额不换身份：复核结论挂在「格」上，不挂在「数」上。"""
    ids = [cell.row_id for cell in cells]
    assert len(set(ids)) == len(ids)
    base = cells[0]
    same = rev.cell_id(
        base.version_key, base.size_tier, base.product_class, base.price_band,
        base.weight_min_oz, base.weight_max_oz,
    )
    assert same == base.row_id
    other = rev.cell_id(
        base.version_key, base.size_tier, base.product_class, base.price_band,
        base.weight_min_oz + 1, base.weight_max_oz,
    )
    assert other != base.row_id


def test_export_does_not_write_to_the_database(conn, cells, tmp_path):
    before = schema_fingerprint(conn)
    count = conn.execute("SELECT COUNT(*) FROM fba_fee_schedule").fetchone()[0]
    rev.write_sheet(tmp_path / "a.csv", cells)
    rev.load_cells(conn, MARKET)
    assert schema_fingerprint(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM fba_fee_schedule").fetchone()[0] == count


# ---------------------------------------------------------------- 回收


def test_all_ok_passes(cells, tmp_path):
    path = _write(tmp_path / "ok.csv", cells, verdict="ok", reviewer="张三", day="2026-11-21")
    summary = rev.summarize(cells, rev.read_sheet(path))
    assert summary.exit_code == 0
    assert summary.ok == len(cells)
    assert summary.pending == 0
    assert summary.coverage == 1.0


def test_blank_verdict_is_pending_not_pass(cells, tmp_path):
    path = _write(tmp_path / "blank.csv", cells)
    summary = rev.summarize(cells, rev.read_sheet(path))
    assert summary.pending == len(cells)
    assert summary.exit_code == 3


def test_truncated_sheet_cannot_report_a_full_pass(cells, tmp_path):
    """只填前 100 行时，未复核数必须按「库里缺结论的格」算，而不是数空行。

    这是这张表最容易造成的假结论：截断的表如果按行数统计，会得到
    「ok 100 · 未复核 0」这样一句自相矛盾的通过。
    """
    path = _write(tmp_path / "half.csv", cells, verdict="ok", reviewer="张三",
                  day="2026-11-21", limit=100)
    summary = rev.summarize(cells, rev.read_sheet(path))
    assert summary.ok == 100
    assert summary.pending == len(cells) - 100
    assert summary.exit_code == 3


def test_mismatch_requires_a_note(cells, tmp_path):
    """说不出错在哪的 mismatch 没法修，只能制造一次返工。"""
    path = _write(tmp_path / "m.csv", cells, verdict="mismatch", reviewer="张三",
                  day="2026-11-21")
    summary = rev.summarize(cells, rev.read_sheet(path))
    assert summary.mismatch == 0
    assert summary.pending == len(cells)
    assert any("必须写备注" in p for p in summary.problems)
    assert summary.exit_code == 1


def test_mismatch_with_note_is_reported_with_its_cell(cells, tmp_path):
    path = _write(tmp_path / "m.csv", cells, verdict="mismatch", reviewer="张三",
                  day="2026-11-21", note="官方页 $10.21，库里 $10.07", limit=1)
    summary = rev.summarize(cells, rev.read_sheet(path))
    assert summary.mismatch == 1
    pairs = rev.mismatched_cells(cells, summary)
    assert len(pairs) == 1
    cell, row = pairs[0]
    assert cell.row_id == row.row_id
    assert "10.21" in row.note


def test_conclusion_without_reviewer_is_rejected(cells, tmp_path):
    """「402 格已复核」要能追到人。缺复核人/日期时结论不计入统计。"""
    path = _write(tmp_path / "n.csv", cells, verdict="ok", limit=1)
    summary = rev.summarize(cells, rev.read_sheet(path))
    assert summary.ok == 0
    assert any("缺复核人或复核日期" in p for p in summary.problems)


def test_bad_date_is_rejected(cells, tmp_path):
    path = _write(tmp_path / "d.csv", cells, verdict="ok", reviewer="张三",
                  day="11/21/2026", limit=1)
    summary = rev.summarize(cells, rev.read_sheet(path))
    assert any("YYYY-MM-DD" in p for p in summary.problems)


def test_unknown_row_id_is_flagged(cells, tmp_path):
    """表与库不是同一次导出时，安静地对不上比报错更危险。"""
    path = _write(tmp_path / "u.csv", cells, verdict="ok", reviewer="张三",
                  day="2026-11-21", limit=1, row_id_override="R0000000")
    summary = rev.summarize(cells, rev.read_sheet(path))
    assert summary.unknown_ids == ("R0000000",)
    assert summary.ok == 0
    assert summary.exit_code == 1


def test_chinese_alias_is_accepted(cells, tmp_path):
    """对着 Excel 填「对」比填 ok 顺手，两种写法都要认。"""
    path = _write(tmp_path / "c.csv", cells, verdict="对", reviewer="张三",
                  day="2026-11-21", limit=2)
    summary = rev.summarize(cells, rev.read_sheet(path))
    assert summary.ok == 2


def test_sheet_missing_a_required_column_is_rejected(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("行号,费率版本\nR1,us_2026_peak\n", encoding="utf-8-sig")
    with pytest.raises(ValueError) as exc:
        rev.read_sheet(path)
    assert "缺少列" in str(exc.value)


def test_export_order_follows_the_official_page_layout(cells):
    """402 格按官方页的表格结构排：一张表 = 期×尺寸段×类别，表内行是重量、列是价格带。

    这条不是为了好看。人核对时是从上往下逐行读的，如果同一个尺寸段的行被
    价格带打散成三段，就得在三张官方表之间来回跳，402 格没有做完的可能。
    """
    groups: list[tuple[str, str, str]] = []
    for cell in cells:
        key = (cell.version_key, cell.size_tier, cell.product_class)
        if not groups or groups[-1] != key:
            assert key not in groups, f"分组 {key} 不连续——同一张官方表被拆成了两段"
            groups.append(key)

    band_rank = {"under_10": 0, "10_to_50": 1, "over_50": 2}
    previous: tuple[tuple[str, str, str], tuple[float, int]] | None = None
    for cell in cells:
        key = (cell.version_key, cell.size_tier, cell.product_class)
        current = (cell.weight_min_oz, band_rank[cell.price_band])
        if previous is not None and previous[0] == key:
            assert current >= previous[1], f"{key} 内顺序倒退：{previous[1]} → {current}"
        previous = (key, current)


def test_weight_rows_come_before_price_columns(cells):
    """价格带是「列」不是「行」：同一重量档的三条价格带必须相邻。

    库里 text 排序会把 over_50 排在 10_to_50 前面（字母序 '1' < 'o'），
    那样同一行的三个价格带会被拆开，核对时等于把一张表切成三张。
    """
    same_weight = [
        cell for cell in cells
        if (cell.version_key, cell.size_tier, cell.product_class, cell.weight_min_oz)
        == (cells[0].version_key, cells[0].size_tier, cells[0].product_class,
            cells[0].weight_min_oz)
    ]
    assert [cell.price_band for cell in same_weight] == ["under_10", "10_to_50", "over_50"]


def test_export_order_follows_the_official_page_layout(cells):
    """402 格按官方页的表格结构排：一张表 = 期×尺寸段×类别，表内行是重量、列是价格带。

    这条不是为了好看。人核对时是从上往下逐行读的，如果同一个尺寸段的行被
    价格带打散成三段，就得在三张官方表之间来回跳，402 格没有做完的可能。
    """
    groups: list[tuple[str, str, str]] = []
    for cell in cells:
        key = (cell.version_key, cell.size_tier, cell.product_class)
        if not groups or groups[-1] != key:
            assert key not in groups, f"分组 {key} 不连续——同一张官方表被拆成了两段"
            groups.append(key)

    band_rank = {"under_10": 0, "10_to_50": 1, "over_50": 2}
    previous: tuple[tuple[str, str, str], tuple[float, int]] | None = None
    for cell in cells:
        key = (cell.version_key, cell.size_tier, cell.product_class)
        current = (cell.weight_min_oz, band_rank[cell.price_band])
        if previous is not None and previous[0] == key:
            assert current >= previous[1], f"{key} 内顺序倒退：{previous[1]} → {current}"
        previous = (key, current)


def test_weight_rows_come_before_price_columns(cells):
    """价格带是「列」不是「行」：同一重量档的三条价格带必须相邻。

    库里 text 排序会把 over_50 排在 10_to_50 前面（字母序 '1' < 'o'），
    那样同一行的三个价格带会被拆开，核对时等于把一张表切成三张。
    """
    same_weight = [
        cell for cell in cells
        if (cell.version_key, cell.size_tier, cell.product_class, cell.weight_min_oz)
        == (cells[0].version_key, cells[0].size_tier, cells[0].product_class,
            cells[0].weight_min_oz)
    ]
    assert [cell.price_band for cell in same_weight] == ["under_10", "10_to_50", "over_50"]
