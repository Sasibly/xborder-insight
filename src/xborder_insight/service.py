"""应用服务层：把领域能力组装成「一次完整业务动作」。

为什么单独一层而不是留在 api.py：
    同一件事（出一次选品决策）有两个入口——同步 HTTP 请求、异步任务执行器。
    如果逻辑留在控制器里，任务执行器要么 import api（分层倒了），要么复制一份
    （两份实现早晚不一致）。服务层让「业务动作」只有一个定义。

这一层只做组装与取数，不做判断：判断在 domain/，取数在 db/。
"""

from __future__ import annotations

import sqlite3

from .db import fba as fba_db
from .domain import compliance as compliance_mod
from .domain import decision as decision_mod
from .domain import package as package_mod
from .domain import profit as profit_mod
from .schemas import DecisionRequest


def decide(conn: sqlite3.Connection, payload: DecisionRequest) -> dict:
    """按成本档案 + 合规规则表出一次决策。

    顺序固定：先定 FBA 费（可能查表算包络），再算利润（纯函数），再查合规（依赖库）。
    利润算不出来就直接抛错，不必为一个注定失败的请求去查合规表。

    调用方没给 fba_fee 时，这里用官方费率表算一个**区间**而不是填一个数：
    真实尺寸重量未知的情况下，区间是这个系统能给出的最强结论——
    它的两端都是官方数值，所以「最坏情况仍然达标」是可以直接采信的。
    """
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
        return_rate=cost.return_rate,
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
    return body
