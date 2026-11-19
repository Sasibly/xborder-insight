"""迁移执行器。

三条性质，任何一条不成立都说明迁移写坏了：
1. **原子性** —— 单个迁移的 up() 要么全部生效，要么全部回滚
2. **可逆性** —— down() 执行后，schema 必须与升级前完全一致（不是"差不多"）
3. **幂等性** —— up → down → up 之后的 schema 必须与第一次 up 完全一致

第 3 条是最容易被忽略、也最容易暴露问题的一条：down() 忘了删索引、
忘了删触发器，都能靠它抓出来。`scripts/check.py` 的第二层就是跑这个往返。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .connection import schema_fingerprint, table_names, transaction
from .migrations import LATEST, Migration, ordered

BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class MigrationStatus:
    version: int
    label: str
    applied_at: str | None

    @property
    def applied(self) -> bool:
        return self.applied_at is not None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_bookkeeping(conn: sqlite3.Connection) -> None:
    conn.execute(BOOKKEEPING)


def applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    ensure_bookkeeping(conn)
    rows = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    return {int(row["version"]): str(row["applied_at"]) for row in rows}


def current_version(conn: sqlite3.Connection) -> int:
    versions = applied_versions(conn)
    return max(versions) if versions else 0


def status(conn: sqlite3.Connection) -> list[MigrationStatus]:
    applied = applied_versions(conn)
    return [
        MigrationStatus(m.version, m.label, applied.get(m.version)) for m in ordered()
    ]


def _apply(conn: sqlite3.Connection, migration: Migration, *, direction: str) -> None:
    with transaction(conn):
        if direction == "up":
            migration.up(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, _now()),
            )
        else:
            migration.down(conn)
            conn.execute(
                "DELETE FROM schema_migrations WHERE version = ?", (migration.version,)
            )


def upgrade(conn: sqlite3.Connection, target: int = LATEST) -> list[int]:
    """升级到 target（含）。返回实际执行的版本号。"""
    ensure_bookkeeping(conn)
    current = current_version(conn)
    if target < current:
        raise ValueError(f"当前版本 {current} 高于目标 {target}，请用 downgrade()")

    done: list[int] = []
    for migration in ordered():
        if migration.version <= current or migration.version > target:
            continue
        _apply(conn, migration, direction="up")
        done.append(migration.version)
    return done


def downgrade(conn: sqlite3.Connection, target: int = 0) -> list[int]:
    """回滚到 target（不含 target，target=0 表示清空）。返回实际回滚的版本号。"""
    ensure_bookkeeping(conn)
    current = current_version(conn)
    if target > current:
        raise ValueError(f"当前版本 {current} 低于目标 {target}，请用 upgrade()")

    done: list[int] = []
    for migration in reversed(ordered()):
        if migration.version <= target or migration.version > current:
            continue
        _apply(conn, migration, direction="down")
        done.append(migration.version)
    return done


def pending(conn: sqlite3.Connection) -> list[str]:
    applied = applied_versions(conn)
    return [m.label for m in ordered() if m.version not in applied]


def roundtrip(conn: sqlite3.Connection) -> dict[str, object]:
    """up(全量) → down(0) → up(全量)，比对三次的 schema 指纹。

    这是迁移的可信度证明，不是形式主义：它同时验证了
    「up 能建起来」「down 能删干净」「重复 up 结果一致」三件事。

    注意执行顺序：发现残留就立即返回，不再继续重建。否则第二次 up 会撞上
    "table already exists"，把一个清晰的「down() 删漏了」掩盖成一个语法错误，
    排查方向直接跑偏。
    """
    upgrade(conn)
    first = schema_fingerprint(conn)
    tables_after_up = table_names(conn)

    downgrade(conn, 0)
    leftover = [t for t in table_names(conn) if t != "schema_migrations"]
    if leftover:
        return {
            "ok": False,
            "rebuilt": False,
            "version": current_version(conn),
            "tables": tables_after_up,
            "leftover_after_down": leftover,
            "schema_drift": [],
            "reason": "down(0) 未清理干净，跳过重建",
        }

    upgrade(conn)
    second = schema_fingerprint(conn)
    tables_after_second = table_names(conn)

    drift = sorted(set(first) ^ set(second)) or [
        key for key in first if key in second and first[key] != second[key]
    ]
    return {
        "ok": not drift and tables_after_up == tables_after_second,
        "rebuilt": True,
        "version": current_version(conn),
        "tables": tables_after_up,
        "leftover_after_down": leftover,
        "schema_drift": drift,
    }
