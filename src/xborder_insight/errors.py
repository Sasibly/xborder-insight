"""类型化错误体系。

对外只暴露规范化结构 {code, message, request_id}，绝不泄露堆栈或上游细节。
"""

from __future__ import annotations


class AppError(Exception):
    """所有业务异常的基类。"""

    code = "internal_error"
    status_code = 500
    # 需要随响应带回的头部（目前只有 429 的 Retry-After）。空字典是默认值，
    # 用 dict 而不是 None：调用方无需先判空再 merge。
    headers: dict[str, str] = {}

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_public(self, request_id: str) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "request_id": request_id}


class ConfigError(AppError):
    code = "config_invalid"
    status_code = 500


class InputInvalid(AppError):
    code = "input_invalid"
    status_code = 422


class UpstreamError(AppError):
    """LLM / Embedding 供应商侧失败（超时、5xx、限流）。"""

    code = "upstream_error"
    status_code = 502


class OutputUnparseable(AppError):
    """模型返回内容无法满足契约，且重试后仍失败。"""

    code = "output_unparseable"
    status_code = 502


class InvalidStateTransition(AppError):
    """试图让状态机走一条它不允许的路径（例如把已完成的 run 改回 running）。"""

    code = "invalid_state_transition"
    status_code = 409


class JobNotFound(AppError):
    code = "job_not_found"
    status_code = 404


class IdempotencyConflict(AppError):
    """同一个幂等键配了不同的请求体。

    不能返回旧任务当作「重放」：调用方会以为新参数已被接受，
    实际跑的是老参数。结论错了但全程没有报错，是最难发现的那类错。
    """

    code = "idempotency_conflict"
    status_code = 409


class ParamUnverified(AppError):
    """需要某个成本/合规参数，但该参数尚未核实到官方来源。

    这不是「系统坏了」，而是「系统拒绝在没依据的情况下给结论」。
    用 503 而非 500：语义是依赖未就绪，补齐参数即可恢复。
    """

    code = "param_unverified"
    status_code = 503


class ParamOutOfRange(AppError):
    """参数已核实，但本次要评估的取值落在它声明的适用区间之外。

    为什么不合并进 ParamUnverified：「没查到」和「查到了但不管用」是两种事，
    需要的动作完全不同——前者去补来源，后者去补该区间的参数或改口径。
    合成一个码会让后者被当成「再多找找就能解决」。

    用 422 而非 503：这是本次输入与参数适用面的错配，换一个取值区间或
    补一条对应区间的参数即可，重试同样的请求不会变好。
    """

    code = "param_out_of_range"
    status_code = 422


class ReturnRateMissing(AppError):
    """要压测的这一台没有登记退货率。

    与 `ParamUnverified` 分开：那一个是「有这一行、但没核实」，
    这里是「根本没有这一行」。前者去补来源，后者去登记两档——
    动作不同，合成一个码会让「补来源」被当成「多找找就有的」，
    而实际上什么都不会变。

    注意 draft 行**不**报这个错：退货率天然难核实，拒答会让压测无法进行。
    证据强弱随结果一起输出（见 `db.product_returns.ProductReturn.usable`）。
    """

    code = "return_rate_missing"
    status_code = 503


class DbNotReady(AppError):
    """数据库还没初始化（未执行迁移）。

    与「服务内部错误」区分开：这是可自行修复的部署步骤缺失，
    报错信息里直接给出修复命令，比让调用方去猜要省一轮沟通。
    """

    code = "db_not_ready"
    status_code = 503


class Unauthorized(AppError):
    """缺少、无效或已吊销的 API Key。

    三种情况刻意用同一个码、同一句话：区分「这个键不存在」和「这个键已吊销」
    等于送出一个密钥是否曾经有效的探测接口。
    """

    code = "unauthorized"
    status_code = 401
    headers = {"WWW-Authenticate": "Bearer"}


class RateLimited(AppError):
    """单位时间内请求过多。

    与 `QuotaExceeded` 分开：限流是「慢一点」，额度是「今天没了」，
    调用方该做的事完全不同（退避重试 vs 等明天或加额度）。
    """

    code = "rate_limited"
    status_code = 429

    def __init__(self, message: str, *, retry_after: int, detail: str | None = None) -> None:
        super().__init__(message, detail=detail)
        self.retry_after = retry_after
        self.headers = {"Retry-After": str(retry_after)}


class QuotaExceeded(AppError):
    """当日 token 额度已用尽。"""

    code = "quota_exceeded"
    status_code = 429


class FeatureDisabled(AppError):
    """该能力在此部署中被关闭（不是参数错，也不是权限不足）。

    用 403 而不是 404：调用方需要知道「接口存在但没开」，
    否则他会一直怀疑自己拼错了路径。
    """

    code = "feature_disabled"
    status_code = 403
