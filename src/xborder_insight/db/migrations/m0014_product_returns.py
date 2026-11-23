"""0014：逐 SKU 退货率 `product_returns`。

为什么必须单独一张表：
    `cost_params.returns.rate` 是**一个**数，语义是「整个市场一个口径」。
    但退货率的真实形态是**逐 SKU**：净化器 12.6%、咖啡机 8%（假设），
    差 4.6 个点直接决定 15% 线上采购上限差 $3 以上（`docs/04` §24 / §25.9）。
    在单值参数表里装不下第二个 SKU，所以 11-23 之前压测只能靠命令行
    `--stress-returns` 传数——**API 与 `make check` 都取不到**。
    一条只存在于某次终端输入里的口径，等于没有进版本库：
    换台机器重跑，同一句 `screen` 会给出不同结论，而没人知道差在哪。

    `product_returns` 表把这件事变成可查询的事实。

四个设计决定，每个都有代价：

1. **唯一键用内部 `sku_key`，不用 ASIN。**
   五台里目前只有咖啡机有 ASIN（B0DCZHWNDJ），净化器的 ASIN 没记录。
   若以 ASIN 作唯一键，净化器这条就落不了库——表建了等于没建。
   所以内部键做唯一约束，ASIN 做**可空的外部标识**：
   有就填（能跨系统关联），没有也能登记。宁可缺关联，不要缺记录。
   ASIN 填了就校验 10 位，避免把 SKU 号或 URL 误填进来。

2. **两档同表、同一行，且 `stress_rate > baseline_rate` 写进 CHECK。**
   压力档是情景参数，不是事实，但它必须跟着基线走：
   分两行存会让「这一台的压力档是多少」变成一次 join 才能回答的问题，
   而漏掉 join 的后果是压力档静默取默认值——正是要防的 ②类错误。
   把 `>` 放进 CHECK，与 `admission.stress_test()` 里的入参校验重复了一次：
   重复是故意的。Python 侧挡的是本次调用，CHECK 挡的是任何写入路径
   （包括以后有人直接用 sqlite3 命令行改库）。两个参数写倒时，
   静默按「压力档 = 基线」算会得到一条永远为正的结论。

3. **`status` 的证据闸与 `cost_params` 同构，但「来源」不是 URL。**
   退货率的来源天然不可链接：它是卖家后台里的一个读数，没有公开页面。
   所以这里要求的是 `source_note`（后台路径 + 读到的是什么）+ `effective_date`
   （读数日期）+ `verified_at`（核实日期），三项齐全才许 `verified`。
   这不比 `cost_params` 松：`source_note` 必须写到「照着这句话能复现出同一个数」，
   写得含糊就复现不出来，等于没有来源。

4. **`estimate` 与 `category_threshold` 永远不许 `verified`，写在 CHECK 里。**
   这是对「假设不许冒充核实」的结构化落地，但它分了两档，因为第一版把两件事
   混成了一个 `backend_read`，一跑就露馅：净化器的 12.6% 确实是从后台读来的，
   但读到的是**类目阈值**，不是这一台的实际退货率。若只标 `backend_read`，
   它在台账上会显示「证据齐全、什么都不差」——一个 draft 行却列不出待办，
   这正是本项目最怕的那种「看起来已完成」。
   所以证据分四档，只有 `backend_read`（该 SKU 的读数）与 `official_page`
   能升 verified；`category_threshold` 与 `estimate` 无论多合理都只能是 draft。

   咖啡机 8% 是假设（用户 2026-11-23 16:37 给的基线档），它必须能被算，
   但永远不能被叫做「核实过」。CHECK 让它即使有人手工 UPDATE 也升不上去——
   除非先改 evidence_kind，而改 evidence_kind 是一个有意识的动作，
   不是顺手把状态刷绿。

种子只放两行，都是 draft，理由不同：
    - 净化器 12.6%：`category_threshold`——读到的是**类目阈值**
      （Small Appliances），不是该 ASIN 的实际退货率。按保守口径当基线用
      （`docs/04` §24），所以 source_note 里必须写明这一点，且不升 verified：
      真正的 ASIN 均值要 Reports → FBA Returns 的 return rate 报表，
      那份报表还没导出（H3）。
    - 咖啡机 8%：`estimate`，用户给的基线假设，连读数都没有，永远 draft。

压力档 17% / 15% 来自用户 2026-11-23 16:37 的规则（`docs/04` §25.1），
是情景设定而非读数，所以不进证据字段——这一点写在 note 里，
免得后来人以为「压力档也该有个来源」。
"""

from __future__ import annotations

import sqlite3

CREATED_AT = "2026-11-23T18:20:00+08:00"  # 迁移内不读时钟，保证可重复执行
MARKET = "US"

PRODUCT_RETURNS = """
CREATE TABLE product_returns (
    id              INTEGER PRIMARY KEY,
    marketplace     TEXT NOT NULL,
    -- 内部稳定键：不随外部标识（ASIN/链接/改名）变动，是这张表真正的主语。
    sku_key         TEXT NOT NULL,
    product_name    TEXT NOT NULL,
    -- 关联 taxonomy 的 category key；还没归类时留空，不硬塞一个猜的值。
    category        TEXT,
    -- 外部标识，可空。填了就必须是 10 位 ASIN，挡掉 SKU 号/URL 误填。
    asin            TEXT,
    baseline_rate   REAL NOT NULL
                    CHECK (baseline_rate >= 0 AND baseline_rate < 1),
    -- 压力档必须严格高于基线：写倒了的后果是「永远为正」的结论。
    stress_rate     REAL NOT NULL
                    CHECK (stress_rate > baseline_rate AND stress_rate < 1),
    evidence_kind   TEXT NOT NULL
                    CHECK (evidence_kind IN (
                        'backend_read',       -- 该 SKU 的后台读数
                        'category_threshold', -- 只读到类目阈值，按保守口径借用
                        'official_page',      -- 官方公开页面
                        'estimate'            -- 假设
                    )),
    source_note     TEXT,
    effective_date  TEXT,
    verified_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'verified')),
    note            TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (marketplace, sku_key),
    CHECK (asin IS NULL OR length(asin) = 10),
    -- 与 cost_params 同构的三闸：来源 + 读数日期 + 核实日期缺一不可。
    CHECK (
        status <> 'verified' OR (
            source_note IS NOT NULL
            AND effective_date IS NOT NULL
            AND verified_at IS NOT NULL
        )
    ),
    -- 假设与「借来的阈值」都不可能是核实过的：两者描述的都**不是这一台的实际退货率**。
    -- 要升 verified 必须先改 evidence_kind，而那一步要求真的去拿了读数。
    CHECK (status <> 'verified' OR evidence_kind IN ('backend_read', 'official_page'))
)
"""

# 不加额外索引：这张表是十级规模，UNIQUE (marketplace, sku_key) 已带索引，
# 按 category 汇总时全表扫描的成本远小于一个索引的维护成本。

PURIFIER_NOTE = (
    "基线 12.6% 读到的是 Seller Central → FBA Returns 页面上该 ASIN 所属 "
    "Small Appliances 类目的**退货处理费阈值**，不是这一台的实际退货率——"
    "后台默认只显示类目阈值（11-16 首次读数，11-23 回读确认）。"
    "按「低估成本不可逆」的口径拿阈值当基线用，因此这个数偏保守，"
    "也正是它没升 verified 的原因：真正的 ASIN 均值要从 "
    "Reports → FBA Returns 的 return rate 报表导（H3）。"
    "压力档 17% 是情景设定（+4.4 个点），不是读数。"
    "成交价 $24.30 在 12.6% 下毛利 14.35%，在 17% 下 9.18%（docs/04 §28.4）。"
)

COFFEE_NOTE = (
    "基线 8% 与压力档 15% 都是**假设**：8% 是用户 2026-11-23 16:37 给的基线档，"
    "15% 是同一条规则给的压力档，两者都不是后台读数，所以 evidence_kind = estimate"
    "且 status 永久 draft（CHECK 挡着）。"
    "咖啡机的两档差 7 个点（净化器是 5），所以压测对它更关键："
    "基线达标而压力档可能转负，那种情况该换杠杆（箱规/售价），不在价格上花时间。"
)

# (sku_key, product_name, category, asin, baseline, stress, evidence, source_note,
#  effective_date, verified_at, status, note)
SEED: tuple[tuple[object, ...], ...] = (
    (
        "purifier_desk_1752",
        "空气净化器（桌面 17.52×13.78×7.48）",
        "air_purifier",
        None,
        0.126,
        0.17,
        "category_threshold",
        "Seller Central → FBA Returns：该 ASIN 所属 Small Appliances 类目"
        "退货处理费阈值 12.6%（11-16 首次读数，11-23 回读）",
        "2026-11-16",
        "2026-11-23",
        "draft",
        PURIFIER_NOTE,
    ),
    (
        "coffee_semi_1613",
        "咖啡机（半自动意式 16×13×9.5）",
        "coffee_machine",
        "B0DCZHWNDJ",
        0.08,
        0.15,
        "estimate",
        None,
        None,
        None,
        "draft",
        COFFEE_NOTE,
    ),
)


def up(conn: sqlite3.Connection) -> None:
    conn.execute(PRODUCT_RETURNS)
    conn.executemany(
        "INSERT INTO product_returns "
        "(marketplace, sku_key, product_name, category, asin, baseline_rate, "
        " stress_rate, evidence_kind, source_note, effective_date, verified_at, "
        " status, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (MARKET, *row, CREATED_AT)
            for row in SEED
        ],
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS product_returns")
