"""共享 fixture。

统一用 `:memory:`：每个用例拿到一个干净库，互不影响，也不碰 data/app.sqlite3。
迁移在 fixture 里跑完，用例只面对「已升级到最新的库」这一种状态。
"""

from __future__ import annotations

import pytest

from xborder_insight import api
from xborder_insight.db import migrate
from xborder_insight.db.connection import connect


@pytest.fixture(scope="session", autouse=True)
def app_db_file(tmp_path_factory):
    """把 HTTP 层的库指向临时文件，测试永不依赖工作区的 data/app.sqlite3。

    为什么必须这么做：`api.DB_FILE` 默认指向 `<repo>/data/app.sqlite3`，而它
    在 .gitignore 里。本机跑过 `db.py up` 后一切正常，**干净克隆里没有这个文件**，
    于是 `/ready`、`/v1/analyze`（两处）、`/metrics` 全部 503——2026-11-23 冷启动
    演练实测 4 failed。这类缺陷只在别人机器上现形，是最典型的交付事故。

    用 session 级而不是函数级：迁移一次就够，别让 695 条测试各建一次库。
    需要「库不存在」场景的用例（test_decision_api 的两条 503 用例）自己再
    monkeypatch 一次即可，monkeypatch 退出时恢复到这里设定的值。
    """
    path = tmp_path_factory.mktemp("appdb") / "app.sqlite3"
    connection = connect(path)
    migrate.upgrade(connection)
    connection.close()

    original = api.DB_FILE
    api.DB_FILE = path
    yield path
    api.DB_FILE = original


@pytest.fixture
def conn():
    """已升级到最新版本的库。"""
    connection = connect(":memory:")
    migrate.upgrade(connection)
    yield connection
    connection.close()


@pytest.fixture
def empty_conn():
    """空白库，用于测试迁移过程本身。"""
    connection = connect(":memory:")
    yield connection
    connection.close()
