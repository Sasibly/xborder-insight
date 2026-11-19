"""合规校验：用规则表，不用模型。

为什么不用 LLM 判断合规：
    合规是「有没有必须的认证」这种二值事实，不是需要语言理解的开放问题。
    让模型回答「这个品进美国要不要 FCC 认证」，它总能给出一段听起来专业的答案，
    而且答错的概率不低。规则表则不同——查不到就是查不到，不会编。

三态而非二态（这是本模块的核心）：
    blocked  —— 命中已核实的 blocker，确定不能做
    unknown  —— 规则表里没有这个「品类 × 市场」组合，或关键 blocker 尚未核实
    clear    —— 无 blocker，且 blocker 级规则都已核实

    「unknown」必须独立存在，不能并进 clear。缺失组合静默通过，
    等于把「我们不知道」说成「没问题」，这是合规场景里最不能犯的错。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

BLOCKER = "blocker"
WARNING = "warning"
VERIFIED = "verified"

STATUS_BLOCKED = "blocked"
STATUS_UNKNOWN = "unknown"
STATUS_CLEAR = "clear"


@dataclass(frozen=True)
class RuleHit:
    rule_key: str
    requirement: str
    severity: str
    status: str
    source_url: str | None

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_key": self.rule_key,
            "requirement": self.requirement,
            "severity": self.severity,
            "verified": self.verified,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class ComplianceCheck:
    status: str
    category: str
    marketplace: str
    blockers: tuple[RuleHit, ...]
    warnings: tuple[RuleHit, ...]
    verified_count: int
    draft_count: int
    reason: str

    @property
    def has_unverified_blocker(self) -> bool:
        return any(not hit.verified for hit in self.blockers)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "category": self.category,
            "marketplace": self.marketplace,
            "blockers": [hit.as_dict() for hit in self.blockers],
            "warnings": [hit.as_dict() for hit in self.warnings],
            "verified_rules": self.verified_count,
            "draft_rules": self.draft_count,
            "reason": self.reason,
        }


def _to_hit(row: sqlite3.Row) -> RuleHit:
    return RuleHit(
        rule_key=row["rule_key"],
        requirement=row["requirement"],
        severity=row["severity"],
        status=row["status"],
        source_url=row["source_url"],
    )


def evaluate(hits: list[RuleHit], category: str, marketplace: str) -> ComplianceCheck:
    """根据规则集合判定状态。纯函数，便于单独测试三态边界。"""
    blockers = tuple(hit for hit in hits if hit.severity == BLOCKER)
    warnings = tuple(hit for hit in hits if hit.severity == WARNING)
    verified_count = sum(1 for hit in hits if hit.verified)
    draft_count = len(hits) - verified_count

    if not hits:
        return ComplianceCheck(
            status=STATUS_UNKNOWN,
            category=category,
            marketplace=marketplace,
            blockers=(),
            warnings=(),
            verified_count=0,
            draft_count=0,
            reason=(
                f"规则表未覆盖「{category} × {marketplace}」，无法判断是否满足准入要求；"
                "缺失组合不视为通过"
            ),
        )

    if blockers:
        return ComplianceCheck(
            status=STATUS_BLOCKED,
            category=category,
            marketplace=marketplace,
            blockers=blockers,
            warnings=warnings,
            verified_count=verified_count,
            draft_count=draft_count,
            reason=(
                f"存在 {len(blockers)} 项阻断级要求"
                + ("（含未核实项，需核实后才能确认是否真的无法满足）" if draft_count else "")
            ),
        )

    if draft_count:
        # 没有 blocker，但有规则没核实。此时不能宣称 clear——
        # 「没查到阻断项」和「确认没有阻断项」是两件事。
        return ComplianceCheck(
            status=STATUS_UNKNOWN,
            category=category,
            marketplace=marketplace,
            blockers=(),
            warnings=warnings,
            verified_count=verified_count,
            draft_count=draft_count,
            reason=f"{draft_count} 条规则尚未核实到官方来源，结论只能是「暂未发现阻断项」",
        )

    return ComplianceCheck(
        status=STATUS_CLEAR,
        category=category,
        marketplace=marketplace,
        blockers=(),
        warnings=warnings,
        verified_count=verified_count,
        draft_count=0,
        reason="无阻断级要求，且全部规则已核实",
    )


def load(
    conn: sqlite3.Connection, category: str, marketplace: str
) -> ComplianceCheck:
    """从规则表读取并判定。draft 规则也参与判定，原因见模块 docstring。"""
    rows = conn.execute(
        "SELECT * FROM compliance_rules WHERE category = ? AND marketplace = ? "
        "ORDER BY severity DESC, rule_key",
        (category, marketplace),
    ).fetchall()
    return evaluate([_to_hit(row) for row in rows], category, marketplace)
