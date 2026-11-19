"""集中配置：全部来自环境变量，启动时一次性校验并快速失败。

设计意图：配置错误必须在进程启动阶段暴露，而不是在第一个请求进来时才崩。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError
from .domain import incoterm as incoterm_mod

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")

VALID_RUN_MODES = {"mock", "live"}
VALID_EMBEDDING_PROVIDERS = {"hash", "dashscope"}

# 每个向量后端各自的标定阈值（scripts/calibrate_cluster.py 在 120 对人工标注上扫出）。
# 不能共用一个默认值：hash 的「同主题对」中位相似度是 0.147，「不同主题对」是 0.160——
# 不同主题反而更相似，任何单一阈值都只能做近似随机切分；而 dashscope 的中位数分别是
# 0.712 / 0.675，同一批数据的最优切点在 0.65 附近。把 0.28 或 0.65 写死成全局默认，
# 必然有一半场景是错的。
CLUSTER_THRESHOLD_BY_PROVIDER = {
    "hash": 0.12,
    "dashscope": 0.65,
}


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise ConfigError(f"缺少必需的环境变量: {name}")
    return value or ""


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是数字，当前值: {raw!r}") from exc


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，当前值: {raw!r}") from exc


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _get_bool(name: str, default: bool) -> bool:
    """布尔变量只认白名单。

    `JOB_WORKER_ENABLED=maybe` 若被当成「非空即真」，执行器会在一个没人预期的
    状态下启动；反过来若被当成假，任务永远不跑。两种误解都安静无声。
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"环境变量 {name} 必须是 true/false，当前值: {raw!r}")


@dataclass(frozen=True)
class Settings:
    run_mode: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout: float
    llm_max_retries: int
    embedding_provider: str
    embedding_model: str
    embedding_dim: int
    cluster_threshold: float
    # 阈值来自标定结果还是环境变量覆盖。回显到 /ready 与任务结果里，
    # 这样「线上跑的到底是标定值还是某次临时改的值」不需要靠记忆。
    cluster_threshold_source: str
    app_host: str
    app_port: int
    log_level: str
    db_path: str
    default_incoterm: str
    jobs_worker_enabled: bool
    job_lease_seconds: float
    job_max_attempts: int
    job_concurrency: int
    job_poll_seconds: float
    auth_required: bool
    sync_analyze_enabled: bool

    @property
    def is_mock(self) -> bool:
        return self.run_mode == "mock"


def load_settings() -> Settings:
    run_mode = _get("RUN_MODE", "mock").strip().lower()
    if run_mode not in VALID_RUN_MODES:
        raise ConfigError(f"RUN_MODE 只能是 {sorted(VALID_RUN_MODES)}，当前值: {run_mode!r}")

    embedding_provider = _get("EMBEDDING_PROVIDER", "hash").strip().lower()
    if embedding_provider not in VALID_EMBEDDING_PROVIDERS:
        raise ConfigError(
            f"EMBEDDING_PROVIDER 只能是 {sorted(VALID_EMBEDDING_PROVIDERS)}，当前值: {embedding_provider!r}"
        )

    llm_api_key = _get("LLM_API_KEY")
    if run_mode == "live" and not llm_api_key:
        raise ConfigError("RUN_MODE=live 时必须配置 LLM_API_KEY")
    if embedding_provider == "dashscope" and not llm_api_key:
        raise ConfigError("EMBEDDING_PROVIDER=dashscope 时必须配置 LLM_API_KEY")

    # 聚类阈值是「向量的属性」，不是全局常量：同一个 0.28 在字符哈希向量下
    # 与在真实语义向量下意味着完全不同的分区。所以默认值按 provider 取标定结果，
    # 显式设置则一律以显式值为准（方便做对照实验和临时降级）。
    # 标定依据见 docs/05-聚类阈值标定.md，脚本 scripts/calibrate_cluster.py。
    calibrated = CLUSTER_THRESHOLD_BY_PROVIDER[embedding_provider]
    threshold = _get_float("CLUSTER_SIMILARITY_THRESHOLD", calibrated)
    threshold_source = (
        "env" if (os.getenv("CLUSTER_SIMILARITY_THRESHOLD") or "") != "" else "calibrated"
    )
    if not 0.0 < threshold <= 1.0:
        raise ConfigError(f"CLUSTER_SIMILARITY_THRESHOLD 必须在 (0, 1] 区间，当前值: {threshold}")

    embedding_dim = _get_int("EMBEDDING_DIM", 512)
    if embedding_dim < 8:
        raise ConfigError(f"EMBEDDING_DIM 过小（至少 8），当前值: {embedding_dim}")

    # 默认贸易术语：建站初期口径。允许用环境变量改，但不允许填不认识的术语——
    # 一个拼错的 FOBb 会让利润模型在启动时静默按错误分支计价，比直接崩更糟。
    default_incoterm = _get("DEFAULT_INCOTERM", incoterm_mod.DEFAULT_INCOTERM).strip().upper()
    if default_incoterm not in incoterm_mod.TERMS:
        raise ConfigError(
            f"DEFAULT_INCOTERM 只能是 {list(incoterm_mod.Incoterm.ALL)}，"
            f"当前值: {default_incoterm!r}"
        )

    # 任务执行器参数。边界值与 m0004_jobs 里的 CHECK 保持一致：
    # 配置层不拦的话，进程能起来、任务永远写不进库，故障点离原因很远。
    job_max_attempts = _get_int("JOB_MAX_ATTEMPTS", 3)
    if not 1 <= job_max_attempts <= 10:
        raise ConfigError(f"JOB_MAX_ATTEMPTS 必须在 1..10 之间，当前值: {job_max_attempts}")

    job_lease_seconds = _get_float("JOB_LEASE_SECONDS", 120.0)
    if job_lease_seconds < 5:
        raise ConfigError(f"JOB_LEASE_SECONDS 过小（至少 5），当前值: {job_lease_seconds}")

    job_concurrency = _get_int("JOB_CONCURRENCY", 2)
    if job_concurrency < 1:
        raise ConfigError(f"JOB_CONCURRENCY 至少为 1，当前值: {job_concurrency}")

    job_poll_seconds = _get_float("JOB_POLL_SECONDS", 0.5)
    if job_poll_seconds <= 0:
        raise ConfigError(f"JOB_POLL_SECONDS 必须大于 0，当前值: {job_poll_seconds}")

    # 鉴权默认值跟随运行模式：mock 下免鉴权是为了本地跑通的摩擦，
    # live 下必须开——「部署时忘了开鉴权」是这类系统最常见的事故，
    # 而它不会有任何报错，只会安静地对所有人开放。
    auth_required = _get_bool("AUTH_REQUIRED", run_mode == "live")
    # 同步分析接口不落 jobs 台账，因此不受 token 配额约束。
    # 生产环境默认关闭它，把流量收敛到有台账可查的异步任务上。
    sync_analyze_enabled = _get_bool("SYNC_ANALYZE_ENABLED", run_mode == "mock")

    return Settings(
        run_mode=run_mode,
        llm_base_url=_get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        llm_api_key=llm_api_key,
        llm_model=_get("LLM_MODEL", "qwen-plus"),
        llm_timeout=_get_float("LLM_TIMEOUT_SECONDS", 30.0),
        llm_max_retries=_get_int("LLM_MAX_RETRIES", 2),
        embedding_provider=embedding_provider,
        embedding_model=_get("EMBEDDING_MODEL", "text-embedding-v3"),
        embedding_dim=embedding_dim,
        cluster_threshold=threshold,
        cluster_threshold_source=threshold_source,
        app_host=_get("APP_HOST", "127.0.0.1"),
        app_port=_get_int("APP_PORT", 8000),
        log_level=_get("LOG_LEVEL", "INFO"),
        db_path=_get("DB_PATH", str(ROOT_DIR / "data" / "app.sqlite3")),
        default_incoterm=default_incoterm,
        jobs_worker_enabled=_get_bool("JOB_WORKER_ENABLED", True),
        job_lease_seconds=job_lease_seconds,
        job_max_attempts=job_max_attempts,
        job_concurrency=job_concurrency,
        job_poll_seconds=job_poll_seconds,
        auth_required=auth_required,
        sync_analyze_enabled=sync_analyze_enabled,
    )


settings = load_settings()
