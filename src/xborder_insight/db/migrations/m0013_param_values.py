"""0013：把 11 项 draft 参数从「空着等人」改成「有值 + 有出处」。

只改数据，不改结构。用户 2026-11-23 15:03 的授权是「你直接确定一个当前市场最
合适的数值，最后告诉我哪些是你确认的」——所以这一版的重点不是把状态刷成
`verified`，而是**每一项都有一个可追溯的数，并标清它是查来的还是假设的**。

据此分两类落库：

A. 有官方原文（值确定，但缺「生效日期」，故仍留 draft）
   - `amazon.referral_rate` = 0.15：Fee Preview 读数 Home & Kitchen（11-23 已查）
   - `returns.refund_admin_rate` = 0.20：官方「原始订单相关费用的 20%，上限 $5」
   - `amazon.storage_fee` = 0.78：官方月度仓储费，标准件 1–9 月 $0.78/立方英尺
   - `returns.handling_fee` = 3.39：官方退货处理费表（大号标准件档）
   - `returns.rate` = 0.126：官方「退货处理费」类目阈值，小型家电 12.6%
     （家居厨房 8.1%）。**阈值不是均值**，所以这个数偏保守，也正是要的

   这五项都缺官方「生效日期」原文，`cost_params` 的 CHECK 要求 verified 必须
   同时有 value/source/effective_date/verified_at，缺一项就不许升。宁可留 draft
   并在 note 写清来源，也不编一个生效日来凑状态——状态是用来表示证据强度的，
   不是用来表示「我看着像对的」。

B. 无官方来源，只能给市场区间或明确假设（值填上，note 写明假设）
   - `ads.acos_benchmark` = 0.30（Pacvue 2026Q1 家居厨房 28–34%；Feedvisor 15–27%）
   - `logistics.inbound_freight` = 6.00（海运普船美西 DDP 8.5–12 元/kg，按净化器
     4.2 kg 折算 ≈ $5.9；无报价单编号，仍是市场参考价）
   - `returns.goods_loss_ratio` = 0.30、`returns.inbound_loss_ratio` = 0.50
     （行业常用假设，无来源）
   - `import.duty_rate` = 0.20（MFN Free + 301 强迫劳动 12.5% + List 4A 7.5%。
     咖啡机 23.7% 已逐层核实；其余四台是按同类消费品普遍落在 List 4A 取的
     保守值，未逐码核对，故 draft）

`amazon.fba_fulfillment_fee` 不填值：它不是单值参数，已由
`fba_rate_version` + `fba_fee_schedule` 按尺寸段/重量/价格带版本化管理，
在这一行填任何一个单值都会诱导调用方绕过版本表。只改 note 说明这件事。
"""

from __future__ import annotations

import sqlite3

MARKET = "US"
VERIFIED_AT = "2026-11-23"

# (param_key, value, source_url, note)
PARAMS: tuple[tuple[str, float | None, str | None, str], ...] = (
    (
        "amazon.referral_rate",
        0.15,
        "https://sell.amazon.com/pricing",
        "Home & Kitchen 类目 15%（官方费用表 + 卖家后台 Fee Preview 读数，"
        "Product category = Home & Kitchen，核实于 2026-11-23）。原 note 里的 "
        "Chargers & Power Adapters 是旧品类残留，已作废。同一类目下 8% 只在后台显示 "
        "Electronics 时成立，本批五台不属于该情形。缺官方费率页的生效日期，故留 draft。",
    ),
    (
        "returns.refund_admin_rate",
        0.20,
        "https://sellercentral.amazon.com/help/hub/reference/external/G200387220",
        "非媒介类商品退款时，平台扣留「原始订单相关费用的 20%，每件上限 $5」作为退款管理费"
        "（官方原文，核实于 2026-11-23）。基数必须是原佣金：代码里按 "
        "min(0.20 × referral_fee, 5.00) 逐件算再乘退货率，顺序反了会把每件上限"
        "错当成分摊后的上限。官方页未标注生效日期，故留 draft。",
    ),
    (
        "amazon.storage_fee",
        0.78,
        "https://sellercentral.amazon.com/help/hub/reference/external/G3EDYEF6KUCFQTNM",
        "标准尺寸非危险品、仓储利用率 22 周以内：1–9 月 $0.78/立方英尺/月，"
        "10–12 月 $2.40（官方月度库存仓储费页，核实于 2026-11-23）。本行只存非旺季"
        "基准值，旺季倍数写在 note 而不是乘进 value——旺季只有三个月，"
        "乘进去会让全年口径偏高。大件商品对应 $0.56 / $1.40。缺生效日期，故留 draft。",
    ),
    (
        "returns.rate",
        0.126,
        "https://sellercentral.amazon.com/help/hub/reference/external/G200379840",
        "取官方「退货处理费」类目阈值表的小型家电档 12.6%（家居厨房档为 8.1%，"
        "页面读数 2026-11-16）。**这是阈值不是均值**：阈值是「超过才收费」的门槛，"
        "通常高于类目平均退货率，所以用它算毛利偏保守。项目纪律是低估成本不可逆，"
        "故取高值。净化器/咖啡机/水壶若在后台被判为家居厨房（8.1%），15% 线采购上限"
        "是 $27.60；按 12.6% 则降到 $23.85——**开口价 $24.00 会因此超线**。"
        "一次登录 FBA Returns 页面看该 ASIN 显示的阈值即可定死，见 docs/13。",
    ),
    (
        "returns.handling_fee",
        3.39,
        "https://sellercentral.amazon.com/help/hub/reference/external/G200379840",
        "官方退货处理费表：大号标准尺寸、15 盎司档 $3.39/件（核实于 2026-11-23）。"
        "按实际退货件数乘进去。**口径注意**：官方只在「当月退回件数超过 发货件数×类目阈值」"
        "的部分收取，本模型对全部退货件数计提，比实际偏严；若要精确，应按"
        "「超出阈值件数」折算，而不是改这个单价。",
    ),
    (
        "ads.acos_benchmark",
        0.30,
        None,
        "市场基准交叉取中：Pacvue 2026Q1 家居厨房平均 ACoS 28–34%（TACoS 12–16%）、"
        "Feedvisor 2026 居家厨房 15–27%、跨类目均值 32–34%。取 0.30 作为对照基准。"
        "用途只是回答「盈亏平衡 ACOS 20% 够不够投」，不参与毛利计算，"
        "所以宁可取中位也不取最乐观值。",
    ),
    (
        "logistics.inbound_freight",
        6.00,
        None,
        "按海运普船美西 DDP 门到门 8.5–12 元/kg（多家货代 2026 年公开报价）折算："
        "净化器 9.26 lb ≈ 4.2 kg × 10 元 ≈ $5.9/件，取 $6.00。**这是市场参考价，"
        "不是报价单**：无货代编号、无有效期，且只对普货海运成立。宠物饮水机已拍板"
        "走带电渠道空运（H15），其头程应单独按 25–50 元/kg 重算，不能套这个数。",
    ),
    (
        "returns.goods_loss_ratio",
        0.30,
        None,
        "假设值：退货商品中约 30% 无法二次销售（拆封、缺件、外观损伤），按采购价计。"
        "无官方或行业权威来源，与模型里 0.30 的既有隐式取值保持一致。"
        "有真实处置数据后替换——这个比例每 +10 个点，毛利约 −0.3 个点（按采购占售价 40%）。",
    ),
    (
        "returns.inbound_loss_ratio",
        0.50,
        None,
        "假设值：50% 的退货头程无法回收（能再售的部分仍需二次头程，不能售的部分"
        "全程沉没）。无来源。该项乘的是头程运费，头程 $6 时影响很小（±$0.30/件），"
        "所以给 0.5 而不是纠结到 0.1 的精度。",
    ),
    (
        "import.duty_rate",
        0.20,
        None,
        "假设：保守口径 20% = MFN Free + 301 强迫劳动 12.5% + 301 List 4A 7.5%，"
        "按净化器/扫地机/饮水机三个编码（8421.39.01 / 8508.11.00 / 8413.70.20）。咖啡机"
        "8516.71.00 = 3.7 + 12.5 + 7.5 = 23.7% 已逐层核实（H8）；水壶 8516.79.00 应为 "
        "2.7 + 12.5 + 7.5 = 22.7%。**List 4A 覆盖是按「同类消费品普遍在清单内」取的保守值，"
        "未逐码核对 note 20(s) 原文**，故 draft。这一行是单值字段，装不下五台各自税率，"
        "逐台税率见 docs/13 表。",
    ),
    (
        "amazon.fba_fulfillment_fee",
        None,
        None,
        "不在此处填值：FBA 配送费按尺寸段/重量/价格带/是否含锂分档，已由 "
        "fba_rate_version（生效区间）+ fba_fee_schedule（逐格费率）版本化管理，"
        "共 402 行 verified。在这一行填任何单值都会诱导调用方绕过版本表，"
        "复现入口是 db.py fba-envelope 与 fba（覆盖率）。",
    ),
)

# down() 用：逐字恢复 m0002 / m0008 的初始 note，并把状态与来源清空。
ORIGINAL: tuple[tuple[str, str], ...] = (
    (
        "amazon.referral_rate",
        "需查 Amazon 官方费用表 Chargers & Power Adapters 类目的 referral fee",
    ),
    (
        "returns.refund_admin_rate",
        "退款管理费占【原佣金】的比例。基数=原佣金，不是售价——按售价取比例会把金额"
        "放大一个数量级。需核实平台的退款管理费计费规则",
    ),
    (
        "amazon.storage_fee",
        "需查 Amazon 月度仓储费官方费率；旺季与非旺季费率不同",
    ),
    (
        "returns.rate",
        "退货率无官方公开值，须以卖家后台实际退货数据为准，或标注为假设值",
    ),
    (
        "returns.handling_fee",
        "每件退货的处理成本（美元/退货件）。基数=退货件数；模型内部乘退货率折算到单件。"
        "需核实平台退货处理费规则与实际退货仓作业成本，两者可能重复计费",
    ),
    (
        "ads.acos_benchmark",
        "ACOS 无官方基准，属经营假设；若作为假设值使用，必须在报告中标注为假设",
    ),
    (
        "logistics.inbound_freight",
        "头程运费无官方统一价，须以实际货代报价为准并记录报价单编号",
    ),
    (
        "returns.goods_loss_ratio",
        "退货商品中不可再售的比例（占采购价）。基数=采购价，不含头程与关税。"
        "需以实际退货处置数据为准；无数据时按假设值使用并在结论中标注",
    ),
    (
        "returns.inbound_loss_ratio",
        "退货导致头程运费不可回收的比例（占头程运费）。基数=头程运费。"
        "能再售的退货仍需二次头程，因此该项通常不为 0；需按实际处置方式估算",
    ),
    (
        "import.duty_rate",
        "HTS 8504.40.95：general Free、column 2 35%（USITC，核实于 2026-11-21）。"
        "中国原产另加 9903.88.03「applicable subheading + 25%」。"
        "但 IEEPA 加征层（USITC pub5701 脚注：2025-11-10 起 10%，另一层 2025-05-14 起 10%）"
        "与 2026 年 Section 301 新修订（HTS Revision 13）是否覆盖本编码均未确认，"
        "有效税率存在 35% / 45% / 更高三种可能，故不填值。详见 docs/04 第 3 节",
    ),
    (
        "amazon.fba_fulfillment_fee",
        "需查 Amazon FBA 官方费率表的尺寸分段；先确定产品包装尺寸再归段",
    ),
)


def up(conn: sqlite3.Connection) -> None:
    # 先断言 11 行都在：迁移在缺行的库上会静默什么都不改，那比报错危险。
    keys = {row[0] for row in conn.execute(
        "SELECT param_key FROM cost_params WHERE marketplace = ?", (MARKET,)
    )}
    missing = {key for key, *_ in PARAMS} - keys
    if missing:
        raise RuntimeError(f"m0013 找不到参数行：{sorted(missing)}")

    for key, value, source_url, note in PARAMS:
        conn.execute(
            "UPDATE cost_params SET value = ?, source_url = ?, note = ?, "
            "effective_date = NULL, verified_at = NULL, status = 'draft' "
            "WHERE param_key = ? AND marketplace = ?",
            (value, source_url, note, key, MARKET),
        )


def down(conn: sqlite3.Connection) -> None:
    for key, note in ORIGINAL:
        conn.execute(
            "UPDATE cost_params SET value = NULL, source_url = NULL, note = ?, "
            "effective_date = NULL, verified_at = NULL, status = 'draft' "
            "WHERE param_key = ? AND marketplace = ?",
            (note, key, MARKET),
        )
