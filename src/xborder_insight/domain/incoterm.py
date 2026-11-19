"""贸易术语（Incoterms）：决定「哪些成本已在报价里」。

为什么这不是一个 if-else 的小事：
    同一句「采购价 8 美元」，FOB 和 DDP 下的真实落地成本可以差 30% 以上。
    如果系统只认「采购价」一个数，它会在两种情况下都算出一个看起来很正常的毛利：
        FOB 报价当成含税价 → 漏算关税与头程 → 毛利虚高
        CIF 报价又加一遍头程 → 运费重复计 → 毛利虚低
    这两个方向相反的错误，在输出上都是「一个两位小数、带百分号的毛利率」，
    从页面完全看不出问题。这是 ②类错误（结论错但系统正常），必须靠结构消灭。

做法：把每个术语拆成两个布尔事实，而不是散落在业务代码里的判断。

    freight_included  报价是否已含头程运费
    duty_included     报价是否已含进口关税

只有 DDP 两个都为真。其余术语运费与关税的归属各不相同，
但共同点是「关税都没含」。

口径声明（对应用户 2026-11-17 的决定）：
    1. 不预设供应商报价方式，模型按术语分支；
    2. 建站初期以 FOB 为默认口径，默认值必须回显给调用方，不做静默填充；
    3. 关税项条件生效——DDP 下不再叠加，且传入 duty_rate 要告警而非默默忽略。
"""

from __future__ import annotations

from dataclasses import dataclass


class UnknownIncoterm(ValueError):
    """传入了不认识的贸易术语。"""


class Incoterm(str):
    """贸易术语。用 str 子类而非 Enum，便于直接出现在 JSON 契约里。"""

    __slots__ = ()

    EXW = "EXW"
    FOB = "FOB"
    CFR = "CFR"
    CIF = "CIF"
    DAP = "DAP"
    DDP = "DDP"

    ALL = ("EXW", "FOB", "CFR", "CIF", "DAP", "DDP")


@dataclass(frozen=True)
class IncotermTerms:
    code: str
    label: str
    freight_included: bool
    duty_included: bool
    note: str


# 这张表就是「商务知识」的可执行版本。改动这里等于改动利润模型的口径，
# 所以每一条都写清依据，不接受「大概是这样」。
TERMS: dict[str, IncotermTerms] = {
    "EXW": IncotermTerms(
        code="EXW",
        label="工厂交货",
        freight_included=False,
        duty_included=False,
        note="卖方仅在工厂交货，头程与关税全部由买方承担",
    ),
    "FOB": IncotermTerms(
        code="FOB",
        label="装运港船上交货",
        freight_included=False,
        duty_included=False,
        note="卖方承担到装运港的费用，海运与进口关税由买方承担；建站初期的默认口径",
    ),
    "CFR": IncotermTerms(
        code="CFR",
        label="成本加运费",
        freight_included=True,
        duty_included=False,
        note="卖方已承担到目的港的运费，因此头程不可重复计入；进口关税仍由买方承担",
    ),
    "CIF": IncotermTerms(
        code="CIF",
        label="成本加保险费加运费",
        freight_included=True,
        duty_included=False,
        note="同 CFR，另含保险；头程与保险均不可重复计入，进口关税仍由买方承担",
    ),
    "DAP": IncotermTerms(
        code="DAP",
        label="目的地交货",
        freight_included=True,
        duty_included=False,
        note="卖方运到目的地但不负责进口清关，关税仍由买方承担",
    ),
    "DDP": IncotermTerms(
        code="DDP",
        label="完税后交货",
        freight_included=True,
        duty_included=True,
        note="卖方承担运费与进口关税，报价已含税；再叠加关税即为重复计税",
    ),
}

# 建站初期的默认口径。默认值是「显式声明的默认」，不是「猜出来的默认」：
# 使用者未指定时，响应里必须回显实际采用的术语，否则调用方无从知道
# 自己的毛利是按哪种口径算的。
DEFAULT_INCOTERM = "FOB"


def terms(code: str) -> IncotermTerms:
    """取术语的条款定义。未知术语直接报错，不做兜底猜测。"""
    normalized = (code or "").strip().upper()
    if normalized not in TERMS:
        raise UnknownIncoterm(
            f"不支持的贸易术语: {code!r}；可选值 {list(Incoterm.ALL)}"
        )
    return TERMS[normalized]


def normalize(code: str | None) -> tuple[str, bool]:
    """规范化术语，并告知调用方是否用了默认值。

    返回 (术语, 是否采用默认值)。第二项必须回显到响应里——
    「用了默认」和「用户明确指定」在审计上不是一回事。
    """
    if code is None or not str(code).strip():
        return DEFAULT_INCOTERM, True
    return terms(str(code)).code, False
