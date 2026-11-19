"""接入控制层：密钥、限流、配额。

刻意不含任何 HTTP 概念（不读 header、不构造响应）。
这样它既能被 HTTP 层调用，也能被 CLI 与后台任务复用，
而「怎么从请求里拿 key」这类决定留在控制器里。
"""

from __future__ import annotations

from . import keys, quota

__all__ = ["keys", "quota"]
