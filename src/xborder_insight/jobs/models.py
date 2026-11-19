"""任务模型：状态机 + 请求指纹。

状态机（唯一允许的路径）：
    queued  → running → succeeded
                      → failed → queued（重试）
    queued  → failed        （租约恢复时重试预算已耗尽，直接判死）

两条限制与 `runs` 状态机的区别：
    runs 的终态不可回退，因为「一次执行」结束了就是结束了。
    jobs 的 failed 可回 queued，因为「一次请求」失败后可以再算一次——
    但复用的是同一个 job_id，否则幂等就失效了（重试会变成一个"新请求"）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

ALLOWED: dict[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING, FAILED}),
    RUNNING: frozenset({SUCCEEDED, FAILED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset({QUEUED}),
}

TERMINAL = frozenset({SUCCEEDED, FAILED})

KIND_ANALYZE = "analyze"
KIND_DECISION = "decision"
KINDS = frozenset({KIND_ANALYZE, KIND_DECISION})


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in ALLOWED.get(from_status, frozenset())


def fingerprint(payload: dict[str, Any]) -> str:
    """请求指纹：规范化 JSON 的 sha256。

    规范化（sort_keys + 紧凑分隔符）是必须的：调用方把同样内容写成
    `{"a":1,"b":2}` 还是 `{"b":2,"a":1}`，语义完全相同，指纹必须相同。
    否则「同一次重发」会被误判成「同一个键换了内容」而被拒。

    不把 model / 版本号算进指纹：换模型不是换了请求，
    幂等键的含义是「这一次请求」，重放应当返回那次的结果，而不是重新算。
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
