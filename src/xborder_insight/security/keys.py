"""API Key：生成、哈希、鉴权、吊销。

一条原则贯穿全文件：**明文只在创建时出现一次**。
库里只有 sha256，任何「帮我看看这个租户的 key 是多少」的诉求都必须
换成一个新 key，而不是把旧 key 找回来——因为找回来的能力本身就是漏洞。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta

from ..db.connection import transaction
from ..errors import InputInvalid, Unauthorized

# 明文前缀：一眼能看出这是本系统的密钥，也便于用日志脱敏规则一把捞干净
KEY_PREFIX = "xb_"
# 展示给人工辨认的明文长度。16 字符 ≈ 96 bit，仍不足以反推 256 bit 密钥
HINT_LEN = 12
# 明显短于生成长度的输入直接拒，省掉一次哈希与一次查询
MIN_KEY_LEN = 24

# last_used_at 的写入节流：每次请求都写会把这个字段变成写入热点，
# 而它的用途只是「哪把钥匙还在用」，分钟级精度完全够。
_TOUCH_INTERVAL_SECONDS = 60


def generate() -> str:
    """生成新密钥。`secrets` 而非 `random`：后者是可预测的伪随机。"""
    import secrets

    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    """明文 → 存储用的哈希。

    不加盐、不用慢哈希，理由见迁移注释：API Key 是高熵随机串，
    慢哈希防的是口令字典攻击，代价是让等值查询退化成全表扫描。
    """
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def _hint(raw: str) -> str:
    return raw[:HINT_LEN]


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def create(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    label: str = "",
    daily_token_quota: int | None = None,
    rpm_limit: int = 60,
) -> tuple[str, sqlite3.Row]:
    """创建密钥，返回 (明文, 行)。

    明文不落库，所以调用方**必须**在这一刻把它交给使用者；
    出了这个函数就再也拿不到了。
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise InputInvalid("tenant_id 不能为空")
    if daily_token_quota is not None and daily_token_quota <= 0:
        raise InputInvalid("daily_token_quota 必须为正整数，或不设（None）")
    if not 1 <= rpm_limit <= 100_000:
        raise InputInvalid(f"rpm_limit 超出范围 1..100000，当前值: {rpm_limit}")

    raw = generate()
    with transaction(conn):
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, tenant_id, label, "
            "daily_token_quota, rpm_limit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (hash_key(raw), _hint(raw), tenant_id, label, daily_token_quota, rpm_limit, _now()),
        )
    return raw, by_hash(conn, hash_key(raw))


def by_hash(conn: sqlite3.Connection, key_hash: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)).fetchone()
    if row is None:
        raise Unauthorized("API Key 无效")
    return row


def authenticate(conn: sqlite3.Connection, raw: str | None) -> sqlite3.Row:
    """校验密钥并返回其记录。

    无效、不存在、已吊销**返回同一句话**：区分它们等于提供一个
    「这个 key 曾经存在过吗」的探测接口。
    """
    key = (raw or "").strip()
    if not key:
        # 只空白字符等同于「没带」：把它当成「key 无效」会让一个多打了空格的
        # 客户端收到误导性的错误。
        raise Unauthorized("缺少 API Key", detail="请求头需带 Authorization: Bearer <key> 或 X-API-Key")
    if len(key) < MIN_KEY_LEN or not key.startswith(KEY_PREFIX):
        # 格式明显不对就不必查库。注意这里不返回「格式错误」——
        # 否则攻击者能靠报错区分「猜错格式」与「格式对但不存在」。
        raise Unauthorized("API Key 无效")

    row = conn.execute("SELECT * FROM api_keys WHERE key_hash = ?", (hash_key(key),)).fetchone()
    if row is None or row["revoked_at"] is not None:
        raise Unauthorized("API Key 无效")

    _touch(conn, str(row["key_hash"]), str(row["last_used_at"] or ""))
    return row


def _touch(conn: sqlite3.Connection, key_hash: str, last_used: str) -> None:
    """更新 last_used_at。写失败不能导致鉴权失败——它是观测字段，不是安全字段。"""
    if last_used:
        try:
            previous = datetime.fromisoformat(last_used)
        except ValueError:
            previous = None
        if previous is not None and (datetime.now() - previous).total_seconds() < _TOUCH_INTERVAL_SECONDS:
            return
    try:
        with transaction(conn):
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?", (_now(), key_hash))
    except sqlite3.Error:
        pass


def revoke(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row:
    """吊销密钥。`identifier` 可以是完整哈希，也可以是创建时展示的明文前缀。

    前缀必须唯一匹配——「大概就是这把」这种吊销方式会造成两种事故：
    误吊销别人的 key，或以为自己吊销了其实没有。
    """
    ident = (identifier or "").strip()
    if not ident:
        raise InputInvalid("需要提供 key 哈希或前缀")

    if len(ident) == 64 and all(c in "0123456789abcdef" for c in ident):
        row = conn.execute("SELECT * FROM api_keys WHERE key_hash = ?", (ident,)).fetchone()
        if row is None:
            raise InputInvalid(f"没有这个 key: {ident[:12]}…")
    else:
        # 不按「是否已吊销」过滤：运维脚本重跑时吊销同一把 key 应该幂等，
        # 而不是报「找不到」。真正的风险是前缀歧义，那个下面单独拦。
        rows = conn.execute("SELECT * FROM api_keys WHERE key_prefix = ?", (ident,)).fetchall()
        if not rows:
            raise InputInvalid(f"没有匹配前缀的 key: {ident}")
        if len(rows) > 1:
            raise InputInvalid(
                f"前缀 {ident} 匹配到 {len(rows)} 把 key，无法确定吊销哪一把",
                detail="请提供更长的前缀或完整哈希",
            )
        row = rows[0]

    if row["revoked_at"] is None:
        with transaction(conn):
            conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE key_hash = ? AND revoked_at IS NULL",
                (_now(), row["key_hash"]),
            )
    return by_hash(conn, str(row["key_hash"]))


def list_keys(conn: sqlite3.Connection, *, tenant_id: str | None = None) -> list[sqlite3.Row]:
    if tenant_id:
        return conn.execute(
            "SELECT * FROM api_keys WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)
        ).fetchall()
    return conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()


def to_public(row: sqlite3.Row) -> dict[str, object]:
    """对外表示。永远不含 key_hash——它虽不可逆推明文，但没必要外泄。"""
    return {
        "key_prefix": row["key_prefix"],
        "tenant_id": row["tenant_id"],
        "label": row["label"],
        "daily_token_quota": row["daily_token_quota"],
        "rpm_limit": row["rpm_limit"],
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
        "last_used_at": row["last_used_at"],
        "active": row["revoked_at"] is None,
    }


def key_age_days(row: sqlite3.Row) -> int:
    """密钥年龄。用于「该轮换了」的巡检，不作强制。"""
    created = datetime.fromisoformat(str(row["created_at"]))
    return (datetime.now() - created).days


def stale(conn: sqlite3.Connection, *, days: int = 90) -> list[sqlite3.Row]:
    """列出超过 N 天未吊销的密钥。轮换提醒，不自动吊销。"""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="milliseconds")
    return conn.execute(
        "SELECT * FROM api_keys WHERE revoked_at IS NULL AND created_at < ? ORDER BY created_at",
        (cutoff,),
    ).fetchall()
