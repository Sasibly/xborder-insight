"""0002：成本参数表与合规规则表。

这两张表存在的唯一目的，是让「凭印象填数」在工程上变成不可能。

硬约束（写在 CHECK 里，不是写在文档里）：
    status = 'verified'  ⇒  value + source_url + effective_date + verified_at 全部非空

也就是说，一个数字如果想被业务代码使用，它必须同时说清「是多少」「谁说的」
「什么时候生效」「我什么时候核实的」。缺任何一项，数据库直接拒绝写入。

为什么不预置数值：
    佣金率、FBA 费、认证要求都随类目、时效、政策变化，且必须来自官方文档。
    凭记忆填一个「大概 15%」会制造 ②类错误——系统正常返回、结论是错的，
    这种错误在页面上完全看不出来，比报错危险得多。
    因此种子数据只声明「需要核实哪些参数」，value 全部留空，status 一律 draft。

诚实的成本：P2 的利润模型在参数补齐前算不出结论。这是有意接受的代价——
算不出来会报错，算错了没人知道。
"""

from __future__ import annotations

import sqlite3

COST_PARAMS = """
CREATE TABLE cost_params (
    id              INTEGER PRIMARY KEY,
    param_key       TEXT NOT NULL,
    marketplace     TEXT NOT NULL,
    category        TEXT,
    unit            TEXT NOT NULL
                    CHECK (unit IN ('ratio', 'usd', 'usd_per_unit', 'usd_per_cubic_foot')),
    value           REAL,
    source_url      TEXT,
    effective_date  TEXT,
    verified_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'verified')),
    note            TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (param_key, marketplace),
    CHECK (
        status <> 'verified' OR (
            value IS NOT NULL AND source_url IS NOT NULL
            AND effective_date IS NOT NULL AND verified_at IS NOT NULL
        )
    )
)
"""

COMPLIANCE_RULES = """
CREATE TABLE compliance_rules (
    id              INTEGER PRIMARY KEY,
    category        TEXT NOT NULL,
    marketplace     TEXT NOT NULL,
    rule_key        TEXT NOT NULL,
    requirement     TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('blocker', 'warning')),
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'verified')),
    source_url      TEXT,
    effective_date  TEXT,
    verified_at     TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (category, marketplace, rule_key),
    CHECK (
        status <> 'verified' OR (
            source_url IS NOT NULL
            AND effective_date IS NOT NULL AND verified_at IS NOT NULL
        )
    )
)
"""

COST_INDEXES: dict[str, str] = {
    "idx_cost_status": "CREATE INDEX idx_cost_status ON cost_params (status, marketplace)",
}
COMPLIANCE_INDEXES: dict[str, str] = {
    "idx_compliance_scope": "CREATE INDEX idx_compliance_scope "
                            "ON compliance_rules (category, marketplace, status)",
}

CREATED_AT = "2026-11-17T00:00:00+08:00"  # 迁移内不使用 now()，保证可重复执行

# 只声明「要核实什么」，不声明「值是多少」
SEED_COST_PARAMS: tuple[tuple[str, str, str | None, str, str], ...] = (
    ("amazon.referral_rate", "US", "wireless_charger", "ratio",
     "需查 Amazon 官方费用表 Chargers & Power Adapters 类目的 referral fee"),
    ("amazon.fba_fulfillment_fee", "US", "wireless_charger", "usd_per_unit",
     "需查 Amazon FBA 官方费率表的尺寸分段；先确定产品包装尺寸再归段"),
    ("amazon.storage_fee", "US", "wireless_charger", "usd_per_cubic_foot",
     "需查 Amazon 月度仓储费官方费率；旺季与非旺季费率不同"),
    ("logistics.inbound_freight", "US", "wireless_charger", "usd_per_unit",
     "头程运费无官方统一价，须以实际货代报价为准并记录报价单编号"),
    ("import.duty_rate", "US", "wireless_charger", "ratio",
     "需先确定 HTS 编码，再到 USITC 官方税率库查询；不得用二手攻略"),
    ("returns.rate", "US", "wireless_charger", "ratio",
     "退货率无官方公开值，须以卖家后台实际退货数据为准，或标注为假设值"),
    ("ads.acos_benchmark", "US", "wireless_charger", "ratio",
     "ACOS 无官方基准，属经营假设；若作为假设值使用，必须在报告中标注为假设"),
)

SEED_COMPLIANCE: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("wireless_charger", "US", "us.radio.equipment_authorization",
     "无线发射类设备进入美国市场需取得 FCC 设备授权；具体适用条款与测试要求需逐条核实",
     "blocker", "需核实 FCC 官方 equipment authorization 页面，确认无线充电器适用的 Part 与流程"),
    ("wireless_charger", "US", "us.safety.testing",
     "充电类产品可能被要求提供安全检测报告；是否强制、依据哪份标准需核实",
     "warning", "需核实是美国联邦强制、平台要求，还是仅买家要求——三者处理方式完全不同"),
    ("wireless_charger", "US", "us.labeling.fcc_id",
     "若适用 FCC 授权，产品或包装需按要求标注 FCC ID",
     "blocker", "需核实标注位置与格式要求；标注不合规是最常见的上架被拒原因"),
    ("wireless_charger", "US", "industry.qi_certification",
     "Qi 认证属 WPC 行业认证，非政府强制；是否作为平台或渠道准入条件需核实",
     "warning", "需区分「法规强制」与「平台准入」——两者都影响能否上架，但补救路径不同"),
)


def up(conn: sqlite3.Connection) -> None:
    conn.execute(COST_PARAMS)
    for statement in COST_INDEXES.values():
        conn.execute(statement)
    conn.execute(COMPLIANCE_RULES)
    for statement in COMPLIANCE_INDEXES.values():
        conn.execute(statement)

    conn.executemany(
        "INSERT INTO cost_params "
        "(param_key, marketplace, category, unit, value, source_url, effective_date, "
        " verified_at, status, note, created_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, 'draft', ?, ?)",
        [(key, market, category, unit, note, CREATED_AT)
         for key, market, category, unit, note in SEED_COST_PARAMS],
    )
    conn.executemany(
        "INSERT INTO compliance_rules "
        "(category, marketplace, rule_key, requirement, severity, status, "
        " source_url, effective_date, verified_at, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'draft', NULL, NULL, NULL, ?, ?)",
        [(category, market, rule_key, requirement, severity, note, CREATED_AT)
         for category, market, rule_key, requirement, severity, note in SEED_COMPLIANCE],
    )


def down(conn: sqlite3.Connection) -> None:
    for name in reversed(list(COMPLIANCE_INDEXES)):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS compliance_rules")
    for name in reversed(list(COST_INDEXES)):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS cost_params")
