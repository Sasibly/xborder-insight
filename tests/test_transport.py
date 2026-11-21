"""运输方式判定：含锂货与渠道的组合必须能拦住非法的那几种。

这组用例守的是**结构**约束，不是数值：
「含锂 + 普货空运」在现实里发不出去，但在没有这一维的系统里，
它会一路算到毛利率才结束，且全程不报错。所以这里断言的是结论的三种取值
（ok / blocked / need_info），而不是某个数字——数字由货代报价单给，不由代码给。

第二组守的是「电池形态不能有乐观默认值」：
旧实现把 PI967 写死在 basis 里，等于把「没确认」当成「确认是内置」。
空运的结论会随形态从「可客机腹舱」翻成「客机禁运」，所以未确认时必须 need_info。
"""

from __future__ import annotations

import pytest

from xborder_insight.domain import transport as tr


def test_lithium_on_general_air_is_refused():
    result = tr.check("air_general", has_lithium_battery=True)
    assert result.ok is False
    assert result.verdict == tr.VERDICT_BLOCKED
    assert result.reason is not None
    assert "伪报" in result.reason
    # 不可发的组合不该顺手列出「要补哪些文件」——补了也发不出去
    assert result.required_docs == ()


def test_lithium_on_battery_air_with_built_in_battery_needs_three_docs():
    result = tr.check(
        "air_battery", has_lithium_battery=True, battery_installation="built_in",
    )
    assert result.ok is True
    assert result.installation is not None
    assert result.installation.packing_instruction == "PI967"
    assert len(result.required_docs) == 3
    joined = " ".join(result.required_docs)
    assert "UN38.3" in joined and "MSDS" in joined and "鉴定书" in joined
    # 内置不可拆不需要 DGD 与危包证——多列这两项等于凭空加成本
    assert "DGD" not in joined
    assert "危包证" not in joined


def test_unknown_battery_installation_does_not_default_to_the_loosest_case():
    """含锂 + 空运但没说形态：不能给可发结论，也不能悄悄按 PI967 算。

    这是本轮的核心用例。同一个未知事实，默认按最宽松的 PI967 处理，
    在真实世界里等于货到机场被拒——而输出上没有任何痕迹。
    """
    result = tr.check("air_battery", has_lithium_battery=True)
    assert result.verdict == tr.VERDICT_NEED_INFO
    assert result.ok is False
    assert result.installation is None
    assert result.reason is not None
    for token in ("PI967", "PI966", "PI965"):
        assert token in result.reason
    # 文件清单按最严档给：宁可多备，不要订舱时才发现缺一份
    joined = " ".join(result.required_docs)
    assert "DGD" in joined and "危包证" in joined
    assert any("最严档" in w for w in result.warnings)


def test_removable_battery_adds_declaration_documents():
    """配套可拆 = PI966：多两样文件，且要提示排期。"""
    result = tr.check(
        "air_battery", has_lithium_battery=True, battery_installation="removable",
    )
    assert result.ok is True
    assert result.installation is not None
    assert result.installation.packing_instruction == "PI966"
    joined = " ".join(result.required_docs)
    assert "DGD" in joined and "危包证" in joined
    assert "UN38.3" in joined
    assert len(result.required_docs) == 5
    assert any("排期" in w for w in result.warnings)


def test_standalone_battery_cannot_use_the_passenger_air_channel():
    """纯电池 PI965 客机禁运：这条渠道本身不成立，不是「多备文件就能发」。"""
    result = tr.check(
        "air_battery", has_lithium_battery=True, battery_installation="standalone",
    )
    assert result.verdict == tr.VERDICT_BLOCKED
    assert result.ok is False
    assert result.reason is not None
    assert "客机禁运" in result.reason
    assert result.required_docs == ()


def test_sea_does_not_flip_on_unknown_installation_but_lists_strict_docs():
    """海运两种形态都发得出去，结论不翻转；文件按最严档列并说明可减。"""
    result = tr.check("sea", has_lithium_battery=True)
    assert result.ok is True
    assert result.verdict == tr.VERDICT_OK
    joined = " ".join(result.required_docs)
    assert "DGD" in joined
    assert any("结论不翻转" in w for w in result.warnings)


def test_sea_with_built_in_battery_drops_declaration_documents():
    result = tr.check("sea", has_lithium_battery=True, battery_installation="built_in")
    joined = " ".join(result.required_docs)
    assert "DGD" not in joined
    assert "UN38.3" in joined


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
    result = tr.check(
        "air_battery", has_lithium_battery=True, battery_installation="built_in",
    )
    assert any("BOM" in w for w in result.warnings)


def test_unknown_mode_is_an_input_error_not_a_pass():
    with pytest.raises(tr.UnknownTransportMode):
        tr.check("teleport", has_lithium_battery=False)


def test_unknown_installation_is_an_input_error_not_a_silent_fallback():
    with pytest.raises(tr.UnknownBatteryInstallation):
        tr.check("air_battery", has_lithium_battery=True, battery_installation="glued")


def test_quote_fields_pin_what_the_freight_quote_must_contain():
    """报价单缺字段就没法落库，这条清单必须稳定。"""
    assert len(tr.QUOTE_FIELDS) == 5
    result = tr.check(
        "air_battery", has_lithium_battery=True, battery_installation="built_in",
    )
    assert result.quote_fields == tr.QUOTE_FIELDS


def test_status_is_draft_because_the_clause_is_not_verified():
    """PI967 口径是行业转述，未经 IATA 原文核实——状态必须是 draft，不能装成 verified。"""
    assert tr.TRANSPORT_MODES["air_battery"].status == "draft"
    result = tr.check(
        "air_battery", has_lithium_battery=True, battery_installation="built_in",
    )
    assert result.as_dict()["status"] == "draft"


def test_as_dict_carries_the_packing_instruction():
    """下游（报价单、接口）要能读到 PI 条款，不能只有一句人话。"""
    result = tr.check(
        "air_battery", has_lithium_battery=True, battery_installation="removable",
    )
    payload = result.as_dict()
    assert payload["battery_installation"] == "removable"
    assert payload["packing_instruction"] == "PI966"
    assert payload["verdict"] == tr.VERDICT_OK
    assert payload["ok"] is True
