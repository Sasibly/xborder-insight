"""`db.py --help` 的守门测试。

2026-11-23 发现顶层 `--help` 整页崩在 ValueError：子命令 `screen` 的说明写着
「15% 毛利线」，argparse 会用 % 格式化 help 文本，于是 `% 毛` 被当成格式符
（`unsupported format character '?' (0x6bdb)`，0x6bdb 正是「毛」）。写 `%%` 才对。

为什么值得专门测：这条命令是执行侧的入口，崩了不报错、只抛 traceback，
人第一反应会以为是环境坏了，而不是一行 help 文案。两层都要测——

- 顶层 `--help` 只渲染子命令的**一行说明**；
- `子命令 --help` 才渲染**参数级 help**，只测顶层会漏掉后者。
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

DB = Path(__file__).resolve().parent.parent / "scripts" / "db.py"

#: 手写清单，而不是从 help 里抓——抓来的清单在 help 崩掉时会变成空表，
#: 于是整组用例静默跳过，正是最该报错的时候不报错。
SUBCOMMANDS = (
    "up",
    "down",
    "status",
    "roundtrip",
    "params",
    "fba",
    "fba-envelope",
    "transport",
    "screen",
    "fba-profile",
    "reset",
    "fba-export",
    "fba-review",
)


def _help(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DB), *argv],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_top_level_help_renders():
    """顶层 help 渲染失败时 returncode 是 1、stderr 是 traceback。"""
    proc = _help("--help")
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "screen" in proc.stdout


def test_subcommand_list_matches_the_hand_written_one():
    """新增子命令要同步上面的清单，否则它永远不会被下面那组用例覆盖。"""
    proc = _help("--help")
    assert proc.returncode == 0, proc.stderr[-800:]
    listed = re.search(r"\{([a-z0-9,\-]+)\}", proc.stdout)
    assert listed is not None, "usage 行里没找到子命令列表，help 格式可能改了"
    assert set(listed.group(1).split(",")) == set(SUBCOMMANDS)


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_each_subcommand_help_renders(name: str):
    """参数级 help：`%` 写错的那些文案只有这一层会暴露。"""
    proc = _help(name, "--help")
    assert proc.returncode == 0, f"{name} --help 崩了：{proc.stderr[-800:]}"
    assert "usage:" in proc.stdout
