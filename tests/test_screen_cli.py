"""`db.py screen` 的入口层默认值（H24）。

模型层不给默认值之后，「没给」必须是一个能被表达、被看见的状态。所以默认值
只能活在入口层，而入口层必须把它打印出来——否则「按 6.00 算的」和
「按你给的 6.00 算的」在屏幕上长得一模一样，而谈判桌上这两句话的分量不同：
一句是假设，一句是已经议定的数。

这三条测的是同一件事的三个面：

1. 不给 `--freight` / `--returns` → 用默认值，并在口径行标「默认」；
2. 给了 → 标「指定」，且数字是给的数（不是默认值）；
3. 批量模式里默认值被**显式注入**每一条候选——不注入的话，`parse_rows`
   产出的 `Candidate` 不带这两项，整批会静默变成「上界」。
"""

import subprocess
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "scripts" / "db.py"

#: 咖啡机：完整口径下 7.90%（FAIL）。样本固定是为了让断言能直接写在数字上。
COFFEE = ("--price", "63.99", "--cogs", "18.50", "--duty", "0.237", "--fba", "14.40")


def _screen(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DB), "screen", *argv],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_missing_calibration_items_stop_before_target_prices():
    """不给 `--freight` / `--returns`：毛利是上界、结论「还判不了」、目标价一个都不给。

    这是 H24 的核心行为。以前这两个参数有默认值 6.00 / 8%，屏幕上照样印出
    7.90% 和一个 $14.90；现在缺就是缺，26.73% 会明确标成「上界」，
    读者不会把它记成结论。
    """
    proc = _screen(*COFFEE)
    assert proc.returncode == 3, proc.stdout + proc.stderr[-500:]  # NEED_INFO
    out = proc.stdout
    assert "头程 未给" in out
    assert "毛利 26.73% 是上界" in out
    assert "缺 退货率" in out and "缺 头程运费" in out
    assert "$14.90" not in out, "上界口径下不该出现目标价"


def test_full_calibration_gives_target_prices():
    """给齐两个口径参数：7.90%、做不成、目标价 $14.90 出现，且标「指定」。"""
    proc = _screen(*COFFEE, "--freight", "6.00", "--returns", "0.08")
    assert proc.returncode == 1, proc.stdout + proc.stderr[-500:]
    out = proc.stdout
    assert "毛利 7.90%" in out
    assert "采购必须 ≤ $14.90" in out
    assert "--freight 指定" in out and "--returns 指定" in out
    assert "是上界" not in out


def test_batch_injects_the_cli_defaults(tmp_path):
    """批量模式必须把口径注入每一条候选，否则整批变「上界」。

    `parse_rows` 只产出 CSV 里有的列（售价/采购/关税/FBA），头程与退货率
    由入口层补。若哪天有人把这个注入删掉，批量结果会从「做不成」变成
    「上界·还判不了」——看起来像数据没准备好，实际是代码漏了一步。
    """
    sheet = tmp_path / "candidates.csv"
    sheet.write_text(
        "name,price,cogs,duty,fba\n"
        "works,60.00,5.00,0.125,9.00\n"
        "fails,63.99,18.50,0.237,14.40\n",
        encoding="utf-8",
    )
    proc = _screen("--csv", str(sheet))
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr[-500:]
    out = proc.stdout
    assert "上界·" not in out, "批量里出现了上界：口径没有注入到每条候选"
    assert "口径：" in out
    assert "批量是**粗筛**口径" in out
