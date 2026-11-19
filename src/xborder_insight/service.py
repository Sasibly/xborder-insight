"""应用服务层：把领域能力组装成「一次完整业务动作」。

为什么单独一层而不是留在 api.py：
    同一件事（出一次选品决策）有两个入口——同步 HTTP 请求、异步任务执行器。
    如果逻辑留在控制器里，任务执行器要么 import api（分层倒了），要么复制一份
    （两份实现早晚不一致）。服务层让「业务动作」只有一个定义。

这一层只做组装与取数，不做判断：判断在 domain/，取数在 db/。
"""

from __future__ import annotations

import sqlite3

from .domain import compliance as compliance_mod
from .domain import decision as decision_mod
from .domain import profit as profit_mod
from .schemas import DecisionRequest


def decide(conn: sqlite3.Connection, payload: DecisionRequest) -> dict:
    """按成本档案 + 合规规则表出一次决策。

    顺序固定：先算利润（纯函数，不依赖库），再查合规（依赖库）。
    利润算不出来就直接抛错，不必为一个注定失败的请求去查库。
    """
    cost = payload.cost
    inputs = profit_mod.CostInputs(
        quoted_price=cost.quoted_price,
        selling_price=cost.selling_price,
        inbound_freight=cost.inbound_freight,
        duty_rate=cost.duty_rate,
        referral_rate=cost.referral_rate,
        fba_fee=cost.fba_fee,
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
    return body
