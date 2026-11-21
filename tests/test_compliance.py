"""合规三态回归测试。

核心不是「能不能识别 blocker」，而是**「不知道」会不会被说成「没问题」**。
缺失组合静默通过是合规场景里最贵的错误：上架被拒、货物被扣，
而系统当初给的是绿灯。
"""

from __future__ import annotations

from xborder_insight.domain.compliance import (
    BLOCKER,
    STATUS_BLOCKED,
    STATUS_CLEAR,
    STATUS_UNKNOWN,
    WARNING,
    RuleHit,
    evaluate,
    load,
)

CAT, MARKET = "wireless_charger", "US"


def _hit(key: str, severity: str, status: str) -> RuleHit:
    return RuleHit(
        rule_key=key,
        requirement=f"{key} 的要求说明",
        severity=severity,
        status=status,
        source_url="https://example.gov/x" if status == "verified" else None,
    )


# ---------- 缺失组合：必须 unknown，不能 clear ----------


def test_no_rules_is_unknown_not_clear():
    """规则表没覆盖这个品类 × 市场时，返回 unknown。

    如果这里返回 clear，「我们没查过」就会变成「没问题」——
    这是合规系统里唯一不可接受的静默降级。
    """
    result = evaluate([], "unknown_category", MARKET)
    assert result.status == STATUS_UNKNOWN
    assert "未覆盖" in result.reason
    assert not result.blockers


# ---------- blocked ----------


def test_verified_blocker_blocks():
    result = evaluate([_hit("us.radio", BLOCKER, "verified")], CAT, MARKET)
    assert result.status == STATUS_BLOCKED
    assert len(result.blockers) == 1
    assert not result.has_unverified_blocker


def test_draft_blocker_also_blocks_but_is_flagged():
    """未核实的 blocker 同样阻断，但必须能看出「这条还没核实」。

    差别在于叙事：已核实是「确定不能做」，未核实是「可能不能做，需核实」。
    两者都阻断当前决策，但不能混为一谈。
    """
    result = evaluate([_hit("us.radio", BLOCKER, "draft")], CAT, MARKET)
    assert result.status == STATUS_BLOCKED
    assert result.has_unverified_blocker is True


# ---------- clear 的三道条件 ----------


def test_all_verified_warnings_only_is_clear():
    result = evaluate(
        [_hit("industry.qi", WARNING, "verified"), _hit("us.labeling", WARNING, "verified")],
        CAT,
        MARKET,
    )
    assert result.status == STATUS_CLEAR
    assert result.draft_count == 0


def test_draft_warning_prevents_clear():
    """没有阻断项，但有规则没核实 → 仍是 unknown。

    「没查到阻断项」和「确认没有阻断项」是两件事。
    把前者说成后者，等于用「暂时没看见」冒充「不存在」。
    """
    result = evaluate([_hit("industry.qi", WARNING, "draft")], CAT, MARKET)
    assert result.status == STATUS_UNKNOWN
    assert "尚未核实" in result.reason


# ---------- 与真实规则表的集成 ----------


def test_seed_rules_produce_expected_state(conn):
    """当前种子：4 条规则，1 条已核实（FCC Part 18 + SDoC）、3 条 draft，含 2 条 blocker。

    仍然 blocked——核实一条 blocker 不等于解除阻断。`clear` 要求**全部**规则
    已核实，这是有意的：部分核实不等于没有问题。
    这条同时锁住一个现实：只要还有未核实的 blocker，这个品类就拿不到 GO。
    """
    result = load(conn, CAT, MARKET)

    assert result.status == STATUS_BLOCKED
    assert len(result.blockers) == 2
    assert result.verified_count == 1
    assert result.draft_count == 3
    assert sum(1 for hit in result.blockers if not hit.verified) == 1


def test_unknown_category_from_db(conn):
    """库里查不到的组合，必须走 unknown 分支而不是空列表当通过。"""
    result = load(conn, "no_such_category", MARKET)
    assert result.status == STATUS_UNKNOWN


def test_dict_shape_is_stable_for_api(conn):
    payload = load(conn, CAT, MARKET).as_dict()
    assert set(payload) >= {
        "status",
        "category",
        "marketplace",
        "blockers",
        "warnings",
        "verified_rules",
        "draft_rules",
        "reason",
    }
    assert isinstance(payload["blockers"], list)
