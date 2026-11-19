"""连接管理：把「谁负责提交事务」这件事定死。

关键决策：`isolation_level=None`（关闭 sqlite3 的隐式事务），改为显式
`transaction()` 上下文。原因：

- Python 的 sqlite3 默认行为是「遇到 DML 自动 BEGIN，遇到 commit() 才提交」，
  迁移脚本里一次 up() 有 10 条 DDL 时，很容易出现「一半成功一半失败」的残局。
- 显式事务让「迁移要么全成功、要么全回滚」成为可保证的性质，而不是碰运气。

WAL 与 foreign_keys 都必须显式打开：SQLite 的默认值是关的，而这两个恰恰
是「能发现关联数据损坏」的前提。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..config import settings

MEMORY = ":memory:"


def resolve_path(path: str | Path | None = None) -> Path | str:
    """统一入口：None 取配置，':memory:' 原样返回，其余转 Path 并确保目录存在。"""
    if path is None:
        path = settings.db_path
    if str(path) == MEMORY:
        return MEMORY
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = resolve_path(path)
    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if target != MEMORY:
        # 内存库不支持 WAL，也不需要——它不会有多进程竞争
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务。嵌套调用会直接复用外层事务，避免 SQLite 的 "cannot start a transaction" 。"""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def schema_fingerprint(conn: sqlite3.Connection) -> dict[str, str]:
    """schema 指纹：表名 → 规范化后的建表/索引 DDL。

    用来证明「迁移 up → down → up 之后结构完全回到原样」。
    只比对结构，不比对数据——数据本来就是空的。
    """
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return {
        f"{row['type']}:{row['name']}": " ".join((row["sql"] or "").split())
        for row in rows
    }
