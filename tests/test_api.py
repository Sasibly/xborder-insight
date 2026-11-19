"""HTTP 层回归测试：锁定对外契约。

重点不是「能不能跑通」，而是「出错时吐什么」：
面试官和调用方都会先试错误路径。这里断言 code / request_id 的结构，
以及堆栈绝不外泄——这类要求一旦退化，通常没人会发现。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from xborder_insight.api import app


def client() -> TestClient:
    return TestClient(app)


def test_health_and_ready():
    with client() as c:
        assert c.get("/health").json()["status"] == "ok"
        assert c.get("/ready").json()["status"] == "ready"


def test_datasets_listed():
    with client() as c:
        datasets = c.get("/v1/datasets").json()["datasets"]
    assert "wireless_charger" in datasets
    assert "pet_fountain" in datasets


def test_analyze_by_dataset_returns_report():
    with client() as c:
        response = c.post("/v1/analyze", json={"dataset": "wireless_charger", "top_k": 3})
    assert response.status_code == 200

    body = response.json()
    assert body["dataset"] == "wireless_charger"
    assert body["parse_success_rate"] == 1.0
    assert 0 < len(body["themes"]) <= 3
    # 排序契约：机会分必须单调递减，否则前端排序是假的
    scores = [t["opportunity_score"] for t in body["themes"]]
    assert scores == sorted(scores, reverse=True)


def test_unknown_dataset_returns_structured_error():
    with client() as c:
        response = c.post("/v1/analyze", json={"dataset": "no_such_dataset"})
    assert response.status_code == 422

    body = response.json()
    assert body["code"] == "input_invalid"
    assert body["request_id"]  # 必须能拿这个 id 去查日志
    assert "Traceback" not in response.text


def test_conflicting_sources_rejected():
    with client() as c:
        response = c.post(
            "/v1/analyze",
            json={"dataset": "wireless_charger", "reviews": []},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "input_invalid"


def test_empty_source_rejected():
    with client() as c:
        response = c.post("/v1/analyze", json={})
    assert response.status_code == 422
    assert "至少提供一个" in response.json()["message"]


def test_empty_reviews_list_rejected():
    """显式传空数组不等于「没传」：不能静默回落到别处。"""
    with client() as c:
        response = c.post("/v1/analyze", json={"reviews": []})
    assert response.status_code == 422
    assert response.json()["code"] == "input_invalid"


def test_request_id_is_echoed():
    with client() as c:
        response = c.get("/health", headers={"X-Request-Id": "trace-me-123"})
    assert response.headers["X-Request-Id"] == "trace-me-123"


def test_inline_reviews_path_works():
    payload = {
        "reviews": [
            {
                "review_id": "inline-1",
                "asin": "B0TEST0001",
                "rating": 1,
                "title": "Leaks",
                "body": "Water everywhere after two days. It leaks from the base.",
            }
        ],
        "top_k": 2,
    }
    with client() as c:
        response = c.post("/v1/analyze", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["dataset"] == "inline"
    assert body["review_count"] == 1
