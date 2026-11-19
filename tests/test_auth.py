"""密钥层的回归测试。

三条最要紧的性质：

1. **明文只出现一次**：库里任何一列都不能存下它，也不存在任何「查回明文」的路径
2. **无效 / 不存在 / 已吊销 说同一句话**：区分它们等于送出一个密钥探测接口
3. **吊销立即生效**：不是「下次登录时生效」
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from xborder_insight.errors import InputInvalid, Unauthorized
from xborder_insight.security import keys


def _now() -> datetime:
    return datetime.now()


def test_plaintext_is_never_stored(conn):
    """逐列比对：没有任何一列存着明文。"""
    raw, row = keys.create(conn, tenant_id="acme", label="prod")
    assert raw.startswith(keys.KEY_PREFIX)
    assert row["key_hash"] == keys.hash_key(raw)

    for name in row.keys():
        assert str(row[name]) != raw, f"列 {name} 存了明文"

    # 前缀是明文的一小段，只够人工辨认，不够还原
    assert raw.startswith(str(row["key_prefix"]))
    assert len(str(row["key_prefix"])) < len(raw) / 2


def test_same_plaintext_hashes_deterministically(conn):
    """同一明文必须得到同一哈希，否则按哈希等值查询就失效。"""
    raw, _ = keys.create(conn, tenant_id="acme")
    assert keys.hash_key(raw) == keys.hash_key(raw)
    assert keys.hash_key(raw) != keys.hash_key(keys.generate())


def test_each_key_is_unique(conn):
    raws = {keys.create(conn, tenant_id="acme")[0] for _ in range(5)}
    assert len(raws) == 5


def test_authenticate_returns_the_key_row(conn):
    raw, created = keys.create(conn, tenant_id="acme", label="prod", daily_token_quota=1000, rpm_limit=30)
    row = keys.authenticate(conn, raw)
    assert row["key_hash"] == created["key_hash"]
    assert row["tenant_id"] == "acme"
    assert row["daily_token_quota"] == 1000
    assert row["rpm_limit"] == 30


def test_bearer_style_whitespace_is_tolerated(conn):
    """头里多一个空格不该导致 401——它在本地永远是「能跑」的，联调时才炸。"""
    raw, _ = keys.create(conn, tenant_id="acme")
    assert keys.authenticate(conn, f"  {raw}  ")["tenant_id"] == "acme"


def test_malformed_and_unknown_share_one_message(conn):
    """格式不对 / 不存在 / 已吊销 必须给出完全相同的信息。

    只要「已吊销」和「不存在」的报错不同，攻击者就能用它判断某个 key
    曾经是否有效，这是密钥系统最典型的侧信道。
    """
    raw, _ = keys.create(conn, tenant_id="acme")
    keys.revoke(conn, raw[: keys.HINT_LEN])

    messages = set()
    for candidate in (raw, keys.generate(), "not-a-key"):
        with pytest.raises(Unauthorized) as exc:
            keys.authenticate(conn, candidate)
        messages.add(exc.value.message)

    assert messages == {"API Key 无效"}, messages


def test_missing_key_says_what_to_send(conn):
    """没带 key 与 key 错了是两件事：前者是调用方漏了，值得告诉他怎么带。

    这与上面那条不矛盾——「请求里根本没有凭据」不是关于某个 key 的信息，
    所以说明它不会泄漏任何东西；而「这个 key 有效过吗」才是必须藏起来的。
    """
    for candidate in (None, "", "   "):
        with pytest.raises(Unauthorized) as exc:
            keys.authenticate(conn, candidate)
        assert "Authorization: Bearer" in (exc.value.detail or "")


def test_revocation_takes_effect_immediately(conn):
    raw, _ = keys.create(conn, tenant_id="acme")
    assert keys.authenticate(conn, raw)["tenant_id"] == "acme"

    keys.revoke(conn, raw[: keys.HINT_LEN])
    with pytest.raises(Unauthorized):
        keys.authenticate(conn, raw)


def test_revoke_is_idempotent(conn):
    """重复吊销不该报错，但也不该刷新 revoked_at——那会掩盖真实的吊销时间。"""
    raw, _ = keys.create(conn, tenant_id="acme")
    first = keys.revoke(conn, raw[: keys.HINT_LEN])
    second = keys.revoke(conn, raw[: keys.HINT_LEN])
    assert second["revoked_at"] == first["revoked_at"]


def test_ambiguous_prefix_is_refused(conn):
    """前缀撞了就必须拒绝，不能「挑第一个」。

    挑第一个的后果：你以为吊销了 leaking-key，实际吊销了 prod-key，
    而 leaking-key 还活着。
    """
    r1, _ = keys.create(conn, tenant_id="a")
    r2, _ = keys.create(conn, tenant_id="b")
    conn.execute(
        "UPDATE api_keys SET key_prefix = 'xb_duplicate' WHERE key_hash IN (?, ?)",
        (keys.hash_key(r1), keys.hash_key(r2)),
    )
    with pytest.raises(InputInvalid) as exc:
        keys.revoke(conn, "xb_duplicate")
    assert "2 把" in exc.value.message


def test_revoke_by_full_hash(conn):
    raw, row = keys.create(conn, tenant_id="acme")
    revoked = keys.revoke(conn, str(row["key_hash"]))
    assert revoked["revoked_at"] is not None
    with pytest.raises(Unauthorized):
        keys.authenticate(conn, raw)


def test_revoke_unknown_identifier_is_rejected(conn):
    with pytest.raises(InputInvalid):
        keys.revoke(conn, "xb_doesnotexist")


def test_last_used_is_written_then_throttled(conn):
    """last_used_at 是观测字段：要更新，但不能每次请求都写。"""
    raw, _ = keys.create(conn, tenant_id="acme")
    assert keys.by_hash(conn, keys.hash_key(raw))["last_used_at"] is None

    keys.authenticate(conn, raw)
    first = keys.by_hash(conn, keys.hash_key(raw))["last_used_at"]
    assert first is not None

    keys.authenticate(conn, raw)
    assert keys.by_hash(conn, keys.hash_key(raw))["last_used_at"] == first, "节流窗内不该重复写"

    # 把时间拨老一分钟，下一次鉴权就该重新写
    old = (_now() - timedelta(seconds=120)).isoformat(timespec="milliseconds")
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?", (old, keys.hash_key(raw)))
    keys.authenticate(conn, raw)
    assert keys.by_hash(conn, keys.hash_key(raw))["last_used_at"] != old


def test_quota_and_rpm_are_validated_at_creation(conn):
    with pytest.raises(InputInvalid):
        keys.create(conn, tenant_id="", label="x")
    with pytest.raises(InputInvalid):
        keys.create(conn, tenant_id="acme", daily_token_quota=0)
    with pytest.raises(InputInvalid):
        keys.create(conn, tenant_id="acme", rpm_limit=0)


def test_none_quota_means_unlimited_not_zero(conn):
    """None 与 0 必须区分：0 是「一条都不许」，None 是「没设上限」。"""
    _, row = keys.create(conn, tenant_id="acme", daily_token_quota=None)
    assert row["daily_token_quota"] is None


def test_to_public_hides_the_hash(conn):
    _, row = keys.create(conn, tenant_id="acme", label="prod")
    public = keys.to_public(row)
    assert "key_hash" not in public
    assert public["tenant_id"] == "acme"
    assert public["active"] is True


def test_list_filters_by_tenant(conn):
    keys.create(conn, tenant_id="a")
    keys.create(conn, tenant_id="b")
    keys.create(conn, tenant_id="b")
    assert len(keys.list_keys(conn, tenant_id="b")) == 2
    assert len(keys.list_keys(conn)) == 3


def test_stale_lists_old_but_active_keys_only(conn):
    raw, row = keys.create(conn, tenant_id="old")
    keys.create(conn, tenant_id="new")
    old_stamp = (_now() - timedelta(days=120)).isoformat(timespec="milliseconds")
    conn.execute("UPDATE api_keys SET created_at = ? WHERE key_hash = ?", (old_stamp, keys.hash_key(raw)))

    stale = keys.stale(conn, days=90)
    assert [r["tenant_id"] for r in stale] == ["old"]
    assert keys.key_age_days(stale[0]) >= 119

    keys.revoke(conn, raw[: keys.HINT_LEN])
    assert keys.stale(conn, days=90) == []
