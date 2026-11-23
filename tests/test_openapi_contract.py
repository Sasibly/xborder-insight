"""接口文档的守门测试：`/openapi.json` 必须自己说清楚契约。

为什么值得单独测一层文档
------------------------
`/docs` 是外部调用方唯一不用读仓库就能看到的接口说明。字段没有 `description`
时，Swagger 里只有一排字段名——`sku_key` 这种「给了它，退货率来源就整体切换」
的字段，光看名字没人猜得到它会跑压力档、会和 `cost.return_rate` 互斥。

更麻烦的是这类退化和代码退化长得不一样：删掉一个 `description=` 不会让任何业务
测试变红，只会让文档悄悄变空。而「文档变空」的代价是调用方按默认理解去填——
正是 `docs/04` §30 / §31 花两节在防的那种错。所以这里把「字段必须有说明」
「错误码必须有形状」写成断言，改坏了就红。

不测措辞好坏，只测三件可判定的事：
1. 每个字段都有非空说明（不许退化成裸字段名）
2. 六项可选成本的「可选 + 默认 null」形状不许回退（H17 的结构化落地）
3. 文档里的错误体字段与真实响应逐字一致（文档不撒谎）
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xborder_insight import api
from xborder_insight.schemas import CostProfileInput, DecisionRequest

#: 六项「不填 ≠ 0」的成本字段（`docs/04` §31）。它们必须可选且默认 null，
#: 一旦谁把 `default=0.0` 加回来，毛利率又会静默变成乐观值。
SIX_OPTIONAL_COSTS = (
    "inbound_freight",
    "duty_rate",
    "return_rate",
    "returns_handling_fee",
    "returns_goods_loss_ratio",
    "returns_inbound_loss_ratio",
)


@pytest.fixture(scope="module")
def spec() -> dict:
    return api.app.openapi()


def _properties(spec: dict, model: str) -> dict:
    return spec["components"]["schemas"][model]["properties"]


@pytest.mark.parametrize("model", ["DecisionRequest", "CostProfileInput", "ErrorResponse"])
def test_every_documented_field_carries_a_description(spec, model):
    """字段没有说明＝文档没写。这条会把「删掉 description」变成一次红灯。"""
    bare = [
        name
        for name, prop in _properties(spec, model).items()
        if not str(prop.get("description") or "").strip()
    ]
    assert bare == [], f"{model} 里这些字段在 /docs 里只有名字、没有说明：{bare}"


def test_six_cost_fields_stay_optional_with_no_numeric_default(spec):
    """六项成本必须仍是「可选 + 无数字默认值」。

    `required` 里出现它们，等于把「报价阶段先粗筛」这条路堵死；
    `default: 0` 出现，等于回到「漏填按 0 算还报 GO」的老路（H17）。
    """
    schema = spec["components"]["schemas"]["CostProfileInput"]
    required = set(schema.get("required", []))
    props = schema["properties"]

    assert required == {"quoted_price", "selling_price", "referral_rate"}
    for name in SIX_OPTIONAL_COSTS:
        assert name not in required, f"{name} 不能是必填（会让报价阶段无法粗筛）"
        prop = props[name]
        # nullable 有两种表达：anyOf 里带 null，或 type 里带 "null"
        assert "anyOf" in prop or "null" in str(prop.get("type")), (
            f"{name} 必须允许 null（表示「未指定」），当前 schema 是 {prop}"
        )
        assert "default" not in prop or prop["default"] is None, (
            f"{name} 不许有数字默认值——漏填被当成 0 会让毛利率静默变成上界"
            "（docs/04 §31）"
        )


def test_sku_key_documents_that_it_switches_the_returns_source(spec):
    """`sku_key` 的说明必须点出「走表 / 跑压力档 / 与 return_rate 互斥」。

    只写「内部 SKU 键」等于没说：调用方会以为它只是个关联字段，
    于是继续填 `cost.return_rate`，然后收到一个莫名其妙的 422。
    """
    desc = _properties(spec, "DecisionRequest")["sku_key"]["description"]
    assert "product_returns" in desc
    assert "互斥" in desc
    assert "压力档" in desc


def test_decision_endpoint_documents_422_and_503_with_a_body_shape(spec):
    """两个「设计内的错误」必须在 OpenAPI 里有形状，而不是只写一句 ad-hoc 文案。"""
    responses = spec["paths"]["/v1/decision"]["post"]["responses"]

    for code in ("422", "503"):
        assert code in responses, f"/v1/decision 没声明 {code}，调用方只能靠猜"
        content = responses[code]["content"]["application/json"]["schema"]
        assert content.get("$ref", "").endswith("/ErrorResponse"), (
            f"{code} 的错误体没有引用 ErrorResponse，文档里看不出返回什么字段"
        )

    assert "returns" in responses["200"]["description"]


def test_documented_error_body_matches_the_real_one():
    """文档里的错误体字段必须与真实响应逐字一致。

    文档写 `{code, message, request_id}`、实际返回 `{error, detail}` 是常见事故：
    两边各自「自洽」，只有对着看才发现。这里直接发两次真请求去比对。
    """
    documented = set(api.app.openapi()["components"]["schemas"]["ErrorResponse"]["properties"])

    with TestClient(api.app) as client:
        conflict = client.post(
            "/v1/decision",
            json={
                "asin": "B0TEST0001",
                "category": "wireless_charger",
                "opportunity_score": 0.66,
                "sku_key": "purifier_desk_1752",
                "cost": {
                    "quoted_price": 8.0,
                    "selling_price": 29.99,
                    "referral_rate": 0.15,
                    "fba_fee": 3.5,
                    "return_rate": 0.08,
                },
            },
        )
        missing = client.post(
            "/v1/decision",
            json={
                "asin": "B0TEST0001",
                "category": "wireless_charger",
                "opportunity_score": 0.66,
                "sku_key": "no_such_sku",
                "cost": {
                    "quoted_price": 8.0,
                    "selling_price": 29.99,
                    "referral_rate": 0.15,
                    "fba_fee": 3.5,
                },
            },
        )

    assert conflict.status_code == 422
    assert missing.status_code == 503
    for response in (conflict, missing):
        body = response.json()
        assert set(body) == documented, f"真实错误体 {set(body)} 与文档 {documented} 不一致"
        assert body["request_id"]


def test_openapi_and_docs_are_publicly_readable():
    """文档本身不能要鉴权：调用方拿到 base_url 后第一件事就是打开 /docs。"""
    with TestClient(api.app) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200


def test_schema_docstring_is_not_empty():
    """模型说明会被 FastAPI 当 description 用；空说明等于契约没有开头。"""
    for model in (DecisionRequest, CostProfileInput):
        assert (model.__doc__ or "").strip(), f"{model.__name__} 缺类级说明"
