"""迁移注册表。

约定（违反即视为迁移不合格）：
1. 每个迁移必须有 `up()` 与其严格逆操作 `down()`；
2. 迁移编号只增不改：已发布的迁移文件禁止再修改，改结构只能新增一个；
3. `up()` 内不得读取当前时间/随机数——迁移必须可重复执行出同样结果；
4. 一个迁移只做一件事，避免「加表 + 改字段 + 塞数据」混在一起导致回滚不干净。

第 3 条看起来苛刻，但它是「up → down → up 结果完全一致」这一性质的前提。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from . import (
    m0001_init,
    m0002_params,
    m0003_returns_detail,
    m0004_jobs,
    m0005_auth,
    m0006_jobs_archive,
    m0007_params_range,
    m0008_official_verification,
    m0009_fba_schedule,
    m0010_fba_rate_version,
    m0011_fba_official_rates,
    m0012_bulky_rates,
    m0013_param_values,
    m0014_product_returns,
    m0015_coffee_returns,
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]
    down: Callable[[sqlite3.Connection], None]

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "init", m0001_init.up, m0001_init.down),
    Migration(2, "params", m0002_params.up, m0002_params.down),
    Migration(3, "returns_detail", m0003_returns_detail.up, m0003_returns_detail.down),
    Migration(4, "jobs", m0004_jobs.up, m0004_jobs.down),
    Migration(5, "auth", m0005_auth.up, m0005_auth.down),
    Migration(6, "jobs_archive", m0006_jobs_archive.up, m0006_jobs_archive.down),
    Migration(7, "params_range", m0007_params_range.up, m0007_params_range.down),
    Migration(8, "official_verification", m0008_official_verification.up,
              m0008_official_verification.down),
    Migration(9, "fba_schedule", m0009_fba_schedule.up, m0009_fba_schedule.down),
    Migration(10, "fba_rate_version", m0010_fba_rate_version.up,
              m0010_fba_rate_version.down),
    Migration(11, "fba_official_rates", m0011_fba_official_rates.up,
              m0011_fba_official_rates.down),
    Migration(12, "bulky_rates", m0012_bulky_rates.up, m0012_bulky_rates.down),
    Migration(13, "param_values", m0013_param_values.up, m0013_param_values.down),
    Migration(14, "product_returns", m0014_product_returns.up,
              m0014_product_returns.down),
    Migration(15, "coffee_returns", m0015_coffee_returns.up,
              m0015_coffee_returns.down),
)

BY_VERSION: dict[int, Migration] = {m.version: m for m in MIGRATIONS}
LATEST: int = max(BY_VERSION) if BY_VERSION else 0


def ordered() -> list[Migration]:
    return sorted(MIGRATIONS, key=lambda m: m.version)


def get(version: int) -> Migration:
    if version not in BY_VERSION:
        raise KeyError(f"不存在的迁移版本: {version}")
    return BY_VERSION[version]
