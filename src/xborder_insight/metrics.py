"""运行指标（`/metrics`）：把库里的台账聚合成 Prometheus 文本格式。

四条约束，都是为了让监控不成为「第二个真相源」：

1. **只读台账，不建计数器**。理由与 `security/quota.py` 相同：独立计数器一定
   会漂移（补记、重试、手工修数），而台账不会。`/metrics` 只是换一种表示方式。
2. **不引第三方依赖**。`prometheus_client` 带一个进程级注册表，用例之间会
   互相污染；而 Prometheus 文本格式本身就是一个字符串。所以这里是纯函数：
   进库、出文本，无状态、可快照。
3. **没有样本时不出该族**。一次都没跑过任务时输出 `xbi_run_latency_ms 0`
   会让人以为延迟是 0ms。Prometheus 语义里「没有这个时间序列」才是诚实的。
4. **标签值必须转义**。租户名来自外部输入，一个引号就能把整段 exposition
   变成非法格式——那时候监控不是「少一个指标」，是整页解析失败。

已知命名债：延迟样本取自 `runs.latency_p95_ms`，但那个字段存的是**单个 run 的
端到端耗时**（`jobs/handlers.py` 传的是 `report.elapsed_ms`），不是 p95。
跨 run 取分位才是 p50/p95。字段名要改得按 m0010 那套手法重建 `runs` 表，
属于单独一次改动；在那之前，本模块的分位数一律现算，不复用字段名里的含义。
"""

from __future__ import annotations

import math
import sqlite3

from .security import quota as quota_mod

#: `/metrics` 的 Content-Type。`version=0.0.4` 是 Prometheus 文本格式的版本，
#: 不是本项目的版本——写成别的值，抓取端会按未知格式处理。
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: 状态枚举写死而不是 `SELECT DISTINCT`：新库没有任务时也要输出 0，
#: 否则告警规则里的 `sum(rate(...))` 会因为序列消失而静默失效。
STATUSES = ("queued", "running", "succeeded", "failed")
TERMINAL = ("succeeded", "failed")


def _label(value: str) -> str:
    """转义标签值。顺序有讲究：反斜杠必须先转，否则会把后面补的转义又转一遍。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _percentile(values: list[float], p: float) -> float:
    """nearest-rank 分位，与 `scripts/run_eval.py` 同口径。

    样本少时不插值：插值会造出一个「看起来更平滑」的尾部，掩盖真实抖动。
    运维看这个数是为了判断「有没有毛刺」，不是为了让曲线好看。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return float(ordered[rank - 1])


def _family(name: str, help_text: str, kind: str, samples: list[str]) -> list[str]:
    """一个指标族的三行头 + 样本。help 里不许出现换行，那是格式分隔符。"""
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}", *samples]


def collect(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None = None,
    daily_quota: int | None = None,
    day: str | None = None,
) -> str:
    """返回一段 Prometheus 文本。`tenant_id` 为空时不输出配额族。

    配额只对**调用者自己的租户**输出，不做全租户枚举：`/metrics` 通常挂在
    内网、由抓取端免密访问，把别的租户名和额度印在上面，等于用监控绕过了
    `api.py` 里那套「跨租户一律 404」的隔离。
    """
    out: list[str] = []

    counts = {status: 0 for status in STATUSES}
    unknown = 0
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        status = str(row["status"])
        if status in counts:
            counts[status] = int(row["n"])
        else:
            # 库里有 CHECK 兜着，理论上到不了这里；真到了就单独计数而不是丢弃，
            # 否则「总数对不上」会变成一个查不出原因的现象。
            unknown += int(row["n"])

    out += _family(
        "xbi_jobs_total",
        "任务数（按状态）",
        "gauge",
        [f'xbi_jobs_total{{status="{status}"}} {counts[status]}' for status in STATUSES]
        + ([f'xbi_jobs_total{{status="unknown"}} {unknown}'] if unknown else []),
    )

    terminal = counts["succeeded"] + counts["failed"]
    if terminal:
        out += _family(
            "xbi_jobs_success_ratio",
            "终态任务成功率 = succeeded / (succeeded + failed)",
            "gauge",
            [f"xbi_jobs_success_ratio {counts['succeeded'] / terminal:.6f}"],
        )

    latencies = [
        float(row[0])
        for row in conn.execute(
            "SELECT latency_p95_ms FROM runs "
            "WHERE status = 'succeeded' AND latency_p95_ms IS NOT NULL"
        )
    ]
    if latencies:
        out += _family(
            "xbi_run_latency_ms",
            "单次分析的端到端耗时（每个 run 一个样本，跨 run 取分位）",
            "gauge",
            [
                f'xbi_run_latency_ms{{quantile="0.5"}} {_percentile(latencies, 50):.0f}',
                f'xbi_run_latency_ms{{quantile="0.95"}} {_percentile(latencies, 95):.0f}',
                f"xbi_run_latency_samples {len(latencies)}",
            ],
        )

    row = conn.execute(
        "SELECT COALESCE(SUM(llm_calls), 0) AS calls, "
        "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0) AS completion_tokens "
        "FROM runs WHERE status = 'succeeded'"
    ).fetchone()
    out += _family(
        "xbi_llm_calls_total",
        "成功执行的 run 累计调用模型次数",
        "counter",
        [f"xbi_llm_calls_total {int(row['calls'])}"],
    )
    out += _family(
        "xbi_tokens_total",
        "成功执行的 run 累计 token 消耗",
        "counter",
        [
            f'xbi_tokens_total{{kind="prompt"}} {int(row["prompt_tokens"])}',
            f'xbi_tokens_total{{kind="completion"}} {int(row["completion_tokens"])}',
        ],
    )

    if tenant_id is not None:
        usage = quota_mod.usage_today(conn, tenant_id, day=day)
        tenant = _label(tenant_id)
        samples = [
            f'xbi_quota_tokens_used{{tenant="{tenant}"}} {usage.tokens}',
            f'xbi_quota_jobs_used{{tenant="{tenant}"}} {usage.jobs}',
        ]
        # 额度为空表示不限量。此时**不输出 limit=0**——0 在告警里意味着
        # 「额度耗尽」，与「没有额度限制」是完全相反的两件事。
        if daily_quota is not None:
            samples += [
                f'xbi_quota_tokens_limit{{tenant="{tenant}"}} {daily_quota}',
                f'xbi_quota_tokens_remaining{{tenant="{tenant}"}} '
                f"{max(0, daily_quota - usage.tokens)}",
            ]
        out += _family(
            "xbi_quota_tokens_used",
            "当日 token 已用（按调用者租户；聚合 jobs 台账，与配额判定同源）",
            "gauge",
            samples,
        )

    return "\n".join(out) + "\n"
