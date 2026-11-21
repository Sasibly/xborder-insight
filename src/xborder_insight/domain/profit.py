"""利润模型：从报价算到盈亏平衡 ACOS。

口径（2026-11-17 与业务方确认，改动需同步 docs/02）：

    落地成本 = 报价 + 头程 + 关税
    退货影响 = 退款冲减 + 处理费 + 退款管理费 + 商品损失 + 头程损失
    毛利     = 售价 − 落地成本 − 佣金 − FBA − 退货影响
    盈亏平衡 ACOS = 毛利 ÷ 售价

三个与直觉不同、但必须这样写的地方：

1. 【关税与头程是条件生效的】
   DDP 报价已含关税，CFR/CIF/DAP 报价已含头程。若此时仍按传入值叠加，
   就是重复计费。模型的处理是「置零 + 记录口径声明」，而不是「忽略」——
   两者在审计上完全不同：前者留下了「我为什么没算」的记录，后者只留下一个 0。
   同时若调用方在含税术语下仍传了非零税率，模型发出 warning 而不是静默吞掉，
   因为那通常意味着调用方对报价方式的理解与术语不一致。

2. 【退货是「一段收入冲减 + 四段费用」，不是五段费用】
   退款冲减    = 退货率 × 售价                性质：交易被撤销
   处理费      = 退货率 × 每件处理费          基数：退货件
   退款管理费  = 退货率 × 费率 × 原佣金       基数：原佣金（不是售价）
   商品损失    = 退货率 × 不可再售比例 × 报价  基数：报价
   头程损失    = 退货率 × 沉没比例 × 实际头程  基数：实际计入的头程
   只扣四项费用时，退货率 50% 仍会算出正毛利（实测 +9.54），
   因为收入还按 100% 计而货退了一半；补上退款冲减后同一情景为 −5.45。
   合成单一比率会让「退货率高」与「退货处理贵」这两个不同杠杆变成一个数，
   而它们的改进手段完全不同。

3. 【负毛利时 ACOS 不是负数，是不存在】
   亏损状态下任何广告投入都只是放大亏损，「盈亏平衡 ACOS = -0.2」会被读成
   「还有 20% 空间」。此时返回 None 并给出理由，而不是交出一个会误导的负数。

assumptions 与 warnings 的区别（这个区分贯穿全项目）：
    assumptions —— 我按什么口径算的，供事后复核；
    warnings    —— 你的输入可能有问题，供当场修正。
    混在一起会让「用了默认术语」和「头程漏填」看起来一样严重，实际上不是。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import InputInvalid, ParamUnverified
from . import incoterm as incoterm_mod

# 允许的浮点误差，避免 1.0000000002 这类输入被判非法
_EPS = 1e-9

# 退款管理费上限（每笔退款）。
# 官方原文（Seller Central · Selling on Amazon fee schedule）：
#   "the applicable refund administration fee, which is the lesser of $5.00
#    or 20% of the applicable referral fee"
# 即 min($5.00, 20% × 原佣金)。低价商品永远碰不到上限，
# 但佣金超过 $25（≈ 售价 $167 @15%）后上限生效，若不封顶会高估退货成本。
REFUND_ADMIN_CAP_USD = 5.00


@dataclass(frozen=True)
class ReturnsLoss:
    """退货的全部财务影响：一段收入冲减 + 四段费用。

    前四项是「退货要花多少钱」，第一项是「退货要退多少钱」。
    两者必须分开：费用可以优化（换退货仓、改包装），退款不能——
    退款是交易的撤销，不是成本。

    为什么必须有 revenue_reversal：
        只扣四项费用时，退货率 50% 仍能算出正毛利（实测 +9.54），
        因为收入仍按 100% 计，而货其实退了一半。这个偏差正好等于
        `退货率 × 售价`，随退货率线性放大。不单列这一项，
        高退货率品类会被系统判为「可做」，而这是最贵的一类错误。
    """

    revenue_reversal: float
    handling: float
    refund_admin: float
    goods: float
    inbound: float

    @property
    def total(self) -> float:
        return self.revenue_reversal + self.handling + self.refund_admin + self.goods + self.inbound

    def as_dict(self) -> dict[str, float]:
        return {
            "revenue_reversal": round(self.revenue_reversal, 4),
            "handling_fee": round(self.handling, 4),
            "refund_admin_fee": round(self.refund_admin, 4),
            "goods_loss": round(self.goods, 4),
            "inbound_loss": round(self.inbound, 4),
            "total": round(self.total, 4),
        }


@dataclass(frozen=True)
class CostInputs:
    """利润模型的全部输入。口径由 incoterm 决定，不由字段名决定。

    quoted_price 是「按术语口径的报价」——
        FOB 下是离岸价，DDP 下是完税到门价。
    同一个数字在两种术语下代表的成本范围不同，所以术语必须与价格一起传。

    `fba_fee=None` 表示「不知道这一单的 FBA 费」，此时必须给 `fba_bounds`
    （来自 `fba.fee_envelope()` 的可证明区间），模型会把毛利率算成区间。
    刻意**没有**默认值 0：把「不知道」写成 0，等于每件静默多赚 $2.4–8.6 的毛利，
    而这种错误在报表上没有任何痕迹——毛利率精确到小数点后两位，只是偏高。
    """

    quoted_price: float
    selling_price: float
    inbound_freight: float
    duty_rate: float
    referral_rate: float
    fba_fee: float | None
    return_rate: float
    returns_handling_fee: float
    returns_refund_admin_rate: float
    returns_goods_loss_ratio: float
    returns_inbound_loss_ratio: float
    incoterm: str | None = None
    fba_bounds: tuple[float, float] | None = None


@dataclass(frozen=True)
class ProfitResult:
    incoterm: str
    incoterm_assumed: bool
    selling_price: float
    quoted_price: float
    freight_included_in_quote: bool
    duty_included_in_quote: bool
    freight: float
    duty: float
    landed_cost: float
    referral_fee: float
    fba_fee: float
    returns: ReturnsLoss
    gross_profit: float
    gross_margin: float
    breakeven_acos: float | None
    breakeven_reason: str | None
    fba_known: bool = True
    fba_low: float = 0.0
    fba_high: float = 0.0
    gross_profit_low: float = 0.0
    gross_profit_high: float = 0.0
    gross_margin_low: float = 0.0
    gross_margin_high: float = 0.0
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def fba_range_usd(self) -> tuple[float, float]:
        return (self.fba_low, self.fba_high)

    @property
    def gross_margin_range(self) -> tuple[float, float]:
        return (self.gross_margin_low, self.gross_margin_high)

    def as_dict(self) -> dict[str, object]:
        """给 API 层用的序列化形式。金额保留 4 位，比率保留 6 位。"""
        return {
            "incoterm": self.incoterm,
            "incoterm_assumed": self.incoterm_assumed,
            "selling_price": round(self.selling_price, 4),
            "quoted_price": round(self.quoted_price, 4),
            "freight": round(self.freight, 4),
            "duty": round(self.duty, 4),
            "landed_cost": round(self.landed_cost, 4),
            "referral_fee": round(self.referral_fee, 4),
            "fba_fee": round(self.fba_fee, 4),
            "fba_known": self.fba_known,
            "fba_range_usd": [round(self.fba_low, 4), round(self.fba_high, 4)],
            "returns": self.returns.as_dict(),
            "gross_profit": round(self.gross_profit, 4),
            "gross_margin": round(self.gross_margin, 6),
            "gross_profit_range_usd": [
                round(self.gross_profit_low, 4),
                round(self.gross_profit_high, 4),
            ],
            "gross_margin_range": [
                round(self.gross_margin_low, 6),
                round(self.gross_margin_high, 6),
            ],
            "breakeven_acos": None if self.breakeven_acos is None else round(self.breakeven_acos, 6),
            "breakeven_reason": self.breakeven_reason,
            "freight_included_in_quote": self.freight_included_in_quote,
            "duty_included_in_quote": self.duty_included_in_quote,
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
        }


def _check_finite(name: str, value: float) -> float:
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise InputInvalid(f"{name} 必须是有限数值，当前值: {value!r}")
    return number


def compute(inputs: CostInputs) -> ProfitResult:
    """算毛利。纯函数，不读库、不读时钟、不读环境变量。"""
    assumptions: list[str] = []
    warnings: list[str] = []

    # ---------- 0. 输入合法性 ----------
    selling_price = _check_finite("售价", inputs.selling_price)
    if selling_price <= 0:
        raise InputInvalid("售价必须大于 0：售价为 0 时毛利率与 ACOS 均无定义")

    quoted_price = _check_finite("报价", inputs.quoted_price)
    if quoted_price < 0:
        raise InputInvalid(f"报价不能为负，当前值: {quoted_price}")

    return_rate = _check_finite("退货率", inputs.return_rate)
    if not (-_EPS <= return_rate <= 1 + _EPS):
        raise InputInvalid(
            f"退货率必须在 [0, 1] 区间，当前值: {return_rate}"
            "（若原始数据是百分比，请先除以 100）"
        )
    return_rate = min(max(return_rate, 0.0), 1.0)

    for name, value in (
        ("头程运费", inputs.inbound_freight),
        ("关税税率", inputs.duty_rate),
        ("佣金率", inputs.referral_rate),
        ("退货处理费", inputs.returns_handling_fee),
        ("退款管理费率", inputs.returns_refund_admin_rate),
        ("商品损失比例", inputs.returns_goods_loss_ratio),
        ("头程沉没比例", inputs.returns_inbound_loss_ratio),
    ):
        if _check_finite(name, value) < 0:
            raise InputInvalid(f"{name} 不能为负，当前值: {value}")

    # ---------- 1. 术语分支：决定哪些成本已在报价里 ----------
    try:
        code, assumed = incoterm_mod.normalize(inputs.incoterm)
    except incoterm_mod.UnknownIncoterm as exc:
        # 术语拼错是调用方的输入问题（422），不是服务端故障（500）。
        # 若让它以 ValueError 逃出去，会被兜底成 internal_error，
        # 排查方向会被彻底带偏。
        raise InputInvalid(str(exc)) from exc
    t = incoterm_mod.terms(code)

    if assumed:
        assumptions.append(
            f"未指定贸易术语，按默认口径 {code}（{t.label}）计算："
            "报价视为不含头程与关税"
        )

    if t.freight_included:
        freight = 0.0
        assumptions.append(
            f"{code} 报价已含头程运费，本次不再单独计入头程"
            "（若重复计入会低估毛利）"
        )
        if inputs.inbound_freight > 0:
            warnings.append(
                f"{code} 下收到非零头程 {inputs.inbound_freight:.2f}，已置零以避免重复计费；"
                "若报价确未含运费，请改用 FOB/EXW 口径"
            )
    else:
        freight = _check_finite("头程运费", inputs.inbound_freight)
        if freight == 0:
            warnings.append(
                f"{code} 报价通常不含运费，但头程为 0——请确认是卖家包邮还是漏填"
            )

    if t.duty_included:
        duty = 0.0
        assumptions.append(
            f"{code} 报价已含进口关税，本次不再叠加关税项；"
            "该口径成立的前提是报价确为完税价"
        )
        if inputs.duty_rate > 0:
            warnings.append(
                f"{code} 下收到非零关税税率 {inputs.duty_rate:.4f}，已置零以避免重复计税；"
                "若报价实际未含税，请改用 FOB 口径"
            )
    else:
        duty = quoted_price * _check_finite("关税税率", inputs.duty_rate)
        assumptions.append(
            f"关税按 {code} 口径计征，计税基础取申报价值（即报价），"
            "未将运费与保险并入完税价格"
        )

    landed_cost = quoted_price + freight + duty

    # ---------- 2. 平台费用 ----------
    referral_fee = selling_price * _check_finite("佣金率", inputs.referral_rate)

    # FBA：要么知道这一单的费（点值），要么知道它的可证明区间（包络）。
    # 两条路之外没有第三条——尤其是「默认 0」，那不是第三态，是错误。
    fba_known = inputs.fba_fee is not None
    if fba_known:
        fba_low = fba_high = _check_finite("FBA 费", inputs.fba_fee)
        if fba_low < 0:
            raise InputInvalid(f"FBA 费不能为负，当前值: {fba_low}")
    else:
        if inputs.fba_bounds is None:
            raise ParamUnverified(
                "未提供 FBA 费，也没有可用的费率包络",
                detail=(
                    "FBA 费不能默认为 0：一件标准尺寸商品的配送费在 $2.4–13.4 之间，"
                    "按 0 计会让毛利率静默偏高同样金额，而报表上看不出任何异常。"
                    "请传 fba_fee，或传 product_class + ship_date 由费率表算包络"
                ),
            )
        raw_low, raw_high = inputs.fba_bounds
        fba_low = _check_finite("FBA 包络下界", raw_low)
        fba_high = _check_finite("FBA 包络上界", raw_high)
        if fba_low < 0 or fba_high < 0:
            raise InputInvalid(
                f"FBA 包络不能为负，当前值: [{fba_low}, {fba_high}]"
            )
        if fba_high < fba_low:
            raise InputInvalid(
                f"FBA 包络上界小于下界：[{fba_low}, {fba_high}]",
                detail="包络必须满足 low ≤ high，否则区间无意义",
            )
        assumptions.append(
            f"FBA 费未提供，按官方费率表的包络 ${fba_low:.2f}–${fba_high:.2f} 计算："
            "毛利率因此是一个区间，[下界] 对应最贵档、[上界] 对应最便宜档"
        )
        if fba_high - fba_low > 1.0:
            warnings.append(
                f"FBA 包络跨度 ${fba_high - fba_low:.2f}（${fba_low:.2f}–${fba_high:.2f}）："
                "跨度越大，毛利率区间越宽；若知道商品的尺寸段或重量上界，"
                "可用 size_tiers / weight_range_oz 收窄后再算"
            )

    # ---------- 3. 退货影响：一段收入冲减 + 四段费用 ----------
    # 退款管理费按「每笔退款」封顶：min($5.00, 费率 × 单件佣金)。
    # 先算单笔再乘退货率，顺序反了会让上限变成「每件分摊后仍封顶」的错误语义。
    refund_admin_per_return = min(
        inputs.returns_refund_admin_rate * referral_fee, REFUND_ADMIN_CAP_USD
    )

    returns = ReturnsLoss(
        # 退款冲减收入：退货率 r 意味着 r 比例的交易被撤销，收入本身要退回去。
        # 这一项不是「费用」，是「这笔钱没收到」，所以必须与下面四项分列。
        revenue_reversal=return_rate * selling_price,
        handling=return_rate * inputs.returns_handling_fee,
        # 基数是「原佣金」：平台退佣金时扣留的那部分，按售价取比例会放大一个数量级
        refund_admin=return_rate * refund_admin_per_return,
        goods=return_rate * inputs.returns_goods_loss_ratio * quoted_price,
        # 基数是「实际计入的头程」：术语已含运费时为 0，
        # 因为那部分沉没成本已包含在报价里，单独再加就是重复计
        inbound=return_rate * inputs.returns_inbound_loss_ratio * freight,
    )

    if refund_admin_per_return >= REFUND_ADMIN_CAP_USD - _EPS:
        assumptions.append(
            f"退款管理费按官方规则取 min(${REFUND_ADMIN_CAP_USD:.2f}, 费率×原佣金)=$"
            f"{refund_admin_per_return:.2f}，本单已触发上限"
        )

    assumptions.append(
        "佣金按全额计、未按退货比例退回，因此本模型对退货的估计偏保守："
        "退货率越高，真实毛利略高于此处数值"
    )

    # ---------- 4. 汇总 ----------
    # 两个端点各算一次：最便宜 FBA → 最高毛利；最贵 FBA → 最低毛利。
    # 对外给的点值取「最低毛利」（即最贵 FBA），因为两种错误的代价不对称：
    # 高估成本会让人错过一个好品（可复核），低估成本会让人投一个亏钱的品（不可逆）。
    def _gross_at(fba: float) -> tuple[float, float]:
        profit_at = selling_price - landed_cost - referral_fee - fba - returns.total
        return profit_at, profit_at / selling_price

    gross_profit_high, gross_margin_high = _gross_at(fba_low)
    gross_profit_low, gross_margin_low = _gross_at(fba_high)
    fba_fee = fba_high
    gross_profit = gross_profit_low
    gross_margin = gross_margin_low

    if not fba_known and gross_profit_low <= 0 < gross_profit_high:
        warnings.append(
            "毛利区间跨过 0：这件商品能不能做，完全取决于包装后的尺寸与实重——"
            "按最便宜档能赚，按最贵档会亏。此时必须先量包装再定价，不能凭感觉"
        )

    if gross_profit > 0:
        breakeven_acos: float | None = gross_profit / selling_price
        breakeven_reason: str | None = None
    else:
        breakeven_acos = None
        breakeven_reason = (
            "毛利非正：广告投入只会放大亏损，不存在盈亏平衡点。"
            "此时任何 ACOS 都不成立，因此不返回数值"
        )

    if return_rate >= 0.5:
        warnings.append(
            f"退货率 {return_rate:.1%} 已超过半数，单件利润对退货假设高度敏感，"
            "结论应视为情景推演而非预期值"
        )

    return ProfitResult(
        incoterm=code,
        incoterm_assumed=assumed,
        selling_price=selling_price,
        quoted_price=quoted_price,
        freight_included_in_quote=t.freight_included,
        duty_included_in_quote=t.duty_included,
        freight=freight,
        duty=duty,
        landed_cost=landed_cost,
        referral_fee=referral_fee,
        fba_fee=fba_fee,
        returns=returns,
        gross_profit=gross_profit,
        gross_margin=gross_margin,
        breakeven_acos=breakeven_acos,
        breakeven_reason=breakeven_reason,
        fba_known=fba_known,
        fba_low=fba_low,
        fba_high=fba_high,
        gross_profit_low=gross_profit_low,
        gross_profit_high=gross_profit_high,
        gross_margin_low=gross_margin_low,
        gross_margin_high=gross_margin_high,
        assumptions=tuple(assumptions),
        warnings=tuple(warnings),
    )
