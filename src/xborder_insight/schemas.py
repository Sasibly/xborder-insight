"""数据契约层。

这里是整个系统最重要的一层：模型输出先要过 Pydantic 校验，才允许进入下游。
校验失败不是异常噪音，而是要被统计的指标。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain.fba import PRODUCT_CLASSES, SIZE_TIERS
from .domain.incoterm import Incoterm
from .taxonomy import LABEL_KEYS, TAXONOMY

Polarity = Literal["negative", "positive", "neutral"]

# 贸易术语闭集。契约层与领域层共用同一份定义，避免两处各写一遍后悄悄不一致。
INCOTERM_ALL = frozenset(Incoterm.ALL)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(min_length=1, max_length=64)
    asin: str = Field(min_length=1, max_length=32)
    rating: int = Field(ge=1, le=5)
    title: str = ""
    body: str = Field(min_length=1, max_length=4000)
    lang: str | None = None


class ExtractedPoint(BaseModel):
    """单条评论抽取出的一个痛点/卖点。label 必须在闭集内。"""

    model_config = ConfigDict(extra="forbid")

    label: str
    polarity: Polarity = "negative"
    evidence: str = Field(min_length=1, max_length=240)
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)

    @field_validator("label")
    @classmethod
    def _label_in_taxonomy(cls, value: str) -> str:
        if value not in LABEL_KEYS:
            raise ValueError(f"label 不在 taxonomy 闭集内: {value!r}")
        return value


class ReviewExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    is_relevant: bool = True
    points: list[ExtractedPoint] = Field(default_factory=list)

    @field_validator("points")
    @classmethod
    def _dedupe_points(cls, points: list[ExtractedPoint]) -> list[ExtractedPoint]:
        seen: set[str] = set()
        result: list[ExtractedPoint] = []
        for point in points:
            if point.label in seen:
                continue
            seen.add(point.label)
            result.append(point)
        return result


class Theme(BaseModel):
    theme_id: str
    label: str
    # 极性是聚类分组键的一部分，不是事后标注：正面评价要「维持」，负面评价要「改进」，
    # 两者不是同一个改进行动。实测 43 对跨极性样本中判为同主题的有 0 对
    # （见 docs/05-聚类阈值标定.md），混在一起聚必然产生自相矛盾的机会点。
    polarity: str = "mixed"
    display_name: str
    mention_count: int
    negative_ratio: float
    severity: float
    opportunity_score: float
    scene_count: int = 1
    evidence: list[str] = Field(default_factory=list)


class AnalysisReport(BaseModel):
    dataset: str
    review_count: int
    parsed_count: int
    parse_success_rate: float
    themes: list[Theme]
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    elapsed_ms: int


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str | None = None
    reviews: list[Review] | None = None
    top_k: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "AnalyzeRequest":
        # 用 is not None 而非真值判断：显式传 {"reviews": []} 时，原实现因空数组
        # 为假值而静默回落到 dataset，属于「猜调用方意图」。宁可 422 报错。
        has_dataset = self.dataset is not None
        has_reviews = self.reviews is not None
        if not has_dataset and not has_reviews:
            raise ValueError("dataset 与 reviews 至少提供一个")
        if has_dataset and has_reviews:
            raise ValueError("dataset 与 reviews 不能同时提供")
        return self


class CostProfileInput(BaseModel):
    """调用方提供的成本档案。

    为什么由调用方提供而不是从 cost_params 读：
        内置参数表存的是「我们核实过的市场费率」，目前 11 项全部为 draft。
        若利润模型只能从参数表读，接口在参数补齐前永远 503，无法验证。
        让调用方带上自己的报价与费率，接口立刻可用，且责任边界清晰——
        我们在响应里标注 data_provenance，不把他人的估算冒充成我们的核实结论。

    校验放在这里而不是模型里：入参不合法应该在进业务逻辑之前就被拒，
    并且错误信息要指出是哪个字段。模型内的校验是第二道防线，不是第一道。
    """

    model_config = ConfigDict(extra="forbid")

    # 报价，口径由 incoterm 决定：FOB 下是离岸价，DDP 下是完税到门价
    quoted_price: float = Field(ge=0)
    selling_price: float = Field(gt=0)

    inbound_freight: float = Field(default=0.0, ge=0)
    duty_rate: float = Field(default=0.0, ge=0, le=1)

    referral_rate: float = Field(ge=0, le=1)

    # FBA 费：知道就给数，不知道就交给费率表算包络。**没有「默认 0」这个选项**——
    # 标准尺寸商品的配送费在 $2.4–13.4 之间，默认 0 等于每件静默多算这么多毛利。
    fba_fee: float | None = Field(default=None, ge=0)
    # 算包络必需的两个维度：用哪套表（产品类别）、哪一期（发货日期）。
    product_class: str | None = None
    ship_date: date | None = None
    # 「部分已知」的收窄条件：知道是标准件、知道重量上界，就不该按全表 20 lb 报区间。
    fba_size_tiers: list[str] | None = None
    fba_weight_range_oz: tuple[float, float] | None = None

    return_rate: float = Field(default=0.0, ge=0, le=1)
    returns_handling_fee: float = Field(default=0.0, ge=0)
    returns_refund_admin_rate: float = Field(default=0.20, ge=0, le=1)
    returns_goods_loss_ratio: float = Field(default=0.0, ge=0, le=1)
    returns_inbound_loss_ratio: float = Field(default=0.0, ge=0, le=1)

    incoterm: str | None = None

    @field_validator("incoterm")
    @classmethod
    def _known_incoterm(cls, value: str | None) -> str | None:
        """术语拼错必须在入参层就被拒。

        FOBb 这类拼写如果不拦，模型会把它当成未知术语抛错（也能拦住），
        但错误发生在业务层，日志里看起来像「模型算错了」而不是「调用方写错了」。
        """
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in INCOTERM_ALL:
            raise ValueError(f"incoterm 只能是 {sorted(INCOTERM_ALL)} 之一，收到 {value!r}")
        return normalized

    @field_validator("product_class")
    @classmethod
    def _known_product_class(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in PRODUCT_CLASSES:
            raise ValueError(
                f"product_class 只能是 {list(PRODUCT_CLASSES)} 之一，收到 {value!r}"
                "（官方按非服装/服装/危险品分三套费率，不能默认）"
            )
        return value

    @field_validator("fba_size_tiers")
    @classmethod
    def _known_size_tiers(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = [tier for tier in value if tier not in SIZE_TIERS]
        if unknown:
            raise ValueError(
                f"未知的尺寸段 {unknown}，可选值 {list(SIZE_TIERS)}"
                "（尺寸段由包装后尺寸决定，不能猜）"
            )
        return value

    @model_validator(mode="after")
    def _fba_must_be_resolvable(self) -> "CostProfileInput":
        """FBA 费要么给数，要么给足算包络的条件；两条都不满足就 422。

        没有第三种处理方式。历史实现把 fba_fee 默认成 0，结果是「不填 = 免费配送」，
        毛利率比真实值高出 $2.4–13.4，而响应里没有任何标记说明这是漏填。
        缺失必须在入参层被拒，而不是在下游被当成 0。
        """
        if self.fba_fee is not None:
            return self

        missing = [
            name
            for name, value in (("product_class", self.product_class), ("ship_date", self.ship_date))
            if value is None
        ]
        if missing:
            raise ValueError(
                f"未提供 fba_fee 时必须给出 {'、'.join(missing)}，"
                "否则无法从官方费率表算费用包络；FBA 费不允许默认按 0 计"
            )

        if self.fba_weight_range_oz is not None:
            low, high = self.fba_weight_range_oz
            if low <= 0 or high < low:
                raise ValueError(
                    f"fba_weight_range_oz 不合法：{self.fba_weight_range_oz!r}，"
                    "下界须大于 0、上界不得小于下界（单位 oz）"
                )
        return self


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asin: str = Field(min_length=1, max_length=32)
    category: str = Field(min_length=1, max_length=64)
    marketplace: str = Field(default="US", min_length=2, max_length=8)
    opportunity_score: float = Field(ge=0, le=1)
    cost: CostProfileInput
    data_provenance: str = Field(default="caller_provided", max_length=120)


class AnalyzeJobPayload(AnalyzeRequest):
    """异步任务：评论分析。继承同步契约，保证两条入口的校验规则完全一致。"""

    kind: Literal["analyze"] = "analyze"


class DecisionJobPayload(DecisionRequest):
    """异步任务：选品决策。"""

    kind: Literal["decision"] = "decision"


# 判别式联合：用 `kind` 直接分流，而不是「先试解析 A 再试解析 B」。
# 后者在字段重叠时会把两种错误混成一条含糊的 422，调用方无法判断该改哪里。
JobPayload = Annotated[
    Union[AnalyzeJobPayload, DecisionJobPayload],
    Field(discriminator="kind"),
]


def display_name(label: str) -> str:
    return TAXONOMY.get(label, label)
