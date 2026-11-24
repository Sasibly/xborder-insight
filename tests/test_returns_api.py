"""决策接口接上逐 SKU 退货率（`docs/04` §30）。

`product_returns` 建好之后，这张表只有 CLI 和测试在用；决策接口还只读
`cost.return_rate` 一个数。这一组用例钉住三件事：

1. **接口不是第二个真相源**：`sku_key` 走表算出来的毛利与压力档，
   必须与人手复算的 `docs/04` §28.4 逐位一致（14.35% / 9.18%）；
2. **两个来源互斥**，且「这一台没登记」是 503 而不是拿默认值替它猜；
3. **证据强弱随结论一起回**：两台都是 draft（一台借类目阈值、一台是假设），
   只回一个 0.126 而不说它是什么，报告就会把「类目阈值」写成「这台的实测值」。

为什么单开一个文件而不是塞进 `test_decision_api.py`：那组用例测的是
「入参合法性 + 三档结论」的契约，用的是不依赖库内 SKU 的假数据；
这一组反过来，**必须**依赖 `m0014` 的种子两行才能测。混在一起会让
「哪些用例会因为种子数据变动而红」变得看不出来。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from xborder_insight import api
from xborder_insight.db import migrate
from xborder_insight.db.connection import connect
from xborder_insight.jobs.runner import JobRunner
from xborder_insight.llm import build_llm_client

PURIFIER_SKU = "purifier_desk_1752"
COFFEE_SKU = "coffee_semi_1613"

#: 净化器成交价口径（`docs/04` §28.3）：$24.30 / 售价 $79.99 / 关税 12.5% /
#: 头程 $6.00 / 佣金 15% / FBA $11.35。**不含 return_rate**——
#: 基线档由 `sku_key` 从 `product_returns` 取（12.6%）。
PURIFIER = {
    "asin": "B0TEST0001",
    "sku_key": PURIFIER_SKU,
    "category": "wireless_charger",
    "marketplace": "US",
    "opportunity_score": 0.66,
    "data_provenance": "test_fixture",
    "cost": {
        "quoted_price": 24.30,
        "selling_price": 79.99,
        "inbound_freight": 6.00,
        "duty_rate": 0.125,
        "referral_rate": 0.15,
        "fba_fee": 11.35,
        "returns_handling_fee": 3.00,
        "returns_refund_admin_rate": 0.20,
        "returns_goods_loss_ratio": 0.30,
        "returns_inbound_loss_ratio": 0.20,
        "incoterm": "FOB",
    },
}


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "returns_api.sqlite3"
    conn = connect(path)
    migrate.upgrade(conn)  # m0014 的两行种子在这里进来
    conn.close()
    return path


@pytest.fixture
def client(db_file, monkeypatch):
    monkeypatch.setattr(api, "DB_FILE", db_file)
    # 关掉后台 worker：需要任务路径的用例自己用 drain() 推进，
    # 否则断言会变成「等一会儿再看看」。
    monkeypatch.setattr(api, "settings", replace(api.settings, jobs_worker_enabled=False))
    with TestClient(api.app) as test_client:
        yield test_client


def _post(client, payload):
    return client.post("/v1/decision", json=payload)


# ---------- 走表：数字必须与文档逐位一致 ----------


def test_sku_key_reads_baseline_and_stress_from_the_table(client):
    """$24.30 这一单：基线 12.6% → 14.35%，压力档 17% → 9.18%（`docs/04` §28.4）。

    这两个数是谈判与「值不值得谈」判定的依据。接口若自己算一遍，
    迟早会和文档、CLI 三处各说一套——所以这里断言的是**逐位一致**，
    不是「大概这个量级」。
    """
    body = _post(client, PURIFIER).json()

    assert body["returns"]["source"] == "product_returns"
    assert body["returns"]["sku_key"] == PURIFIER_SKU
    assert body["returns"]["rate"] == pytest.approx(0.126)

    # 利润用的是表里的 12.6%，不是入参默认的 0：14.35% 只在这个口径下成立。
    assert body["profit"]["gross_margin"] == pytest.approx(0.1435, abs=5e-4)

    stress = body["returns"]["stress"]
    assert stress["baseline_returns"] == pytest.approx(0.126)
    assert stress["stress_returns"] == pytest.approx(0.17)
    assert stress["baseline_margin"] == pytest.approx(0.1435, abs=5e-4)
    assert stress["stress_margin"] == pytest.approx(0.0918, abs=5e-4)
    assert stress["margin_drop_pts"] == pytest.approx(5.16, abs=0.02)
    # 两档都为正 → 值得谈（`docs/04` §25.1 的规则，判定由 domain 给，不是接口自己判）
    assert stress["baseline_ok"] is True
    assert stress["stress_ok"] is True
    assert stress["worth_negotiating"] is True
    assert stress["label"] == "值得谈"


def test_evidence_strength_travels_with_the_conclusion(client):
    """净化器那行是 `category_threshold`：借的是类目阈值，不是这一台的实测值。

    证据必须随结论一起回。只回一个 12.6%，读的人无法区分「读到的」和「借来的」——
    而这正是 `m0014` 拆出第四档的原因。
    """
    body = _post(client, PURIFIER).json()

    evidence = body["returns"]["evidence"]
    assert evidence["kind"] == "category_threshold"
    assert evidence["status"] == "draft"
    assert evidence["usable"] is False
    assert evidence["verified"] is False
    assert "未核实" in evidence["label"]

    # 待办要能执行：说清缺的是「该 ASIN 的实际退货率」，不是笼统的「证据不足」
    missing = body["returns"]["missing_evidence"]
    assert any("实际退货率" in item for item in missing)

    # 顶层也要有一句，因为只看 verdict 的人不会翻进 returns 里
    assert any("不是这一台的实测值" in w for w in body["warnings"])


def test_estimate_row_is_flagged_as_assumption(client):
    """咖啡机那行是 `estimate`：整行假设，warning 里必须说清。

    与净化器分开测，因为两者是**不同的欠账**：一个是借了别人的数，
    一个是还没有数。动作不同（前者去导报表，后者去要读数）。
    """
    payload = {**PURIFIER, "sku_key": COFFEE_SKU}
    body = _post(client, payload).json()

    assert body["returns"]["evidence"]["kind"] == "estimate"
    assert body["returns"]["rate"] == pytest.approx(0.15)
    assert body["returns"]["stress"]["stress_returns"] == pytest.approx(0.20)
    assert any("不是这一台的实测值" in w for w in body["warnings"])


# ---------- 错误路径：不许用默认值替它猜 ----------


def test_unknown_sku_key_is_503_not_a_default_rate(client):
    """没登记这一台 → 503 `return_rate_missing`，不是静默按 8% 或 0 算。

    按默认值算出来的毛利看起来完全正常，但那个数没有出处——
    这正是 `product_returns` 存在的理由。
    """
    response = _post(client, {**PURIFIER, "sku_key": "no_such_sku"})

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "return_rate_missing"
    assert "no_such_sku" in body["message"]


def test_marketplace_must_match_the_registered_row(client):
    """同一个 `sku_key` 在别的市场没登记，就不是同一台的两档。"""
    response = _post(client, {**PURIFIER, "marketplace": "GB"})

    assert response.status_code == 503
    assert response.json()["code"] == "return_rate_missing"


def test_sku_key_and_explicit_rate_conflict(client):
    """两个来源同时给 → 422，且错误信息点出两个字段。

    不是「显式优先」那种容忍：基线来自调用方、压力档来自库，两档不同源，
    比出来的差值就是假的——而压测的全部意义就在那个差值。
    """
    payload = {
        **PURIFIER,
        "cost": {**PURIFIER["cost"], "return_rate": 0.08},
    }
    response = _post(client, payload)

    assert response.status_code == 422
    message = response.json()["message"]
    assert "sku_key" in message
    assert "return_rate" in message


# ---------- 老契约不变：显式档仍然可用，只是没有压力档 ----------


def test_explicit_rate_still_works_without_stress(client):
    """不给 `sku_key` 时行为与接表之前完全一致：没有 `stress` 块。

    这一条防的是「为了接表把老调用方一起改了」——那会逼所有调用方
    先去登记 SKU 才能出结论。
    """
    payload = {
        k: v for k, v in PURIFIER.items() if k != "sku_key"
    }
    payload["cost"] = {**PURIFIER["cost"], "return_rate": 0.08}
    body = _post(client, payload).json()

    assert body["returns"] == {
        "source": "explicit",
        "rate": 0.08,
        "sku_key": None,
        "evidence": None,
        "missing_evidence": [],
        "stress": None,
    }
    # 没有表可查，就没有「证据强弱」可警告——但也不许凭空造一个压力档
    assert body["warnings"] == []


def test_returns_block_has_the_same_shape_in_both_modes(client):
    """两种模式字段同名同义，客户端不必分支。

    这是接口层的可维护性断言：如果哪天给表模式加了一个只在那边存在的键，
    客户端要么写两套读取逻辑，要么静默取到 None。
    """
    from_table = _post(client, PURIFIER).json()["returns"]
    explicit_payload = {k: v for k, v in PURIFIER.items() if k != "sku_key"}
    explicit_payload["cost"] = {**PURIFIER["cost"], "return_rate": 0.08}
    from_caller = _post(client, explicit_payload).json()["returns"]

    assert set(from_table) == set(from_caller)
    assert set(from_table["stress"]) >= {"baseline_margin", "stress_margin", "worth_negotiating"}


def test_same_request_same_response_with_sku_key(client):
    """查表不能带来随机性：同输入同输出。"""
    first = _post(client, PURIFIER).json()
    second = _post(client, PURIFIER).json()
    assert first == second


# ---------- 异步任务走的是同一份实现 ----------


def test_decision_job_carries_the_returns_block(db_file, client):
    """任务侧不复制一份逻辑：`handle_decision` 调的就是 `service.decide()`。

    同步与异步给出不同的 `returns` 块，等于告诉调用方「你换个入口结论就变了」。
    """
    response = client.post("/v1/jobs", json={"kind": "decision", **PURIFIER})
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    @contextmanager
    def session():
        conn = connect(db_file)
        try:
            yield conn
        finally:
            conn.close()

    runner = JobRunner(
        session=session,
        client=build_llm_client(api.settings),
        settings=api.settings,
        worker_id="w-test",
        lease_seconds=60,
    )
    asyncio.run(runner.drain())

    result = client.get(f"/v1/jobs/{job_id}").json()
    assert result["status"] == "succeeded"
    returns = result["result"]["returns"]
    assert returns["source"] == "product_returns"
    assert returns["stress"]["stress_margin"] == pytest.approx(0.0918, abs=5e-4)
