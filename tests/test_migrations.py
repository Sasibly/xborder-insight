"""迁移框架回归测试。

锁的是三条性质：原子性、可逆性、幂等性。
其中最容易被忽略的是「可逆性」——down() 少删一个索引，程序照样跑，
只有在真要回滚的那天才发现回不去。所以这里用 schema 指纹做严格比对，
而不是「表还在就算过」。
"""

from __future__ import annotations

import sqlite3

import pytest

from xborder_insight.db import migrate
from xborder_insight.db.connection import schema_fingerprint, table_names, transaction
from xborder_insight.db.migrations import LATEST, ordered

BUSINESS_TABLES = {"reviews", "runs", "cost_params", "compliance_rules"}


# ---------- 注册表本身的完整性 ----------


def test_versions_are_contiguous_from_one():
    """版本号必须从 1 连续递增：跳号说明有人删过已发布的迁移。"""
    assert [m.version for m in ordered()] == list(range(1, LATEST + 1))


def test_every_migration_has_callable_up_and_down():
    for migration in ordered():
        assert callable(migration.up), f"{migration.label} 缺少 up()"
        assert callable(migration.down), f"{migration.label} 缺少 down()"


# ---------- 升级 ----------


def test_upgrade_creates_all_tables(empty_conn):
    applied = migrate.upgrade(empty_conn)
    assert applied == list(range(1, LATEST + 1))
    assert BUSINESS_TABLES <= set(table_names(empty_conn))
    assert migrate.current_version(empty_conn) == LATEST


def test_upgrade_is_idempotent(empty_conn):
    migrate.upgrade(empty_conn)
    assert migrate.upgrade(empty_conn) == []  # 第二次不应有任何动作


def test_partial_upgrade(empty_conn):
    migrate.upgrade(empty_conn, target=1)
    tables = set(table_names(empty_conn))
    assert {"reviews", "runs"} <= tables
    assert "cost_params" not in tables
    assert migrate.current_version(empty_conn) == 1


def test_bookkeeping_records_timestamps(empty_conn):
    migrate.upgrade(empty_conn)
    rows = migrate.status(empty_conn)
    assert len(rows) == LATEST
    assert all(row.applied for row in rows)
    assert all(row.applied_at for row in rows)


# ---------- 回滚 ----------


def test_downgrade_to_zero_removes_business_tables(empty_conn):
    migrate.upgrade(empty_conn)
    rolled = migrate.downgrade(empty_conn, 0)
    assert rolled == sorted(range(1, LATEST + 1), reverse=True)
    # 只允许留下框架自己的版本表
    assert set(table_names(empty_conn)) == {"schema_migrations"}


def test_downgrade_one_step_equals_never_having_upgraded(empty_conn):
    """回退一步后的库，必须与「从未升级过那一步」的库完全一致。

    这个定义比「某张表还在不在」稳得多，原因很实际：
    m0001/m0002 是建表，m0003 只插入参数行不建表。
    若断言写成「回退后 cost_params 必须消失」，一加 m0003 就会红——
    而红的不是 bug，是断言把「回退 = 删表」这个误解硬编码了。

    改成与参照库比对后，无论未来迁移是建表、加列还是插数据，这条都成立。
    """
    from xborder_insight.db.connection import connect

    migrate.upgrade(empty_conn)
    migrate.downgrade(empty_conn, LATEST - 1)

    reference = connect(":memory:")
    try:
        migrate.upgrade(reference, target=LATEST - 1)
        assert migrate.current_version(empty_conn) == LATEST - 1
        assert schema_fingerprint(empty_conn) == schema_fingerprint(reference)
        assert _param_keys(empty_conn) == _param_keys(reference)
        assert _param_keys(empty_conn), "回退一步不应把更早迁移的种子数据一起带走"
    finally:
        reference.close()


def _param_keys(conn) -> set[str]:
    """参数键集合。表不存在时返回空集，避免测试因迁移顺序变化而崩。"""
    try:
        return {row[0] for row in conn.execute("SELECT param_key FROM cost_params")}
    except sqlite3.OperationalError:
        return set()


def test_roundtrip_schema_is_identical(empty_conn):
    """up → down → up 之后 schema 指纹必须完全一致，否则说明 down() 没删干净。"""
    result = migrate.roundtrip(empty_conn)
    assert result["ok"] is True, result
    assert result["leftover_after_down"] == []
    assert result["schema_drift"] == []


def test_roundtrip_detects_leftover_table(empty_conn):
    """down() 删漏时，必须报「残留」而不是继续重建。

    继续重建会撞上 table already exists，把清晰的问题掩盖成语法错误——
    这个分支是专门为「排查方向不跑偏」而存在的。
    """
    empty_conn.execute("CREATE TABLE stray (id INTEGER)")
    result = migrate.roundtrip(empty_conn)

    assert result["ok"] is False
    assert result["rebuilt"] is False
    assert "stray" in result["leftover_after_down"]


def test_roundtrip_leaves_working_database(empty_conn):
    migrate.roundtrip(empty_conn)
    assert migrate.current_version(empty_conn) == LATEST
    empty_conn.execute(
        "INSERT INTO reviews (review_id, dataset, asin, rating, body, content_hash, ingested_at) "
        "VALUES ('r1', 'd', 'A1', 3, 'body', 'h1', '2026-11-17T00:00:00')"
    )
    assert empty_conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1


# ---------- 方向错误要报错，不能静默 ----------


def test_upgrade_below_current_rejected(empty_conn):
    migrate.upgrade(empty_conn)
    with pytest.raises(ValueError, match="downgrade"):
        migrate.upgrade(empty_conn, target=LATEST - 1)


def test_downgrade_above_current_rejected(empty_conn):
    with pytest.raises(ValueError, match="upgrade"):
        migrate.downgrade(empty_conn, target=1)


# ---------- 原子性 ----------


def test_failed_migration_rolls_back(empty_conn):
    """up() 中途报错时，已执行的 DDL 必须一起回滚，不留半拉子结构。"""
    import dataclasses

    from xborder_insight.db import migrations as registry

    original = registry.get(1)

    def exploding_up(connection):
        original.up(connection)
        raise RuntimeError("迁移中途失败")

    # Migration 是 frozen dataclass，用 replace 造一个「up 会炸」的副本，
    # 而不是 monkeypatch 改注册表——后者会污染其他用例
    broken = dataclasses.replace(original, up=exploding_up)
    with pytest.raises(RuntimeError):
        migrate._apply(empty_conn, broken, direction="up")

    assert "reviews" not in table_names(empty_conn)
    assert migrate.current_version(empty_conn) == 0


# ---------- schema 指纹本身的可靠性 ----------

def test_fingerprint_changes_when_schema_changes(empty_conn):
    migrate.upgrade(empty_conn, target=1)
    before = schema_fingerprint(empty_conn)
    with transaction(empty_conn):
        empty_conn.execute("ALTER TABLE reviews ADD COLUMN extra TEXT")
    assert schema_fingerprint(empty_conn) != before
