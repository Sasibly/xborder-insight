"""runbook 与代码的一致性。

文档漂移不能靠自觉：这几条测试让「改了错误码却忘了改 runbook」变成红灯，
而不是等出事那天才发现手册上写着一个已经不存在的码。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from xborder_insight import errors

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "runbook.md"


def _read() -> str:
    assert RUNBOOK.exists(), "docs/runbook.md 不存在（docs/02 P4-4 的交付物）"
    return RUNBOOK.read_text(encoding="utf-8")


def _codes_in_code() -> dict[str, int]:
    """从 errors.py 反射出 {code: status_code}，不手写清单。

    手写一份清单做对比，等于让测试也参与漂移——新增错误码时清单不会自己变。
    """
    found: dict[str, int] = {}
    for obj in vars(errors).values():
        if not isinstance(obj, type) or not issubclass(obj, errors.AppError):
            continue
        if obj is errors.AppError:
            continue
        # 子类可能共享 code（如 429 的两个码各不同），一旦重复说明有类忘了声明
        assert obj.code not in found, f"{obj.__name__} 与已有类共用 code={obj.code}"
        found[obj.code] = obj.status_code
    assert found, "errors 模块里没有可识别的 AppError 子类"
    return found


def _codes_in_runbook() -> dict[str, int]:
    text = _read()
    begin, end = "<!-- error-codes:begin -->", "<!-- error-codes:end -->"
    assert begin in text and end in text, "错误码表的起止标记被删了，测试无法定位"
    block = text.split(begin, 1)[1].split(end, 1)[0]

    rows: dict[str, int] = {}
    for line in block.splitlines():
        match = re.match(r"\|\s*`([a-z_]+)`\s*\|\s*(\d{3})\s*\|", line)
        if match:
            rows[match.group(1)] = int(match.group(2))
    assert rows, "错误码表没解析出任何行（表格格式变了？）"
    return rows


def test_runbook_lists_every_error_code_with_the_right_status():
    """附录 A 必须与 errors.py 一一对应：不多、不少、状态码一致。"""
    in_code = _codes_in_code()
    in_doc = _codes_in_runbook()

    missing = sorted(set(in_code) - set(in_doc))
    extra = sorted(set(in_doc) - set(in_code))
    assert not missing, f"这些错误码在代码里存在，但 runbook 附录 A 没写：{missing}"
    assert not extra, f"runbook 附录 A 写了代码里不存在的错误码：{extra}"

    wrong = {k: (in_doc[k], in_code[k]) for k in in_code if in_doc[k] != in_code[k]}
    assert not wrong, f"状态码与代码不一致（runbook, 代码）：{wrong}"


def test_runbook_only_references_existing_make_targets():
    """写进 runbook 的 `make xxx` 必须真的存在。

    这个坑踩过：文档里写了 `db.py profit`，但那条命令是刻意不加的
    （会邀请人走最乐观的路径）。手册里出现一条不存在的命令，值班的人会先怀疑自己。
    """
    text = _read()
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    defined = set(re.findall(r"^([a-z][a-z0-9-]*):", makefile, re.MULTILINE))

    referenced = set(re.findall(r"\bmake\s+([a-z][a-z0-9-]*)", text))
    unknown = sorted(referenced - defined)
    assert not unknown, f"runbook 里引用了 Makefile 中不存在的目标：{unknown}"


def test_runbook_only_references_existing_scripts():
    """`scripts/xxx.py` 同理：路径写错比命令不存在更难发现（会报「文件找不到」）。"""
    text = _read()
    referenced = set(re.findall(r"scripts/([a-z0-9_]+)\.py", text))
    unknown = sorted(name for name in referenced if not (ROOT / "scripts" / f"{name}.py").exists())
    assert not unknown, f"runbook 里引用了不存在的脚本：{unknown}"


@pytest.mark.parametrize("endpoint", ["/health", "/ready", "/metrics"])
def test_three_ops_endpoints_are_documented(endpoint):
    """三个运维端点各有分工，runbook 第 0 节必须逐个写清，不能只提一个。"""
    assert endpoint in _read(), f"runbook 没提 {endpoint}"
