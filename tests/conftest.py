"""共享 fixture。

统一用 `:memory:`：每个用例拿到一个干净库，互不影响，也不碰 data/app.sqlite3。
迁移在 fixture 里跑完，用例只面对「已升级到最新的库」这一种状态。
"""

from __future__ import annotations

import pytest

from xborder_insight.db import migrate
from xborder_insight.db.connection import connect


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
