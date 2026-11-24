"""决策台页面的守门测试。

页面是静态 HTML + 原生 JS，交互测不了；能测的是两类**退化后不会报错**的毛病：
字段名与接口契约对不上（点了「算一下」回 422，页面看起来却正常），
以及悄悄引入外部资源（离线、内网、审计场景全挂）。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from xborder_insight import api
from xborder_insight.api import app


def client() -> TestClient:
    return TestClient(app)


def test_console_page_renders_and_posts_to_decision():
    with client() as c:
        response = c.get("/console")
    assert response.status_code == 200
    body = response.text
    assert 'id="f"' in body, "表单容器被改名，JS 绑定会静默失效"
    assert "fetch('/v1/decision'" in body, "页面不再调用决策接口"


def test_console_field_names_all_exist_in_openapi():
    """页面上每个 name= 都必须是契约里的字段。

    拼错一个（比如 referral_fee 写成 referral_rate 之外的名字）不会红，
    只会让每次点「算一下」都回 422 —— 调用方看到的是「系统坏了」。
    """
    schemas = api.app.openapi()["components"]["schemas"]
    cost_props = set(schemas["CostProfileInput"]["properties"])
    decision_props = set(schemas["DecisionRequest"]["properties"])

    with client() as c:
        body = c.get("/console").text

    names = set(
        re.findall(r'<(?:input|select|textarea)[^>]*name="([a-z_]+)"', body)
    )
    assert names, "页面里一个表单字段都没有，字段全丢了"
    unknown = names - cost_props - decision_props
    assert not unknown, f"页面字段不在契约里：{sorted(unknown)}"

    for required in schemas["CostProfileInput"]["required"]:
        assert f'name="{required}"' in body, f"必填字段 {required} 没在页面上"


def test_console_keeps_the_upper_bound_warning():
    """上界提示一旦被删，缺项时那串漂亮的毛利率就会被当成结论读。"""
    with client() as c:
        body = c.get("/console").text
    assert "这是上界，不是结论" in body
    assert "missing_inputs" in body


def test_console_has_no_external_resources():
    """不引 CDN、不引字体、不引框架：断网环境下面板必须照常可用。"""
    with client() as c:
        body = c.get("/console").text
    assert "<script src=" not in body
    assert "<link " not in body
    assert "http://" not in body
    assert "https://" not in body


def test_index_links_to_console():
    """首页没有入口的话，这一页等于不存在。"""
    with client() as c:
        home = c.get("/").text
    assert 'href="/console"' in home
    assert 'href="/"' in home
