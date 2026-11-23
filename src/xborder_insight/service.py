"""应用服务层：把领域能力组装成「一次完整业务动作」。

为什么单独一层而不是留在 api.py：
    同一件事（出一次选品决策）有两个入口——同步 HTTP 请求、异步任务执行器。
    如果逻辑留在控制器里，任务执行器要么 import api（分层倒了），要么复制一份
    （两份实现早晚不一致）。服务层让「业务动作」只有一个定义。

这一层只做组装与取数，不做判断：判断在 domain/，取数在 db/。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from .db import fba as fba_db
from .db import product_returns as returns_db
from .domain import admission as admission_mod
from .domain import compliance as compliance_mod
from .domain import decision as decision_mod
from .domain import package as package_mod
from .domain import profit as profit_mod
from .schemas import DecisionRequest


def _resolve_returns(
    conn: sqlite3.Connection, payload: DecisionRequest
) -> tuple[float | None, returns_db.ProductReturn | None]:
    """退货率从哪来：库里的两档，还是调用方自己给的基线。

    优先级只有两档，因为契约层已经把「两个都填」挡在 422：
    给了 `sku_key` 就一定走库，没给就用 `cost.return_rate`（可能仍是 None =
    调用方没指定，此时模型按 0 计入并把结论降级成上界，见 `docs/04` §31）。

    走到库这一档时，`require()` 找不到行会抛 `ReturnRateMissing`（503）——
    这是「没有这一台」，不是「有但没核实」。draft 行照常返回，证据强弱
    随结论一起输出（见 `db.product_returns` 模块注释）。
    """
    if payload.sku_key is None:
        return payload.cost.return_rate, None
    record = returns_db.require(conn, payload.sku_key, payload.marketplace)
    return record.baseline_rate, record


def _returns_block(
    record: returns_db.ProductReturn | None,
    rate: float | None,
    stress: admission_mod.StressResult | None,
) -> dict[str, object]:
    """响应的 `returns` 块。字段在两种模式下**同名同义**，客户端不必分支。

    为什么把证据强弱也放进来：`product_returns` 里两台都是 draft
    （一台借类目阈值、一台是假设）。只回一个 0.126 而不说它是什么，
    报告就会把「类目阈值」写成「这台的实际退货率」——差一整档可信度。
    """
    block: dict[str, object] = {
        "source": "product_returns" if record is not None else "explicit",
        "rate": None if rate is None else round(rate, 6),
        "sku_key": None if record is None else record.sku_key,
        "evidence": None,
        "missing_evidence": [],
        "stress": None,
    }
    if record is not None:
        block["evidence"] = {
            "kind": record.evidence_kind,
            "label": record.evidence_label,
            "status": record.status,
            "verified": record.verified,
            "usable": record.usable,
            "source_note": record.source_note,
        }
        block["missing_evidence"] = list(record.missing_evidence)
    if stress is not None:
        block["stress"] = stress.as_dict()
    return block


def _return_warnings(
    record: returns_db.ProductReturn | None,
    stress: admission_mod.StressResult | None,
    verdict: str,
) -> tuple[str, ...]:
    """把两件必须随结论一起读的事提到顶层 `warnings`。

    1. 这个退货率不是这一台的实测值（借来的或猜的）；
    2. 压力档转负——结论不改，但「能做什么」改了：只能做自然单，别在价格上花时间。

    写成顶层而不是塞进 `reasons`：`reasons` 回答的是「能不能做」，
    压力档回答的是「值不值得花时间谈价」（`docs/04` §22.7 两条线的分工）。
    混在一起会出现一条 `fail` 却不改变 verdict 的依据，读的人会以为结论算错了。
    """
    warnings: list[str] = []
    if record is not None and not record.usable:
        warnings.append(
            f"退货率 {record.baseline_rate:.1%} 不是这一台的实测值"
            f"（{record.evidence_label}）：对外引用这个毛利率时必须连这条一起写"
        )
    if stress is None:
        return tuple(warnings)
    if not stress.baseline_ok:
        warnings.append(
            f"基线档毛利已经是 {stress.baseline_margin:.2%}，压测没有意义："
            "先把采购价或箱规解决，再谈退货风险"
        )
    elif not stress.stress_ok:
        warnings.append(
            f"基线档 {stress.baseline_margin:.2%} 达标，但压力档（退货 "
            f"{stress.stress_returns:.0%}）转负到 {stress.stress_margin:.2%}："
            f"结论仍是 {verdict}，但退货风险会吃掉全部利润——"
            "先换杠杆（箱规 / 售价），别在价格上花时间"
        )
    return tuple(warnings)


def decide(conn: sqlite3.Connection, payload: DecisionRequest) -> dict:
    """按成本档案 + 合规规则表出一次决策。

    顺序固定：先定退货率（可能查表、可能 503），再定 FBA 费（可能查表算包络），
    再算利润（纯函数），再查合规（依赖库）。
    利润算不出来就直接抛错，不必为一个注定失败的请求去查合规表。

    调用方没给 fba_fee 时，这里用官方费率表算一个**区间**而不是填一个数：
    真实尺寸重量未知的情况下，区间是这个系统能给出的最强结论——
    它的两端都是官方数值，所以「最坏情况仍然达标」是可以直接采信的。
    """
    # 退货率先解析：它要么来自表（可能 503），要么来自入参。
    # 放在最前面是为了让「这一台没登记」比后续任何计算都更早暴露——
    # 后面的计算都要用到这个数，用它算完再报错等于白算。
    return_rate, record = _resolve_returns(conn, payload)

    cost = payload.cost
    envelope = None
    package = None
    fba_bounds = None
    if cost.fba_fee is None:
        if cost.uses_package_facts:
            # 只有三个判断题也能算：类别、尺寸段、计费重量都由事实推出来。
            package, envelope = fba_db.envelope_for_package(
                conn,
                payload.marketplace,
                package_mod.PackageFacts(
                    has_lithium_battery=cost.has_lithium_battery,  # type: ignore[arg-type]
                    unit_weight_oz=cost.unit_weight_oz,
                    dims_in=cost.package_dims_in,
                    fits_standard_size=cost.fits_standard_size,
                    volume_ceiling_in3=cost.package_volume_in3,
                ),
                selling_price=cost.selling_price,
                ship_date=cost.ship_date,
            )
        else:
            envelope = fba_db.envelope(
                conn,
                payload.marketplace,
                product_class=cost.product_class,
                selling_price=cost.selling_price,
                ship_date=cost.ship_date,
                size_tiers=cost.fba_size_tiers,
                weight_range_oz=cost.fba_weight_range_oz,
            )
        fba_bounds = (envelope.low_usd, envelope.high_usd)

    inputs = profit_mod.CostInputs(
        quoted_price=cost.quoted_price,
        selling_price=cost.selling_price,
        inbound_freight=cost.inbound_freight,
        duty_rate=cost.duty_rate,
        referral_rate=cost.referral_rate,
        fba_fee=cost.fba_fee,
        fba_bounds=fba_bounds,
        return_rate=return_rate,
        returns_handling_fee=cost.returns_handling_fee,
        returns_refund_admin_rate=cost.returns_refund_admin_rate,
        returns_goods_loss_ratio=cost.returns_goods_loss_ratio,
        returns_inbound_loss_ratio=cost.returns_inbound_loss_ratio,
        incoterm=cost.incoterm,
    )
    profit_result = profit_mod.compute(inputs)
    compliance_result = compliance_mod.load(conn, payload.category, payload.marketplace)

    result = decision_mod.decide(
        profit_result,
        compliance_result,
        payload.opportunity_score,
        data_provenance=payload.data_provenance,
    )
    body = result.as_dict()
    body["asin"] = payload.asin
    body["marketplace"] = payload.marketplace
    body["category"] = payload.category
    if envelope is not None:
        # 区间怎么来的要能自己核对：哪个版本、哪个价格带、上下界分别落在哪一档。
        body["fba_envelope"] = envelope.as_dict()
    if package is not None:
        # 类别与尺寸段是从哪三个事实推出来的，也要能核对。
        body["fba_package"] = package.as_dict()

    # 压力档：只重算一遍利润，不重跑 FBA 与合规——
    # 退货率只进利润模型，动它不会改变费率表或规则表的结论。
    # 用 `replace` 而不是新建 CostInputs：新建要抄 12 个字段，抄漏一个就是
    # 「压力档与基线档不是同一套参数下的对比」，差出来的数无法解释。
    stress = None
    if record is not None:
        stressed = profit_mod.compute(replace(inputs, return_rate=record.stress_rate))
        stress = admission_mod.StressResult.from_margins(
            baseline_returns=record.baseline_rate,
            stress_returns=record.stress_rate,
            baseline_margin=profit_result.gross_margin,
            stress_margin=stressed.gross_margin,
        )
        # FBA 未知时两端都是区间下界（`profit` 的 `gross_margin` 取保守端），
        # 所以这个判定是「最坏尺寸重量 + 压力档退货率」下的判定——
        # 最保守的那个组合，结论为真时可以直接采信。
    body["returns"] = _returns_block(record, return_rate, stress)
    body["warnings"] = list(_cost_warnings(profit_result)) + list(
        _return_warnings(record, stress, str(body["verdict"]))
    )
    return body


def _cost_warnings(profit_result: profit_mod.ProfitResult) -> tuple[str, ...]:
    """成本项缺省时，把「算出来的是上界」提到顶层。

    只写在 `profit.warnings` 里不够：那份列表是按字段逐条列的，
    读结论的人（尤其是只看 `verdict` 与 `warnings` 的）会漏掉
    「这个毛利率本身不可当作结论用」这层意思。
    """
    if not profit_result.missing_inputs:
        return ()
    return (
        "成本项未指定："
        + "、".join(profit_result.missing_labels)
        + "——本次按 0 计入，得到的是最乐观上界；"
        "结论最多到 NEED_MORE_INFO，补齐后才可能给出 GO",
    )
