"""运输方式：这单能不能发、要哪些文件、报价单还缺什么。

为什么这不是「给订单打个标签」：
    含锂与否只被用在 FBA 费率表上（危险品表 vs 普货表），那是**到仓之后**的费用。
    头程这一段完全没有维度承载「货里有没有电池」，于是系统可以一边用危险品表算仓配、
    一边默认按普货订舱——两句话都对，合起来是一个发不出去的方案。
    这类错误在输出上没有任何痕迹：毛利算得出来，结论看着正常，货到机场才被拒。

做法：把「允许什么货」写成一个布尔事实，让非法组合在**算钱之前**就被拒。

    allows_lithium=False 的模式（普货空运）遇到含锂货 → 不给结论，而不是给个数字。

口径状态是 draft：
    PI967 / PI966 / PI965 三档、「≤100Wh 可走带电渠道」这些来自 IATA DGR 的行业转述
    （2026-11-21 检索的货代文章），**不是** IATA 或民航局原文，也未经本项目逐条核实。
    所以这里只写「哪种组合允许、需要哪几份文件」——这些是二值判断；
    不写时效天数、不写单价，那两个数必须由货代报价单给出。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 一张头程报价单必须给出的字段。少一项就没法把运费落进利润模型：
#: 单价决定可变成本，最低收费决定小批量是否失真，时效决定资金占用与补货节奏。
QUOTE_FIELDS = (
    "单价与计费单位（$/kg 或 $/CBM）",
    "最低收费（不足起运量时的保底）",
    "时效（天，门到门）",
    "报价单编号",
    "有效期至",
)


class UnknownTransportMode(ValueError):
    """给了不存在的运输方式。"""


@dataclass(frozen=True)
class TransportMode:
    """一种运输方式：允许装什么货、含锂时需要哪些文件、依据是哪一条。"""

    code: str
    label: str
    allows_lithium: bool
    lithium_docs: tuple[str, ...]
    basis: str
    status: str = "draft"


TRANSPORT_MODES: dict[str, TransportMode] = {
    "air_battery": TransportMode(
        code="air_battery",
        label="带电渠道空运",
        allows_lithium=True,
        lithium_docs=(
            "UN38.3 测试报告",
            "MSDS（GHS 格式，标注 Wh 与 SOC）",
            "航空运输条件鉴定书",
        ),
        basis=(
            "UN3481 / PI967：电池内置且不可拆卸、额定能量 ≤100Wh，"
            "可按带电渠道配载（客机腹舱可承运）"
        ),
    ),
    "sea": TransportMode(
        code="sea",
        label="海运",
        allows_lithium=True,
        lithium_docs=(
            "UN38.3 测试报告",
            "MSDS（GHS 格式）",
            "危包证 / 危险品申报（按船公司要求，须货代确认）",
        ),
        basis="IMDG 规则；内置电池设备是否可按豁免条款申报，须以船公司/货代确认为准",
    ),
    "air_general": TransportMode(
        code="air_general",
        label="普货空运",
        allows_lithium=False,
        lithium_docs=(),
        basis="普货申报不得夹带锂电池；伪报属违规，后果是退仓、罚款并留下记录",
    ),
}


@dataclass(frozen=True)
class TransportCheck:
    """某个「货 + 运输方式」组合的可行性结论。"""

    mode: TransportMode
    has_lithium_battery: bool
    ok: bool
    reason: str | None
    required_docs: tuple[str, ...]
    warnings: tuple[str, ...]
    quote_fields: tuple[str, ...] = QUOTE_FIELDS

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.code,
            "mode_label": self.mode.label,
            "has_lithium_battery": self.has_lithium_battery,
            "ok": self.ok,
            "reason": self.reason,
            "required_docs": list(self.required_docs),
            "basis": self.mode.basis,
            "status": self.mode.status,
            "warnings": list(self.warnings),
            "quote_fields": list(self.quote_fields),
        }


def check(mode: str, *, has_lithium_battery: bool) -> TransportCheck:
    """判断「这批货 + 这个运输方式」是否成立。

    不成立的组合返回 `ok=False` 并给出原因，**不抛异常**：
    调用方需要拿到结论去决定「换运输方式」还是「改电池设计」，
    这不是输入错误，是一笔不能做的生意。
    """
    spec = TRANSPORT_MODES.get(mode)
    if spec is None:
        raise UnknownTransportMode(
            f"未知的运输方式: {mode!r}。可选值: {', '.join(TRANSPORT_MODES)}"
        )

    if has_lithium_battery and not spec.allows_lithium:
        return TransportCheck(
            mode=spec,
            has_lithium_battery=True,
            ok=False,
            reason=(
                f"{spec.label}不允许含锂电池的货：按普货申报属伪报，"
                "安检或海关查出会退仓、罚款并留下记录。"
                "含锂货请改走 air_battery（带电渠道空运）或 sea（海运）"
            ),
            required_docs=(),
            warnings=(),
        )

    docs = spec.lithium_docs if has_lithium_battery else ()
    warnings: list[str] = []
    if not has_lithium_battery and spec.code == "air_battery":
        warnings.append(
            "普货走带电渠道：可行，但会付带电渠道的溢价。"
            "这一条不是错误，只是钱——确认该 SKU 确实不含电池后可以换回 air_general"
        )
    if has_lithium_battery:
        warnings.append(
            "含锂判定看 BOM，不看默认使用方式：支持「插电 + 无线」双模的产品，"
            "电池是产品的一部分，不能按普货申报"
        )

    return TransportCheck(
        mode=spec,
        has_lithium_battery=has_lithium_battery,
        ok=True,
        reason=None,
        required_docs=docs,
        warnings=tuple(warnings),
    )
