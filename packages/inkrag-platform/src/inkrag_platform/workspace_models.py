"""工作台、显式选用与服务控制协议；冻结候选与已发布配置共用定义。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .models import (
    Document,
    DomainError,
    ExpectedSource,
    Job,
    KnowledgeBase,
    MetadataField,
    StrictModel,
    validate_metadata,
)

type CollectionKind = Literal["knowledge", "parse"]
type RetrievalMode = Literal["hybrid", "vector", "keyword"]


class Page[T](StrictModel):
    """同一数据库快照的分页；总数覆盖全部匹配项。"""

    items: list[T]
    total: int = Field(ge=0)
    limit: int
    offset: int


class CollectionSummary(StrictModel):
    """集合事实及有界列表所需计数；解析集合不显示为可发布知识库。"""

    knowledge_base: KnowledgeBase
    document_count: int
    selected_count: int
    job_count: int
    unresolved_count: int


class WorkspaceJob(StrictModel):
    """全局任务保留准确归属和资料名称，不能用当前KB代替来源。"""

    job: Job
    kb_name: str
    collection_kind: CollectionKind
    document_name: str | None


class Overview(StrictModel):
    """主体可见范围内的全局指标，不通过前一百项列表推算。"""

    knowledge_base_count: int
    parse_collection_count: int
    document_count: int
    selected_count: int
    task_count: int
    running_count: int
    queued_count: int
    failed_count: int
    published_count: int


class SelectionRequest(StrictModel):
    """以资料聚合版本做比较后选择已存结果，空值表示取消下次构建选用。"""

    expected_document_revision: int = Field(ge=0)
    result_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirm_content: bool = False
    activate: bool = False


class ActivationSetting(StrictModel):
    """控制新上传清洗完成后的自动生效。"""

    enabled: bool


class AdoptionRequest(StrictModel):
    """复用已解析资产并在目标KB独立管理属性，不触发重新解析。"""

    source_document_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    result_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class Filter(StrictModel):
    """仅允许声明的用户字段及有限操作符，范围字段永远来自可信主体。"""

    field: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    operator: Literal["eq", "ne", "in", "gt", "gte", "lt", "lte", "contains"] = "eq"
    value: JsonValue


class ServiceConfig(StrictModel):
    """服务草稿；构建冻结固定范围与允许调用者提供的字段。"""

    name: str = Field(default="知识检索服务", min_length=1, max_length=255)
    mode: RetrievalMode = "hybrid"
    top_k: int = Field(default=5, ge=1, le=100)
    fixed_filters: list[Filter] = Field(default_factory=list[Filter], max_length=20)
    allowed_fields: list[Literal["mode", "top_k", "filters"]] = Field(
        default_factory=lambda: ["mode", "top_k", "filters"], max_length=3
    )
    allowed_callers: list[str] = Field(default_factory=list[str], max_length=100)
    allowed_filter_fields: list[str] | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def unique_policy(self) -> "ServiceConfig":
        """重复开放字段/调用方不产生隐式优先级，拒绝空主体。"""
        if self.allowed_filter_fields is not None and len(set(self.allowed_filter_fields)) != len(
            self.allowed_filter_fields
        ):
            raise ValueError("动态筛选字段白名单不能重复")
        if len(set(self.allowed_fields)) != len(self.allowed_fields):
            raise ValueError("开放字段不能重复")
        if len(set(self.allowed_callers)) != len(self.allowed_callers) or any(
            not item.strip() or len(item) > 256 for item in self.allowed_callers
        ):
            raise ValueError("调用方必须唯一且非空")
        return self


class ServiceDraft(StrictModel):
    """草稿版本独立比较并同步推进KB业务版本，让候选过期明确可见。"""

    kb_id: str
    revision: int
    config: ServiceConfig
    updated_at: datetime | None


class ServiceUpdate(StrictModel):
    """草稿第一次保存expected_revision=0；后续提交读取到的草稿版本。"""

    expected_revision: int = Field(ge=0)
    config: ServiceConfig


class TrialRequest(StrictModel):
    """动态字段遵守服务策略；详细诊断仅供编辑者候选试查，线上接口拒绝。"""

    query: str = Field(min_length=1, max_length=4000)
    mode: RetrievalMode | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
    filters: list[Filter] | None = Field(default=None, max_length=20)
    diagnostics: bool = False


class QueryTrace(StrictModel):
    """可核查单次检索实际目标和命中来源；不保存原文片段。"""

    trace_id: str
    kb_id: str
    subject: str
    target_id: str
    target_kind: Literal["build", "release"]
    request: dict[str, JsonValue]
    outcome: dict[str, JsonValue]
    created_at: datetime


class CandidateEvaluation(StrictModel):
    """固定候选的逐题指标与来源快照。"""

    evaluation_id: str
    kb_id: str
    build_id: str
    name: str
    metrics: dict[str, JsonValue]
    details: list[dict[str, JsonValue]]
    created_at: datetime


class EvaluationRequest(StrictModel):
    """候选最多逐次评测一百条已保存人工样本。"""

    name: str = Field(min_length=1, max_length=255)
    sample_ids: list[str] = Field(min_length=1, max_length=100)
    top_k: int = Field(default=5, ge=1, le=100)


type DocumentListStatus = Literal[
    "review", "ready", "failed", "processing", "activating", "live", "saved"
]


class DocumentListItem(Document):
    """列表状态和最近活动由资料、版本及后台任务事实派生。"""

    list_status: DocumentListStatus
    activity_at: datetime


class DocumentPage(Page[DocumentListItem]):
    """资料有界清单与准确总数，供独立解析和KB统一使用。"""

    counts: dict[DocumentListStatus, int]


class ReviewRequest(StrictModel):
    """审核绑定不可变解析结果，并校验页面所基于的资料版本。"""

    expected_document_revision: int = Field(ge=0)
    status: Literal["pending", "approved"]


class QualitySampleRequest(StrictModel):
    """问题标注追加保存；无答案与正例不能混在一个样本内。"""

    query: str = Field(min_length=1, max_length=4000)
    expected_document_ids: list[str] = Field(default_factory=list[str], max_length=100)
    expected_sources: list[ExpectedSource] = Field(
        default_factory=list[ExpectedSource], max_length=100
    )
    no_answer: bool = False
    supersedes_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def validate_labels(self) -> "QualitySampleRequest":
        """拒绝无金标正例和含正例来源的无答案标注。"""
        if self.no_answer == bool(self.expected_document_ids or self.expected_sources):
            raise ValueError("无答案样本不能包含金标；正例必须包含相关资料或具体证据")
        if not self.query.strip():
            raise ValueError("问题不能为空")
        return self


def validate_service_filters(config: ServiceConfig, fields: list[MetadataField]) -> None:
    """服务配置与字段声明共用同一规则，避免删字段后草稿才在查询时失效。"""
    definitions = {field.name: field for field in fields}
    if (
        config.allowed_filter_fields is not None
        and not set(config.allowed_filter_fields) <= definitions.keys()
    ):
        raise DomainError("invalid_filter", "动态筛选白名单只能使用此知识库声明的字段")
    for condition in config.fixed_filters:
        if condition.field not in definitions:
            raise DomainError("invalid_filter", "固定范围只能使用此知识库声明的字段")
        definition = definitions[condition.field]
        if condition.operator in {"gt", "gte", "lt", "lte"} and definition.type not in {
            "number",
            "date",
        }:
            raise DomainError("invalid_filter", "范围比较只适用于数值或日期字段")
        if condition.operator == "contains" and definition.type not in {"text", "enum"}:
            raise DomainError("invalid_filter", "包含条件只适用于文字字段")
        values = (
            condition.value
            if condition.operator == "in" and isinstance(condition.value, list)
            else [condition.value]
        )
        if condition.operator == "in" and (
            not isinstance(condition.value, list) or not 1 <= len(condition.value) <= 50
        ):
            raise DomainError("invalid_filter", "多选条件需要1到50个合法值")
        for value in values:
            validate_metadata({condition.field: value}, [definition])
