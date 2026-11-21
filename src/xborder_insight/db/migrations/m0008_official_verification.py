"""0008：2026-11-21 的官方核实结果入库。

只改数据，不改结构。核实依据与踩到的坑写在 docs/04 与本文档，
不写进数据库——数据库只存结论，过程供人读。

本次核实的三条结论（一条 verified、两条补来源、一条补 note）：

1. **FCC 授权路径：Part 18 + SDoC，置 verified。**
   47 CFR 18.107(c)：ISM 设备 = "designed to generate and use locally RF energy
   for industrial, scientific, medical, domestic or similar purposes, excluding
   applications in the field of telecommunication"——无线充电正是把 RF 能量用于
   传能而非通信，落在这个定义里。
   FCC Equipment Authorization Procedures 页明列："Equipment that only contains
   digital circuitry ... such as ... consumer ISM equipment ... are subject to
   approval using the SDoC procedure or may optionally use the certification
   procedure."
   两条都是官方站点原文，链条完整，故置 verified。

2. **FCC ID 标注：补来源，保持 draft。**
   SDoC 设备不提交申请、不列入 FCC 数据库，因此不分配 FCC ID；但须随附符合性
   声明。这条对「纯 WPT 设备」成立，对「含独立蓝牙/Wi-Fi 模块」不成立——
   那部分按 Part 15 走 certification 并需 FCC ID。
   是否含独立发射机是**产品事实**，不是法规事实，规则表无法替产品回答，
   所以保持 draft：它能提示风险，但不能宣称「你家产品一定不需要」。

3. **进口关税：补 note，保持 draft。**
   HTS 8504.40.95 现行 general = Free、column 2 = 35%（USITC 查询，2026-11-21）。
   中国原产另加 9903.88.03「the duty provided in the applicable subheading + 25%」。
   这一条是明确的。但 USITC 出版物 pub5701 的脚注记录了 IEEPA 两层加征的演变
   （2025-11-10 起回落至 10%；另一层 2025-05-14 起 10%），且 USITC 首页提到
   2026 HTS Revision 13「reflects recently implemented additional tariffs under
   section 301」——是否覆盖 8504.40.95 未确认。
   有效税率因此存在 35% / 45% / 更高三种可能，填任一值都会让毛利失真。
   保持 draft，等 CBP 裁定或官方税率查询确认后再填。
"""

from __future__ import annotations

import sqlite3

VERIFIED_AT = "2026-11-21"
CATEGORY = "wireless_charger"
MARKET = "US"

# (rule_key, requirement, severity, status, source_url, effective_date, note)
RULE_UPDATES: tuple[tuple[str, str, str, str, str | None, str | None, str], ...] = (
    (
        "us.radio.equipment_authorization",
        "无线充电设备按 FCC Part 18 管理（47 CFR 18.107(c) 的 ISM 设备：产生并使用本地 RF 能量"
        "作非通信用途）。消费级设备经 SDoC 自我声明即可，无需向 FCC 或 TCB 提交申请，"
        "但须保留测试报告与符合性声明，FCC 索取时提供",
        "blocker",
        "verified",
        "https://www.fcc.gov/oet/ea/procedures.html",
        "2017-11-02",
        "ISM 定义见 47 CFR 18.107(c)：https://www.ecfr.gov/current/title-47/"
        "chapter-I/subchapter-A/part-18/subpart-A/section-18.107 。"
        "SDoC 适用于「只含数字电路、不含发射机」的 consumer ISM 设备，"
        "且允许改用 certification。核实日期 2026-11-21。",
    ),
    (
        "us.labeling.fcc_id",
        "SDoC 授权的设备不分配 FCC ID、不列入 FCC 数据库，须随附符合性声明与测试报告供追溯；"
        "若产品含独立有意发射机（如蓝牙/Wi-Fi 模块），该发射部分另按 Part 15 走 certification "
        "并需标注 FCC ID",
        "blocker",
        "draft",
        "https://www.fcc.gov/oet/ea/procedures.html",
        None,
        "两条路径的标注要求不同，取决于产品是否含独立发射机——这是产品事实，不是法规事实，"
        "规则表无法代答，故保持 draft。FCC 程序页原文：SDoC 设备「is not required to file an "
        "equipment authorization application」且「not listed in a Commission database」。",
    ),
    (
        "us.safety.testing",
        "充电类产品可能被要求提供安全检测报告；是否强制、依据哪份标准需核实",
        "warning",
        "draft",
        None,
        None,
        "需核实是美国联邦强制、平台要求，还是仅买家要求——三者处理方式完全不同",
    ),
    (
        "industry.qi_certification",
        "Qi 认证属 WPC 行业认证，非政府强制；是否作为平台或渠道准入条件需核实",
        "warning",
        "draft",
        None,
        None,
        "需区分「法规强制」与「平台准入」——两者都影响能否上架，但补救路径不同",
    ),
)

DUTY_KEY = "import.duty_rate"
DUTY_NOTE = (
    "HTS 8504.40.95：general Free、column 2 35%（USITC，核实于 2026-11-21）。"
    "中国原产另加 9903.88.03「applicable subheading + 25%」。"
    "但 IEEPA 加征层（USITC pub5701 脚注：2025-11-10 起 10%，另一层 2025-05-14 起 10%）"
    "与 2026 年 Section 301 新修订（HTS Revision 13）是否覆盖本编码均未确认，"
    "有效税率存在 35% / 45% / 更高三种可能，故不填值。详见 docs/04 第 3 节"
)

# down() 用的原始文本，逐字取自 m0002 的 SEED_COMPLIANCE
ORIGINAL_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        "us.radio.equipment_authorization",
        "无线发射类设备进入美国市场需取得 FCC 设备授权；具体适用条款与测试要求需逐条核实",
        "blocker",
        "需核实 FCC 官方 equipment authorization 页面，确认无线充电器适用的 Part 与流程",
    ),
    (
        "us.safety.testing",
        "充电类产品可能被要求提供安全检测报告；是否强制、依据哪份标准需核实",
        "warning",
        "需核实是美国联邦强制、平台要求，还是仅买家要求——三者处理方式完全不同",
    ),
    (
        "us.labeling.fcc_id",
        "若适用 FCC 授权，产品或包装需按要求标注 FCC ID",
        "blocker",
        "需核实标注位置与格式要求；标注不合规是最常见的上架被拒原因",
    ),
    (
        "industry.qi_certification",
        "Qi 认证属 WPC 行业认证，非政府强制；是否作为平台或渠道准入条件需核实",
        "warning",
        "需区分「法规强制」与「平台准入」——两者都影响能否上架，但补救路径不同",
    ),
)

ORIGINAL_DUTY_NOTE = (
    "需先确定 HTS 编码，再到 USITC 官方税率库查询；不得用二手攻略"
)


def up(conn: sqlite3.Connection) -> None:
    for key, requirement, severity, status, source_url, effective_date, note in RULE_UPDATES:
        conn.execute(
            "UPDATE compliance_rules SET requirement = ?, severity = ?, status = ?, "
            "source_url = ?, effective_date = ?, verified_at = ?, note = ? "
            "WHERE rule_key = ? AND category = ? AND marketplace = ?",
            (
                requirement,
                severity,
                status,
                source_url,
                effective_date,
                VERIFIED_AT if status == "verified" else None,
                note,
                key,
                CATEGORY,
                MARKET,
            ),
        )
    conn.execute(
        "UPDATE cost_params SET note = ? WHERE param_key = ? AND marketplace = ?",
        (DUTY_NOTE, DUTY_KEY, MARKET),
    )


def down(conn: sqlite3.Connection) -> None:
    for key, requirement, severity, note in ORIGINAL_RULES:
        conn.execute(
            "UPDATE compliance_rules SET requirement = ?, severity = ?, status = 'draft', "
            "source_url = NULL, effective_date = NULL, verified_at = NULL, note = ? "
            "WHERE rule_key = ? AND category = ? AND marketplace = ?",
            (requirement, severity, note, key, CATEGORY, MARKET),
        )
    conn.execute(
        "UPDATE cost_params SET note = ? WHERE param_key = ? AND marketplace = ?",
        (ORIGINAL_DUTY_NOTE, DUTY_KEY, MARKET),
    )
