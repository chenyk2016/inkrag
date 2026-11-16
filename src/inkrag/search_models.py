"""定义外部索引的配置和响应边界，文档片段继续使用LlamaIndex原生节点。"""

from typing import Literal

from pydantic import Field, JsonValue

from .lexical import LEGACY_LEXICAL, LexicalProfile
from .models import StrictModel

type Scalar = str | int | float | bool
type FieldType = Literal["text", "number", "boolean", "date"]


class SearchFilter(StrictModel):
    """只开放已定义字段的受控条件，不允许调用方传搜索DSL。"""

    field: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    operator: Literal["eq", "ne", "in", "gt", "gte", "lt", "lte", "contains"] = "eq"
    value: JsonValue


class SearchManifest(StrictModel):
    """后端阻止写入且校验完成的候选清单，不等同PG已发布。"""

    generation: str
    node_count: int = Field(ge=0)
    document_count: int = Field(ge=0)
    model_revision: str
    dimensions: int = Field(ge=1, le=65536)


class GenerationMetadata(StrictModel):
    """将索引物理版本绑定可信作用域、模型和字段，不从用户查询猜测。"""

    tenant_id: str
    knowledge_base_id: str
    build_id: str
    model_revision: str
    dimensions: int = Field(ge=1, le=65536)
    field_schema: dict[str, FieldType] = Field(default_factory=dict[str, FieldType])
    node_pipeline_revision: int = Field(default=0, ge=0)
    lexical_profile: LexicalProfile = Field(
        default=LEGACY_LEXICAL, exclude_if=lambda value: value == LEGACY_LEXICAL
    )
    previous_generation: str | None = None
    copy_complete: bool
