"""运输方式判定：含锂货与渠道的组合必须能拦住非法的那几种。

这组用例守的是一条**结构**约束，不是一个数值：
「含锂 + 普货空运」在现实里发不出去，但在没有这一维的系统里，
它会一路算到毛利率才结束，且全程不报错。所以这里断言的是 ok/不 ok，
而不是某个数字——数字由货代报价单给，不由代码给。
"""

from __future__ import annotations

import pytest

from xborder_insight.domain import transport as tr


def test_lithium_on_general_air_is_refused():
    result = tr.check("air_general", has_lithium_battery=True)
    assert result.ok is False
    assert result.reason is not None
    assert "伪报" in result.reason
    # 不可发的组合不该顺手列出「要补哪些文件」——补了也发不出去
    assert result.required_docs == ()


def test_lithium_on_battery_air_is_allowed_with_three_docs():
    result = tr.check("air_battery", has_lithium_battery=True)
    assert result.ok is True
    assert len(result.required_docs) == 3
    joined = " ".join(result.required_docs)
    assert "UN38.3" in joined and "MSDS" in joined and "鉴定书" in joined


def test_lithium_on_sea_is_allowed():
    result = tr.check("sea", has_lithium_battery=True)
    assert result.ok is True
    assert result.required_docs


def test_general_cargo_on_battery_air_is_allowed_but_warns_about_premium():
    """普货走带电渠道不是错误，是白花钱；不能报错，也不能一声不响。"""
    result = tr.check("air_battery", has_lithium_battery=False)
    assert result.ok is True
    assert result.required_docs == ()
    assert any("溢价" in w for w in result.warnings)


def test_general_cargo_on_general_air_has_no_docs_and_no_warnings():
    result = tr.check("air_general", has_lithium_battery=False)
    assert result.ok is True
    assert result.required_docs == ()
    assert result.warnings == ()


def test_lithium_verdict_comes_from_bom_not_usage_mode():
    """含锂货任何时候都该带上这条提示：双模产品按插电款申报是常见错法。"""
    result = tr.check("air_battery", has_lithium_battery=True)
    assert any("BOM" in w for w in result.warnings)


def test_unknown_mode_is_an_input_error_not_a_pass():
    with pytest.raises(tr.UnknownTransportMode):
        tr.check("teleport", has_lithium_battery=False)


def test_quote_fields_pin_what_the_freight_quote_must_contain():
    """报价单缺字段就没法落库，这条清单必须稳定。"""
    assert len(tr.QUOTE_FIELDS) == 5
    result = tr.check("air_battery", has_lithium_battery=True)
    assert result.quote_fields == tr.QUOTE_FIELDS


def test_status_is_draft_because_the_clause_is_not_verified():
    """PI967 口径是行业转述，未经 IATA 原文核实——状态必须是 draft，不能装成 verified。"""
    assert tr.TRANSPORT_MODES["air_battery"].status == "draft"
    assert tr.check("air_battery", has_lithium_battery=True).as_dict()["status"] == "draft"
