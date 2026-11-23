"""决策接口的 HTTP 契约回归测试。

关注三件事：
1. 入参不合法必须在 422 层被拒，且错误信息指向具体字段（不是笼统的「参数错误」）
2. 数据库未初始化要报 503 + 修复命令，不能是 500
3. 响应结构稳定——字段名变更会直接打坏看板与外部调用方
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xborder_insight import api
from xborder_insight.db import migrate
from xborder_insight.db.connection import connect

VALID = {
    "asin": "B0TEST0001",
    "category": "wireless_charger",
    "marketplace": "US",
    "opportunity_score": 0.66,
    "data_provenance": "test_fixture",
    "cost": {
        "quoted_price": 8.00,
        "selling_price": 29.99,
        "inbound_freight": 1.20,
        "duty_rate": 0.075,
        "referral_rate": 0.15,
        "fba_fee": 3.50,
        "return_rate": 0.08,
        "returns_handling_fee": 0.80,
        "returns_refund_admin_rate": 0.20,
        "returns_goods_loss_ratio": 0.30,
        "returns_inbound_loss_ratio": 1.00,
        "incoterm": "FOB",
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """把 DB_FILE 指到临时库，避免测试污染 data/app.sqlite3。"""
    db_file = tmp_path / "api_test.sqlite3"
    conn = connect(db_file)
    migrate.upgrade(conn)
    conn.close()

    monkeypatch.setattr(api, "DB_FILE", db_file)
    with TestClient(api.app) as test_client:
        yield test_client


def _post(client, payload):
    return client.post("/v1/decision", json=payload)


def test_decision_returns_full_structure(client):
    response = _post(client, VALID)
    assert response.status_code == 200

    body = response.json()
    assert set(body) >= {
        "asin",
        "verdict",
        "headline",
        "profit",
        "compliance",
        "opportunity_score",
        "reasons",
        "data_provenance",
        "thresholds",
    }
    assert body["asin"] == "B0TEST0001"
    assert body["data_provenance"] == "test_fixture"
    # 核算口径要能被外部看到，否则「这个毛利率怎么来的」无法当场回答
    assert body["profit"]["incoterm"] == "FOB"
    assert "assumptions" in body["profit"]


def test_incoterm_default_is_echoed(client):
    """不传术语时按 FOB 算，且必须回显 incoterm_assumed=true。

    默认值本身不是问题，「用了默认却不告诉你」才是。
    """
    payload = {**VALID, "cost": {**VALID["cost"]}}
    payload["cost"].pop("incoterm")

    body = _post(client, payload).json()
    assert body["profit"]["incoterm"] == "FOB"
    assert body["profit"]["incoterm_assumed"] is True


def test_unknown_category_yields_need_more_info(client):
    """规则表没有的品类，在利润/需求达标后应落到「需补充信息」。

    不能因为「查不到规则」就判 GO——那是把未知当无风险。
    """
    payload = {**VALID, "category": "no_such_category"}
    body = _post(client, payload).json()

    assert body["compliance"]["status"] == "unknown"
    assert body["verdict"] in {"NO_GO", "NEED_MORE_INFO"}


def test_seed_category_is_blocked_by_draft_rules(client):
    """当前种子仍有未核实的 blocker（FCC ID 标注取决于产品是否含发射机）→ 必须 NO_GO。

    这条断言是「不许编数」在接口层的样子：只要还差一条没核实的阻断项，就不给绿灯。
    """
    body = _post(client, VALID).json()
    assert body["compliance"]["status"] == "blocked"
    assert body["verdict"] == "NO_GO"
    assert len(body["compliance"]["blockers"]) == 2
    assert sum(1 for hit in body["compliance"]["blockers"] if not hit["verified"]) == 1


# ---------- 入参校验 ----------


def test_bad_incoterm_rejected_with_field_name(client):
    payload = {**VALID, "cost": {**VALID["cost"], "incoterm": "FOBb"}}
    response = _post(client, payload)

    assert response.status_code == 422
    assert "incoterm" in response.json()["message"]


def test_zero_price_rejected(client):
    payload = {**VALID, "cost": {**VALID["cost"], "selling_price": 0}}
    assert _post(client, payload).status_code == 422


def test_return_rate_above_one_rejected(client):
    payload = {**VALID, "cost": {**VALID["cost"], "return_rate": 8}}
    assert _post(client, payload).status_code == 422


def test_extra_field_rejected(client):
    """契约禁止多余字段：静默忽略会让调用方以为自己传的参数生效了。"""
    payload = {**VALID, "unexpected": 1}
    assert _post(client, payload).status_code == 422


def test_missing_cost_rejected(client):
    payload = {key: value for key, value in VALID.items() if key != "cost"}
    assert _post(client, payload).status_code == 422


def test_request_id_echoed_on_error(client):
    payload = {**VALID, "cost": {**VALID["cost"], "selling_price": -1}}
    response = _post(client, payload)
    assert response.status_code == 422
    assert response.json()["request_id"] == response.headers["X-Request-Id"]


# ---------- 数据库未初始化 ----------


def test_missing_database_returns_503_with_fix_hint(tmp_path, monkeypatch):
    """库不存在时报 503 且给出修复命令。

    报 500 会让调用方以为是代码 bug，实际只是少跑了一条迁移命令。
    """
    monkeypatch.setattr(api, "DB_FILE", tmp_path / "does_not_exist.sqlite3")
    with TestClient(api.app) as test_client:
        response = test_client.post("/v1/decision", json=VALID)

    assert response.status_code == 503
    assert response.json()["code"] == "db_not_ready"


def test_ready_fails_when_database_missing(tmp_path, monkeypatch):
    """就绪检查要覆盖数据库，不能等到业务请求才暴露。"""
    monkeypatch.setattr(api, "DB_FILE", tmp_path / "missing.sqlite3")
    with TestClient(api.app) as test_client:
        response = test_client.get("/ready")

    assert response.status_code == 503


# ---------- 确定性 ----------


def test_same_request_same_response(client):
    """同输入同输出，接口层也要保证。"""
    first = _post(client, VALID).json()
    second = _post(client, VALID).json()
    assert first == second


# ---------- 缺省成本项：算出来的是上界，不得称 PASS（docs/04 §31） ----------
#
# H17 之前，`cost` 里六项成本都有默认值 0，漏填与「这一项真的是 0」在契约层
# 就分不出来。咖啡机只填售价与采购价能算出 30.99%，补齐三项后是 6.90%，
# 而中间那条路径一路 200、不报错。现在漏填是 None，接口必须自己说出来。


#: 只填「报价阶段一定有的那两个数」。这是最容易发生的一种调用。
THIN_COST = {
    "quoted_price": 18.00,
    "selling_price": 59.99,
    "referral_rate": 0.15,
    "fba_fee": 14.40,
}


def test_omitted_cost_fields_are_named_in_the_response(client):
    """漏填要能被看见：字段名进 `missing_inputs`，一句人话进顶层 warnings。

    不是「返回 422 让你补」——报价阶段确实可能还没拿到头程；
    接口该做的是把结论降级并说清缺什么，而不是拒绝服务或按 0 静默算完。
    """
    payload = {**VALID, "cost": THIN_COST}
    body = _post(client, payload).json()

    assert body["profit"]["missing_inputs"] == ["return_rate", "inbound_freight", "duty_rate"]
    assert any("成本项未指定" in w for w in body["warnings"])
    # 退货率这一路也必须是 None，而不是被填成 0
    assert body["returns"]["source"] == "explicit"
    assert body["returns"]["rate"] is None


def test_optimistic_margin_cannot_be_reported_as_a_pass(client):
    """同一件货：只填两个数 → NEED_MORE_INFO；补齐三个数 → NO_GO。

    左边这个数（31.0%）比右边（10.3%）好看得多，而它之所以好看，
    仅仅是因为三项成本被按 0 算了。以前这一步直接是 GO。
    """
    thin = {**VALID, "category": "desk_lamp", "cost": THIN_COST}
    full = {
        **VALID,
        "category": "desk_lamp",
        "cost": {
            **THIN_COST,
            "inbound_freight": 2.50,
            "duty_rate": 0.237,
            "return_rate": 0.08,
            "returns_handling_fee": 3.00,
            "returns_goods_loss_ratio": 0.30,
            "returns_inbound_loss_ratio": 0.20,
            "incoterm": "FOB",
        },
    }

    thin_body = _post(client, thin).json()
    full_body = _post(client, full).json()

    assert thin_body["verdict"] == "NEED_MORE_INFO"
    assert "成本项未指定" in thin_body["headline"]
    assert full_body["verdict"] == "NO_GO"
    assert thin_body["profit"]["gross_margin"] > full_body["profit"]["gross_margin"] + 0.20


def test_explicit_zero_is_a_declaration_not_an_omission(client):
    """显式写 0 = 「这一项确实是 0」，不算缺省，也就不降级。

    两者在数值上一样，区别只在「谁说的」。分不清这两件事，
    补数据的人就永远不知道自己是在补一个空，还是在改一个声明。
    """
    payload = {
        **VALID,
        "cost": {
            **THIN_COST,
            "inbound_freight": 0.0,
            "duty_rate": 0.0,
            "return_rate": 0.0,
            "returns_handling_fee": 0.0,
            "returns_goods_loss_ratio": 0.0,
            "returns_inbound_loss_ratio": 0.0,
            "incoterm": "FOB",
        },
    }
    body = _post(client, payload).json()
    assert body["profit"]["missing_inputs"] == []
    assert not any("成本项未指定" in w for w in body["warnings"])
