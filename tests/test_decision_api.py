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
    """当前种子的无线充电器规则含 2 条 draft blocker → 必须 NO_GO。

    这条断言是「不许编数」在接口层的样子：规则没核实，就不给绿灯。
    """
    body = _post(client, VALID).json()
    assert body["compliance"]["status"] == "blocked"
    assert body["verdict"] == "NO_GO"
    assert len(body["compliance"]["blockers"]) == 2
    assert all(not hit["verified"] for hit in body["compliance"]["blockers"])


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
