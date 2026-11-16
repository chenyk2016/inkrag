"""定义配置、来源和业务结果，片段本身直接使用 LlamaIndex 原生类型。"""

from typing import Annotated, Literal

from inkparse.models import FileFormat
from llama_index.core.schema import NodeWithScore
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class Error(Exception):
    """携带安全业务说明及关联记录，不在核心中绑定 HTTP 状态码。"""

    def __init__(self, code: str, message: str, record_id: str | None = None) -> None:
        """保存可由接入层展示的错误信息，原始提供方异常不进入说明。"""
        super().__init__(message)
        self.code = code
        self.record_id = record_id


class StrictModel(BaseModel):
    """严格校验业务边界，不自动转换类型或接受未知字段。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ProcessingConfig(StrictModel):
    """按格式选择切分策略，预算单位为 LlamaIndex 默认 tokenizer 的 token。"""

    chunking: Literal["auto", "sentence", "markdown"] = "auto"
    chunk_size: int = Field(default=256, ge=32, le=8192)
    chunk_overlap: int = Field(default=32, ge=0)
    include_title: bool = False

    @model_validator(mode="after")
    def check_overlap(self) -> "ProcessingConfig":
        """要求切分可以向前推进，避免重叠等于或超过预算。"""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self


class HybridConfig(StrictModel):
    """控制两路候选深度和原生融合方式；未设置的深度随查询 top_k 决定。"""

    keyword_top_k: int | None = Field(default=None, ge=1, le=100)
    vector_top_k: int | None = Field(default=None, ge=1, le=100)
    fusion: Literal["rrf", "relative_score"] = "rrf"
    keyword_weight: float | None = Field(default=None, gt=0, lt=1)

    @model_validator(mode="after")
    def check_weight(self) -> "HybridConfig":
        """RRF 按名次融合，不接受只对归一化分数融合有效的权重。"""
        if self.keyword_weight is not None and self.fusion != "relative_score":
            raise ValueError("keyword_weight 仅适用于 relative_score")
        return self


class Source(StrictModel):
    """同一原件的不可变存储引用及可展示名称。"""

    name: str
    uri: str


class PictureOrigin(StrictModel):
    """人工图意正文与独立图片原件的精确关联。"""

    kind: Literal["reviewed_picture_description"] = "reviewed_picture_description"
    element_ref: str
    asset_id: str
    page: int | None


class ParseOrigin(StrictModel):
    """保留解析包身份和原件摘要；source.uri仍指向RAG自己保存的输入载体。"""

    result_id: str
    revision: int
    original_name: str
    original_sha256: str
    element_ref: str
    element_locator: str | None = None
    source_asset_id: str | None = None
    related_asset_ids: list[str] = Field(default_factory=list[str])
    picture_origin: PictureOrigin | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class Provenance(StrictModel):
    """所有格式共享的节点来源，缺失页码或章节使用空值。"""

    tenant_id: Literal["local"] = "local"
    knowledge_base_id: Literal["default"] = "default"
    document_id: str
    resource_id: str
    resource_revision: int = Field(ge=1)
    source: Source
    format: FileFormat
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    indexed_title: str | None = None
    processing_revision: str
    parse_origin: ParseOrigin | None = None
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class IngestionResult(StrictModel):
    """已提交的资料与快照凭据，document_id 可用于后续更新同一资料。"""

    document_id: str
    resource_id: str
    resource_revision: int
    snapshot_id: str
    node_count: int
    job_id: str


class RetrievalResult(StrictModel):
    """原生检索证据及诊断关联，空结果仍是成功执行。"""

    hits: list[NodeWithScore]
    trace_id: str
    snapshot_id: str | None
    release_version: int | None = None


class Job(StrictModel):
    """资料处理的实际状态和阶段，未完成任务不会被标记成功。"""

    job_id: str
    status: Literal["running", "succeeded", "failed"]
    stage: str
    error_code: str | None
    message: str | None
    snapshot_id: str | None


type DocumentId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]
type Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
type TopK = Annotated[int, Field(ge=1, le=100)]
type RetrievalMode = Literal["vector", "keyword", "hybrid"]
