"""运输方式：这单能不能发、要哪些文件、报价单还缺什么。

为什么这不是「给订单打个标签」：
    含锂与否只被用在 FBA 费率表上（危险品表 vs 普货表），那是**到仓之后**的费用。
    头程这一段完全没有维度承载「货里有没有电池」，于是系统可以一边用危险品表算仓配、
    一边默认按普货订舱——两句话都对，合起来是一个发不出去的方案。
    这类错误在输出上没有任何痕迹：毛利算得出来，结论看着正常，货到机场才被拒。

做法：把「允许什么货」写成一个布尔事实，让非法组合在**算钱之前**就被拒。

    allows_lithium=False 的模式（普货空运）遇到含锂货 → 不给结论，而不是给个数字。

第二维：**电池在货里是什么形态**（`BatteryInstallation`）
    同样含锂，电池焊在机身里（PI967）和可以拔出来单独放（PI966）、以及裸电池（PI965），
    在空运上不是一回事：PI967 可按客机腹舱配载、只要三份文件；PI966 要 DGD 与危包证、
    优先全货机；PI965 客机禁运。
    所以「电池形态」不能有一个乐观的默认值。以前 `air_battery` 的 basis 直接写死了
    PI967——那是把「没确认」当成了「确认是内置」。现在它是显式入参：

        未给形态 + 含锂 + 空运  →  verdict=need_info，不给可发结论
        未给形态 + 含锂 + 海运  →  结论不翻转（两种形态都能走），但文件按最严档列

    这条分界的理由：need_info 只在**结论会翻转**时用。海运两种形态都发得出去，
    只是文件多少不同，那属于「取最严档」；空运则会从「可客机腹舱」翻成「客机禁运」，
    用最严档糊过去等于替客户做了他没做的确认。

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

VERDICT_OK = "ok"
VERDICT_BLOCKED = "blocked"
VERDICT_NEED_INFO = "need_info"

VERDICT_LABELS = {
    VERDICT_OK: "可发",
    VERDICT_BLOCKED: "不可发",
    VERDICT_NEED_INFO: "需先确认电池形态",
}

#: 含锂电池货物在任何渠道都要的两份（设备类通用）。
BASE_LITHIUM_DOCS = (
    "UN38.3 测试报告",
    "MSDS（GHS 格式，标注 Wh 与 SOC）",
)

#: 空运额外要的：证明这批货按什么条件被接受上飞机。
AIR_LITHIUM_DOCS = ("航空运输条件鉴定书",)

#: 海运额外要的（措辞保留「按船公司要求」——豁免条款各家不同，不能替船公司下结论）。
#: 刻意不写死「危包证」：形态可拆时下面 DG_DECLARATION_DOCS 会明确列出，
#: 这里再写一遍就成了同一份文件两个名字，读的人会以为是两样东西。
SEA_LITHIUM_DOCS = ("危险品订舱 / 申报（按船公司要求，须货代确认）",)

#: 形态不是「内置不可拆卸」时要多出来的两样：包装鉴定 + 申报单。
#: 这两样只能由持证申报员/包装厂出具，打样阶段就要排期，订舱时才发现就晚了。
DG_DECLARATION_DOCS = (
    "危包证（出境货物运输包装使用鉴定结果单）",
    "DGD 危险品申报单（须持证申报员签署）",
)


class UnknownTransportMode(ValueError):
    """给了不存在的运输方式。"""


class UnknownBatteryInstallation(ValueError):
    """给了不存在的电池形态。"""


@dataclass(frozen=True)
class BatteryInstallation:
    """电池在货里的形态：决定 PI 条款、能否上客机、要哪些申报文件。"""

    code: str
    label: str
    packing_instruction: str
    passenger_aircraft_ok: bool
    needs_dg_declaration: bool
    note: str


BATTERY_INSTALLATIONS: dict[str, BatteryInstallation] = {
    "built_in": BatteryInstallation(
        code="built_in",
        label="内置且不可拆卸（电池焊死在机身里）",
        packing_instruction="PI967",
        passenger_aircraft_ok=True,
        needs_dg_declaration=False,
        note="最宽松的一档：≤100Wh 可按带电渠道配载，客机腹舱可承运，只要三份文件",
    ),
    "removable": BatteryInstallation(
        code="removable",
        label="配套可拆卸（设备 + 可取出的电池，同箱）",
        packing_instruction="PI966",
        passenger_aircraft_ok=True,
        needs_dg_declaration=True,
        note="比 PI967 严一档：要 DGD 与危包证，货代通常优先排全货机，时效与单价都会变",
    ),
    "standalone": BatteryInstallation(
        code="standalone",
        label="纯电池 / 电池单独运输（不与设备同箱）",
        packing_instruction="PI965",
        passenger_aircraft_ok=False,
        needs_dg_declaration=True,
        note="客机禁运，仅全货机；本工具按客机腹舱口径设计，这一档不能直接套用",
    ),
}


def installation_of(code: str | None) -> BatteryInstallation | None:
    """把 CLI/接口传来的字符串转成形态；None 表示「还没确认」。"""
    if code is None:
        return None
    spec = BATTERY_INSTALLATIONS.get(code)
    if spec is None:
        raise UnknownBatteryInstallation(
            f"未知的电池形态: {code!r}。可选值: {', '.join(BATTERY_INSTALLATIONS)}"
        )
    return spec


@dataclass(frozen=True)
class TransportMode:
    """一种运输方式：允许装什么货、含锂时需要哪些文件、依据是哪一条。"""

    code: str
    label: str
    allows_lithium: bool
    basis: str
    status: str = "draft"


TRANSPORT_MODES: dict[str, TransportMode] = {
    "air_battery": TransportMode(
        code="air_battery",
        label="带电渠道空运",
        allows_lithium=True,
        basis=(
            "UN3481（电池与设备同装）：内置不可拆卸走 PI967、配套可拆卸走 PI966，"
            "额定能量 ≤100Wh 时可按带电渠道配载。纯电池走 UN3480 / PI965，客机禁运"
        ),
    ),
    "sea": TransportMode(
        code="sea",
        label="海运",
        allows_lithium=True,
        basis="IMDG 规则；内置电池设备是否可按豁免条款申报，须以船公司/货代确认为准",
    ),
    "air_general": TransportMode(
        code="air_general",
        label="普货空运",
        allows_lithium=False,
        basis="普货申报不得夹带锂电池；伪报属违规，后果是退仓、罚款并留下记录",
    ),
}


def required_documents(
    mode_code: str,
    installation: BatteryInstallation | None,
    *,
    strict: bool = False,
) -> tuple[str, ...]:
    """这批货走这条渠道要备哪些文件。

    `strict=True` 表示形态未确认、按最严档列：把 DGD 与危包证一起列出来。
    宁可多备一份，也不要订舱时才发现缺一份——但结论是否成立不因此改变。
    """
    docs = list(BASE_LITHIUM_DOCS)
    if mode_code == "air_battery":
        docs.extend(AIR_LITHIUM_DOCS)
    elif mode_code == "sea":
        docs.extend(SEA_LITHIUM_DOCS)

    needs_declaration = strict or (
        installation is not None and installation.needs_dg_declaration
    )
    if needs_declaration:
        docs.extend(DG_DECLARATION_DOCS)
    return tuple(docs)


@dataclass(frozen=True)
class TransportCheck:
    """某个「货 + 运输方式（+ 电池形态）」组合的可行性结论。"""

    mode: TransportMode
    has_lithium_battery: bool
    verdict: str
    reason: str | None
    required_docs: tuple[str, ...]
    warnings: tuple[str, ...]
    installation: BatteryInstallation | None = None
    quote_fields: tuple[str, ...] = QUOTE_FIELDS

    @property
    def ok(self) -> bool:
        """能不能照这个方案直接走。need_info 不算 ok——没确认就不放行。"""
        return self.verdict == VERDICT_OK

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABELS[self.verdict]

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.code,
            "mode_label": self.mode.label,
            "has_lithium_battery": self.has_lithium_battery,
            "battery_installation": (
                self.installation.code if self.installation else None
            ),
            "packing_instruction": (
                self.installation.packing_instruction if self.installation else None
            ),
            "verdict": self.verdict,
            "ok": self.ok,
            "reason": self.reason,
            "required_docs": list(self.required_docs),
            "basis": self.mode.basis,
            "status": self.mode.status,
            "warnings": list(self.warnings),
            "quote_fields": list(self.quote_fields),
        }


def check(
    mode: str,
    *,
    has_lithium_battery: bool,
    battery_installation: str | None = None,
) -> TransportCheck:
    """判断「这批货 + 这个运输方式」是否成立。

    不成立的组合返回 `verdict=blocked` 并给出原因，**不抛异常**：
    调用方需要拿到结论去决定「换运输方式」还是「改电池设计」，
    这不是输入错误，是一笔不能做的生意。

    含锂但没说清电池形态时返回 `verdict=need_info`——空运的结论会随形态翻转
    （PI967 可客机腹舱 / PI965 客机禁运），不能替调用方默认成最宽松的那种。
    """
    spec = TRANSPORT_MODES.get(mode)
    if spec is None:
        raise UnknownTransportMode(
            f"未知的运输方式: {mode!r}。可选值: {', '.join(TRANSPORT_MODES)}"
        )
    installation = installation_of(battery_installation)

    if has_lithium_battery and not spec.allows_lithium:
        return TransportCheck(
            mode=spec,
            has_lithium_battery=True,
            verdict=VERDICT_BLOCKED,
            reason=(
                f"{spec.label}不允许含锂电池的货：按普货申报属伪报，"
                "安检或海关查出会退仓、罚款并留下记录。"
                "含锂货请改走 air_battery（带电渠道空运）或 sea（海运）"
            ),
            required_docs=(),
            warnings=(),
            installation=installation,
        )

    if not has_lithium_battery:
        warnings: list[str] = []
        if spec.code == "air_battery":
            warnings.append(
                "普货走带电渠道：可行，但会付带电渠道的溢价。"
                "这一条不是错误，只是钱——确认该 SKU 确实不含电池后可以换回 air_general"
            )
        return TransportCheck(
            mode=spec,
            has_lithium_battery=False,
            verdict=VERDICT_OK,
            reason=None,
            required_docs=(),
            warnings=tuple(warnings),
        )

    # ---- 以下为「含锂 + 该渠道允许含锂」 ----
    common_warnings = [
        "含锂判定看 BOM，不看默认使用方式：支持「插电 + 无线」双模的产品，"
        "电池是产品的一部分，不能按普货申报"
    ]

    if installation is None and spec.code == "air_battery":
        return TransportCheck(
            mode=spec,
            has_lithium_battery=True,
            verdict=VERDICT_NEED_INFO,
            reason=(
                "不知道电池是「内置不可拆卸」还是「可拆卸配套」，空运结论会翻转："
                "内置不可拆（PI967）可按客机腹舱配载、三份文件即可；"
                "可拆配套（PI966）要 DGD 与危包证、优先全货机；"
                "纯电池（PI965）客机禁运。未确认前不给可发结论"
            ),
            required_docs=required_documents(spec.code, None, strict=True),
            warnings=tuple(
                common_warnings
                + [
                    "下面这份文件清单是**最严档**（按可拆卸形态列的），"
                    "确认内置不可拆后可去掉 DGD 与危包证两项"
                ]
            ),
            installation=None,
        )

    if (
        installation is not None
        and spec.code == "air_battery"
        and not installation.passenger_aircraft_ok
    ):
        return TransportCheck(
            mode=spec,
            has_lithium_battery=True,
            verdict=VERDICT_BLOCKED,
            reason=(
                f"{installation.label}属 {installation.packing_instruction}，客机禁运。"
                "带电渠道空运按客机腹舱口径设计，这一档须改走全货机危险品渠道或海运，"
                "不能套用本渠道"
            ),
            required_docs=(),
            warnings=tuple(common_warnings),
            installation=installation,
        )

    warnings = list(common_warnings)
    if installation is None:
        warnings.append(
            "电池形态未确认：海运两种形态都发得出去、结论不翻转，"
            "但可拆卸形态要多备 DGD 与危包证。文件已按最严档列出，"
            "确认内置不可拆后可去掉那两项"
        )
    elif installation.needs_dg_declaration:
        warnings.append(
            f"{installation.packing_instruction}：{installation.note}。"
            "DGD 与危包证出具需要排期，别等订舱时才申请"
        )

    return TransportCheck(
        mode=spec,
        has_lithium_battery=True,
        verdict=VERDICT_OK,
        reason=None,
        # 形态未确认（只会是海运：空运那种已在上面拦成 need_info）→ 文件取最严档
        required_docs=required_documents(
            spec.code, installation, strict=installation is None,
        ),
        warnings=tuple(warnings),
        installation=installation,
    )
