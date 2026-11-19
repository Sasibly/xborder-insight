"""任务化：排队、幂等、租约、执行。

对外的入口只有两个：
- `store.submit()` / `store.get()` 等纯数据操作（同步，便于单测）
- `JobRunner` 执行器（异步，负责真正跑起来）

刻意不提供「全局单例 runner」：单例会让测试之间互相污染状态，
而状态一旦藏起来，flaky 测试就永远修不掉。
"""

from __future__ import annotations

from . import models, store
from .runner import JobRunner, new_worker_id

__all__ = ["models", "store", "JobRunner", "new_worker_id"]
