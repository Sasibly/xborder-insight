"""0006：任务结果归档位。

`jobs.result` 是这张表里最大的字段——一次评论分析的完整报告（主题、证据、统计），
而它只在任务完成后很短一段时间内被读。不处理的话 `jobs` 会随时间单调膨胀，
被拖慢的第一个查询恰恰是「列出我的任务」这种最常被调用的简单查询。

**为什么是「清字段」而不是「删行」**

    幂等靠 `UNIQUE(tenant_id, idempotency_key)`。删掉行，归档过的幂等键就跟着消失，
    同一个键重放会变成一条新任务——重新花一次钱，而调用方看不出任何区别。
    这是归档方案最容易踩的坑。所以归档必须保留行，只能清掉重字段，
    再用一列 `archived_at` 把「结果已归档」和「本来就没有结果」区分开——
    这两件事在接口上长得一模一样，混在一起就只能靠猜。

**为什么建部分索引，而不是普通索引**

    `WHERE ... AND result IS NOT NULL` 让索引只覆盖「还有结果可归档」的行。
    行一旦归档就退出索引，索引不会跟着表一起膨胀。
    代价是这条索引对别的查询没用——它本来就只为清理任务服务。
"""
from __future__ import annotations

import sqlite3

COLUMN = "archived_at"

ARCHIVE_COLUMN = "ALTER TABLE jobs ADD COLUMN archived_at TEXT"

# 只给「终态 + 有结果 + 未归档」的行建索引：任务一旦归档即退出索引。
ARCHIVE_INDEX = (
    "CREATE INDEX idx_jobs_archive_candidates ON jobs (finished_at) "
    "WHERE status IN ('succeeded', 'failed') AND result IS NOT NULL AND archived_at IS NULL"
)


def up(conn: sqlite3.Connection) -> None:
    conn.execute(ARCHIVE_COLUMN)
    conn.execute(ARCHIVE_INDEX)


def down(conn: sqlite3.Connection) -> None:
    # 先删索引再删列：部分索引的 WHERE 里引用了 archived_at，
    # 反过来做会因「索引依赖的列不存在」而失败。
    conn.execute("DROP INDEX IF EXISTS idx_jobs_archive_candidates")
    conn.execute("ALTER TABLE jobs DROP COLUMN archived_at")
