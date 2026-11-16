"""定义独立解析的版本化JSON边界，正文复用Docling原生模型。"""

import hashlib
import json
from typing import Literal

from docling_core.types.doc.document import DoclingDocument
from inkstore import AssetId
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

ParserEngine = Literal["builtin", "docling"]

FileFormat = Literal[
    "text", "markdown", "pdf", "html", "json", "xml", "csv", "docx", "pptx", "xlsx", "image"
]


class ParseError(Exception):
    """携带安全错误类别，不绑定HTTP或RAG对象。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        location: str | None = None,
        suggestion: str | None = None,
        retryable: bool = False,
    ) -> None:
        """记录可公开的错误码与说明。"""
        super().__init__(message)
        self.code = code
        self.location = location
        self.suggestion = suggestion
        self.retryable = retryable

    def diagnostic(self) -> "ParseDiagnostic":
        """返回允许跨进程和HTTP传输的说明，不包含异常栈或机器路径。"""
        return ParseDiagnostic(
            code=self.code,
            message=str(self),
            location=self.location,
            suggestion=self.suggestion,
            retryable=self.retryable,
        )


class StrictModel(BaseModel):
    """外层协议拒绝隐式转换与未知字段，原生文档遵守Docling校验。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ParseDiagnostic(StrictModel):
    """解析失败的安全说明；调用者只传入已脱敏的格式和位置描述。"""

    code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    message: str = Field(min_length=1, max_length=1000)
    location: str | None = Field(default=None, max_length=200)
    suggestion: str | None = Field(default=None, max_length=1000)
    retryable: bool = False


class FileSource(StrictModel):
    """仅接受本地文件路径；URL和字节上传留给后续显式接入。"""

    kind: Literal["file"] = "file"
    path: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[^/\\\x00]+$")


class ParseOptions(StrictModel):
    """明确解析工具与格式；缺省内置兼容旧包，Docling模型由宿主离线装配。"""

    engine: ParserEngine = Field(default="builtin", exclude_if=lambda value: value == "builtin")
    ocr_enabled: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    ocr_mode: Literal["auto", "full_page"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    table_structure: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    table_mode: Literal["fast", "accurate"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    format: Literal[
        "auto",
        "text",
        "markdown",
        "pdf",
        "html",
        "json",
        "xml",
        "csv",
        "docx",
        "pptx",
        "xlsx",
        "image",
    ] = "auto"
    max_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)


class ParseRequest(StrictModel):
    """界面、代码与未来HTTP可共用的同步解析输入。"""

    schema_version: Literal["inkparse.request.v1"] = "inkparse.request.v1"
    source: FileSource
    options: ParseOptions = Field(default_factory=ParseOptions)


class SourceInfo(StrictModel):
    """冻结输入字节的描述；不输出解析机器的绝对路径。"""

    name: str = Field(min_length=1)
    format: FileFormat
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=20 * 1024 * 1024)


class EngineInfo(StrictModel):
    """记录实际使用的读取方式及版本，避免把模型能力当成已执行。"""

    name: Literal[
        "utf8",
        "docling",
        "pypdf",
        "beautifulsoup4",
        "stdlib-json",
        "stdlib-xml",
        "stdlib-csv",
        "python-docx",
        "python-pptx",
    ]
    version: str
    docling_core_version: str


class Warning(StrictModel):
    """风险提示不是准确率，也不替代人工质量验收。"""

    code: str
    message: str
    page: int | None = Field(default=None, ge=1)


class AssetBinding(StrictModel):
    """将衍生资源关联到本次Docling元素及原件物理页。"""

    asset_id: AssetId
    element_ref: str = Field(pattern=r"^#/(texts|pictures|tables)/[0-9]+$")
    role: Literal["image", "page_preview", "crop"]
    page: int | None = Field(default=None, ge=1)
    media_type: str = Field(min_length=1, max_length=128)


class ElementOrigin(StrictModel):
    """记录原生元素在原件中的逻辑定位，不把逻辑位置伪装为渲染坐标。"""

    element_ref: str = Field(pattern=r"^#/(texts|pictures|tables)/[0-9]+$")
    locator: str = Field(min_length=1, max_length=4096)
    resource_uri: str | None = Field(default=None, max_length=4096)


class EditorState(StrictModel):
    """v4人工审校投影；排除保留原生元素，人工说明不冒充机器识别或原件定位。"""

    excluded_refs: list[str] = Field(default_factory=list[str], max_length=20000)
    picture_descriptions: dict[str, str] = Field(default_factory=dict[str, str])
    picture_description_refs: dict[str, str] = Field(default_factory=dict[str, str])
    manual_refs: list[str] = Field(default_factory=list[str], max_length=20000)


class ParsedPayload(StrictModel):
    """传输包的事实内容；无检索、租户、嵌入或发布字段。"""

    schema_version: Literal["inkparse.result.v3", "inkparse.result.v4"] = "inkparse.result.v3"
    revision: int = Field(default=1, ge=1)
    previous_result_id: AssetId | None = None
    source: SourceInfo
    options: ParseOptions
    engine: EngineInfo
    coverage: Literal["text_only", "native_structure"] = "text_only"
    document: DoclingDocument
    assets: list[AssetBinding] = Field(default_factory=list[AssetBinding])
    origins: list[ElementOrigin] = Field(default_factory=list[ElementOrigin])
    warnings: list[Warning] = Field(default_factory=list[Warning])
    editor: EditorState | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def check_structure_budget(self) -> "ParsedPayload":
        """在摘要序列化前约束原生表格展开，阻止合并大格放大为巨量JSON。"""
        if (
            len(self.document.texts) + len(self.document.tables) + len(self.document.pictures)
            > 20000
        ):
            raise ValueError("原生内容超过20000个元素")
        expanded_bytes = 0
        cells_count = 0
        grid_count = 0
        for table in self.document.tables:
            data = table.data
            if data.num_rows < 1 or data.num_cols < 1 or data.num_rows * data.num_cols > 100000:
                raise ValueError("原生表格超过100000格或维度无效")
            cells_count += len(data.table_cells)
            grid_count += data.num_rows * data.num_cols
            if cells_count > 100000 or grid_count > 100000:
                raise ValueError("原生表格总单元格超过100000项")
            covered = 0
            for cell in data.table_cells:
                rows = cell.end_row_offset_idx - cell.start_row_offset_idx
                columns = cell.end_col_offset_idx - cell.start_col_offset_idx
                if (
                    cell.start_row_offset_idx < 0
                    or cell.start_col_offset_idx < 0
                    or rows < 1
                    or columns < 1
                    or cell.end_row_offset_idx > data.num_rows
                    or cell.end_col_offset_idx > data.num_cols
                ):
                    raise ValueError("原生单元格范围无效")
                covered += rows * columns
                if covered > data.num_rows * data.num_cols:
                    raise ValueError("原生表格合并覆盖数量无效")
                expanded_bytes += (
                    len(json.dumps(cell.text, ensure_ascii=False).encode()) + 256
                ) * (rows * columns + 1)
                if expanded_bytes > 32 * 1024 * 1024:
                    raise ValueError("原生表格展开超过32MiB预算")
        return self


class ParseResult(ParsedPayload):
    """带内容摘要的解析结果；保存与消费时重新校验，防止原生可变模型被改写。"""

    result_id: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def check_digest(self) -> "ParseResult":
        """摘要绑定来源、配置、结构与风险，未知版本或篡改结果明确失败。"""
        if (self.revision == 1) != (self.previous_result_id is None):
            raise ValueError("首版必须没有前序结果，修订版必须指明前序结果")
        if self.previous_result_id == self.result_id:
            raise ValueError("修订结果不能引用自身")
        if self.schema_version == "inkparse.result.v3" and self.editor is not None:
            raise ValueError("人工结构审校必须使用inkparse.result.v4")
        if self.result_id != fingerprint(self):
            raise ValueError("result_id与解析内容不一致")
        if self.source.asset_id is not None and self.source.asset_id != self.source.sha256:
            raise ValueError("原件资产ID与字节摘要不一致")
        for binding in [*self.assets, *self.origins]:
            collection, ordinal = binding.element_ref[2:].split("/")
            counts = {
                "texts": len(self.document.texts),
                "pictures": len(self.document.pictures),
                "tables": len(self.document.tables),
            }
            if int(ordinal) >= counts[collection]:
                raise ValueError("资源引用的文档元素不存在")
            if (
                isinstance(binding, AssetBinding)
                and binding.page is not None
                and binding.page not in self.document.pages
            ):
                raise ValueError("资源引用的物理页不存在")
        if self.assets and self.source.asset_id is None:
            raise ValueError("衍生资源必须关联已存储原件")
        if len({origin.element_ref for origin in self.origins}) != len(self.origins):
            raise ValueError("原件定位不能重复绑定同一元素")
        if not self.document.texts and not self.document.tables and not self.document.pictures:
            raise ValueError("解析结果没有内容元素")
        if self.editor is not None:
            references = {
                item.self_ref
                for item in [*self.document.texts, *self.document.pictures, *self.document.tables]
            }
            if len(set(self.editor.excluded_refs)) != len(self.editor.excluded_refs):
                raise ValueError("排除元素不能重复")
            if not set(self.editor.excluded_refs).issubset(references):
                raise ValueError("排除元素不存在")
            pictures = {item.self_ref for item in self.document.pictures}
            if not set(self.editor.picture_descriptions).issubset(pictures):
                raise ValueError("人工图片说明引用不存在")
            if set(self.editor.picture_description_refs) != set(self.editor.picture_descriptions):
                raise ValueError("人工图片说明必须绑定已保存文字")
            texts = {item.self_ref: item for item in self.document.texts}
            if len(set(self.editor.manual_refs)) != len(self.editor.manual_refs) or not set(
                self.editor.manual_refs
            ).issubset(texts):
                raise ValueError("人工元素必须绑定唯一已保存文字")
            if not set(self.editor.picture_description_refs.values()).issubset(
                self.editor.manual_refs
            ):
                raise ValueError("人工图片说明必须标明人工来源")
            if any(item.element_ref in self.editor.manual_refs for item in self.origins):
                raise ValueError("人工元素不能伪造原件定位")
            if any(texts[ref].prov for ref in self.editor.manual_refs):
                raise ValueError("人工元素不能伪造物理页坐标")
            for picture, reference in self.editor.picture_description_refs.items():
                if (
                    reference not in texts
                    or texts[reference].text != self.editor.picture_descriptions[picture]
                ):
                    raise ValueError("人工图片说明与原生文字不一致")
            if any(
                len(text.encode()) > 1024 * 1024
                for text in self.editor.picture_descriptions.values()
            ):
                raise ValueError("人工图片说明超过1MiB")
        return self


def fingerprint(payload: ParsedPayload) -> str:
    """按稳定JSON规范计算交换内容摘要，不依赖输入对象的字段顺序。"""
    value = TypeAdapter(JsonValue).validate_json(payload.model_dump_json(exclude={"result_id"}))
    if isinstance(value, dict):
        if not payload.origins:
            value.pop("origins", None)
        if payload.schema_version == "inkparse.result.v3":
            value.pop("editor", None)
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
