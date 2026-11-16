"""生产事实模型；不依赖HTTP、具体对象存储或检索实现。"""

import json
import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

MAX_BUILD_DOCUMENTS = 5000
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class DomainError(Exception):
    """可公开的领域拒绝，接入层负责映射协议状态。"""

    def __init__(
        self, code: str, message: str, *, details: dict[str, JsonValue] | None = None
    ) -> None:
        """保存安全错误类别，不携带底层SQL或凭据。"""
        super().__init__(message)
        self.code = code
        self.details = details


class StrictModel(BaseModel):
    """在外部边界重新验证事实，不接受多余字段或隐式类型转换。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Principal(StrictModel):
    """由可信身份适配生成的主体，KB范围为None表示租户内角色授权。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=256)
    roles: list[str] = Field(default_factory=list[str])
    kb_ids: list[str] | None = None


class MetadataField(StrictModel):
    """知识库声明的标量字段；枚举按文字索引，日期只接受ISO自然日。"""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=128)
    type: Literal["text", "number", "date", "boolean", "enum"]
    required: bool = False
    options: list[str] = Field(default_factory=list[str], max_length=100)

    @model_validator(mode="after")
    def validate_options(self) -> "MetadataField":
        """拒绝重复/空枚举项，以及给其它类型挂不可执行的枚举配置。"""
        if self.type == "enum":
            if (
                not self.options
                or len(self.options) != len(set(self.options))
                or any(not value.strip() or len(value) > 128 for value in self.options)
            ):
                raise ValueError("枚举需要1到100个唯一且非空的选项")
        elif self.options:
            raise ValueError("只有枚举字段可指定选项")
        return self


def missing_required_fields(
    values: dict[str, JsonValue], fields: list[MetadataField]
) -> list[MetadataField]:
    """空白必填文字与缺失值同样需要补录，零和布尔否仍是有效值。"""
    return [
        field
        for field in fields
        if field.required
        and (
            field.name not in values
            or (isinstance(value := values[field.name], str) and not value.strip())
        )
    ]


def validate_metadata(
    values: dict[str, JsonValue], fields: list[MetadataField], *, require_complete: bool = True
) -> None:
    """校验已填写的字段；必填规则作用于当前资料和新写入。"""
    if len(values) > 64 or len(json.dumps(values, ensure_ascii=False).encode()) > 16384:
        raise DomainError("invalid_metadata", "业务字段最多64项，总大小不能超过16KiB")
    definitions = {field.name: field for field in fields}
    if set(values) - definitions.keys():
        raise DomainError("invalid_metadata", "请先定义业务字段，再给资料填写值")
    if require_complete and (missing := missing_required_fields(values, fields)):
        raise DomainError(
            "invalid_metadata",
            "请填写必填业务字段：" + "、".join(f"「{field.label}」" for field in missing),
        )
    for field in fields:
        if field.name not in values:
            continue
        value = values[field.name]
        valid = False
        if field.type == "boolean":
            valid = isinstance(value, bool)
        elif field.type == "number":
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and abs(value) <= 1e100
                and math.isfinite(value)
            )
        elif field.type in {"text", "enum", "date"}:
            valid = isinstance(value, str) and len(value) <= 512
            if valid and isinstance(value, str) and field.type == "enum":
                valid = value in field.options
            elif valid and isinstance(value, str) and field.type == "date":
                try:
                    valid = date.fromisoformat(value).isoformat() == value
                except ValueError:
                    valid = False
        if not valid:
            raise DomainError("invalid_metadata", "字段类型或取值无效：" + field.label)


class KnowledgeBase(StrictModel):
    """一个租户内知识库的业务版本和已发布引用。"""

    kb_id: str
    tenant_id: str
    name: str
    revision: int
    authz_epoch: int
    retrieval_epoch: int = 1
    active_release_id: str | None
    auto_activate: bool = True
    created_at: datetime
    fields: list[MetadataField] = Field(default_factory=list[MetadataField])
    kind: Literal["knowledge", "parse"] = "knowledge"


class Document(StrictModel):
    """资料当前解析指针与知识同步指针；原件字节保存在注入资产服务。"""

    document_id: str
    tenant_id: str
    kb_id: str
    name: str
    source_asset_id: str
    result_id: str | None
    revision: int
    indexed_result_id: str | None
    indexed_revision: int | None = None
    selected_result_id: str | None = None
    selected_revision: int | None = None
    selected_metadata: dict[str, JsonValue] | None = None
    metadata: dict[str, JsonValue]
    # 资料创建时冻结的解析工具，列表/详情据此展示“实际用了哪个引擎”，不在前端凭扩展名猜测。
    parser_engine: str = "builtin"
    processing: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    enabled: bool = True
    deleted: bool
    created_at: datetime


class ResultVersion(StrictModel):
    """不可变解析产物元数据，全文通过受控资产接口读取。"""

    document_id: str
    result_id: str
    revision: int
    previous_result_id: str | None
    asset_ids: list[str]
    review_status: Literal["pending", "approved"] = "pending"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime


type JobKind = Literal["parse", "ingest", "build", "evaluate", "delete", "import_preview"]
type JobStatus = Literal[
    "queued",
    "running",
    "retry_wait",
    "cancel_requested",
    "cancelled",
    "succeeded",
    "failed",
    "reconciling",
]


class Job(StrictModel):
    """队列执行以PG事实为准；epoch隔离失去执行权的旧尝试。"""

    job_id: str
    operation_id: str
    tenant_id: str
    kb_id: str
    document_id: str | None
    kind: JobKind
    status: JobStatus
    stage: str
    payload: dict[str, JsonValue]
    result: dict[str, JsonValue] | None
    expected_result_id: str | None
    expected_document_revision: int | None
    attempt: int
    lease_epoch: int
    lease_owner: str | None
    lease_until: datetime | None
    error_code: str | None
    error_diagnostic: dict[str, JsonValue] | None = None
    created_at: datetime
    updated_at: datetime


class OutboxEvent(StrictModel):
    """可靠投递事件只携带业务任务身份，不能包含原件或秘密。"""

    event_id: str
    job_id: str
    attempts: int


class BuildDocument(StrictModel):
    """冻结一次构建的资料与解析版本，用于查询和发布一致性。"""

    document_id: str
    result_id: str
    revision: int
    processing: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    metadata: dict[str, JsonValue]


class Build(StrictModel):
    """待发布或已发布的物理索引generation及其不可变输入清单。"""

    build_id: str
    tenant_id: str
    kb_id: str
    job_id: str
    index_name: str
    model_revision: str
    kb_revision: int
    authz_epoch: int
    manifest: list[BuildDocument]
    document_count: int = Field(default=0, ge=0)
    manifest_bytes: int = Field(default=0, ge=0)
    manifest_included: bool = True
    field_schema: dict[str, Literal["text", "number", "date", "boolean"]] = Field(
        default_factory=dict[str, Literal["text", "number", "date", "boolean"]]
    )
    service_config: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    status: Literal["building", "ready", "failed"]
    created_at: datetime

    @property
    def generation(self) -> str:
        """返回真实物理索引名，避免调用方根据构建ID重新猜测。"""
        return self.index_name


class Release(StrictModel):
    """发布的权威记录；搜索别名不能成为另一份当前状态。"""

    release_id: str
    kb_id: str
    build_id: str
    version: int
    index_name: str
    model_revision: str
    authz_epoch: int
    created_at: datetime

    @property
    def generation(self) -> str:
        """当前请求固定读取此物理版本，发布切换不改变历史实例。"""
        return self.index_name


class IssuedKey(StrictModel):
    """一次性返回业务凭证原文，数据库只保存其摘要。"""

    key_id: str
    token: str
    name: str
    kb_id: str


class ApiKeyInfo(StrictModel):
    """可列举的凭证元信息，禁止回显原文或摘要。"""

    key_id: str
    name: str
    kb_id: str
    revoked: bool
    created_at: datetime


class AuditEvent(StrictModel):
    """不包含正文和秘密的可归责操作记录。"""

    kb_id: str | None = None
    event_id: str
    subject: str
    action: str
    target_id: str
    created_at: datetime


class ExpectedSource(StrictModel):
    """人工相关证据；可指定保存结果、元素或页码，避免同文档误段落也被判对。"""

    document_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    result_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    element_ref: str | None = Field(default=None, pattern=r"^#/(texts|tables|pictures)/[0-9]+$")
    page: int | None = Field(default=None, ge=1)


class EvaluationSample(StrictModel):
    """文档级人工标注样本，不能伪装为自动生成的质量结论。"""

    sample_id: str
    kb_id: str
    query: str
    expected_document_ids: list[str]
    expected_sources: list[ExpectedSource] = Field(default_factory=list[ExpectedSource])
    no_answer: bool = False
    annotation_revision: int = 1
    supersedes_id: str | None = None
    created_at: datetime


class Evaluation(StrictModel):
    """固定发布版本的评测记录，指标由真实检索执行器计算。"""

    evaluation_id: str
    kb_id: str
    release_id: str
    name: str
    metrics: dict[str, JsonValue]
    created_at: datetime
