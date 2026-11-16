"""将不可变原件读取为原生文档，并复用库组件完成格式化切分。"""

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.items.group import GroupItem
from docling_core.types.doc.items.picture.picture import PictureItem
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.table.table_data import RichTableCell
from docling_core.types.doc.items.text import TextItem
from docling_core.types.doc.labels import DocItemLabel
from inkparse import (
    FileSource,
    ParseError,
    ParseOptions,
    Parser,
    ParseRequest,
    ParseResult,
    effective_document,
)
from inkparse.parser import FORMATS as FORMATS
from llama_index.core import Document
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter
from llama_index.core.schema import BaseNode, TextNode
from llama_index.core.utils import get_tokenizer
from pydantic import TypeAdapter

from .models import Error, ParseOrigin, PictureOrigin, ProcessingConfig, Provenance
from .table_chunks import table_rows

MAX_FILE_BYTES = 20 * 1024 * 1024
NODE_PIPELINE_REVISION = 3


def processing_revision(
    config: ProcessingConfig,
    *,
    tokenizer_revision: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """将明确的处理配置与引擎版本绑定，供结果追溯和节点身份使用。"""
    payload = {
        "engine": "llama-index-0.14.25",
        "node_pipeline_revision": NODE_PIPELINE_REVISION,
        **config.model_dump(),
    }
    if tokenizer_revision is not None or max_tokens is not None:
        payload.update(tokenizer_revision=tokenizer_revision, max_tokens=max_tokens)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class NativeText:
    """待索引正文与原解析元素；表格行共享原表定位。"""

    text: str
    element_ref: str
    page: int | None
    section: str | None
    table_context: str | None = None
    table_row_key: str = ""

    def apply_table_context(self, document: Document) -> Document:
        """切分阶段使用表格上下文，成品来源不暴露这些内部字段。"""
        if self.table_context is not None:
            document.metadata["_table_context"] = self.table_context
            document.metadata["_table_row_key"] = self.table_row_key
        return document


def iter_native_texts(result: ParseResult, section: str | None = None) -> Iterator[NativeText]:
    """为 SDK、预览及正式索引提供相同的原生正文和表格行。"""
    effective = effective_document(result)
    heading_page: int | None = None
    indexed_layers = {ContentLayer.BODY, ContentLayer.NOTES}
    rich_cell_roots = {
        cell.ref.cref
        for table in effective.tables
        for cell in table.data.table_cells
        if isinstance(cell, RichTableCell)
    }
    cell_level: int | None = None
    for item, level in effective.iterate_items(
        with_groups=True, included_content_layers=set(ContentLayer)
    ):
        if cell_level is not None and level > cell_level:
            continue
        cell_level = None
        if item.self_ref in rich_cell_roots:
            # 富文本格已在表格行中索引，不再将其子树作为无列头的独立正文。
            cell_level = level
            continue
        if isinstance(item, (GroupItem, PictureItem)) or item.content_layer not in indexed_layers:
            continue
        if not isinstance(item, (TableItem, TextItem)):
            raise Error("unsupported_parse_content", "当前RAG适配尚不支持此结构元素")
        page = item.prov[0].page_no if item.prov else None
        if isinstance(item, TableItem):
            heading = section if heading_page is None or heading_page == page else None
            for text, context, row_key in table_rows(item, effective, heading):
                yield NativeText(text, item.self_ref, page, section, context, row_key)
        else:
            text = item.text
            if not text.strip():
                continue
            if item.label in {DocItemLabel.TITLE, DocItemLabel.SECTION_HEADER}:
                section = text.strip()
                heading_page = page
            yield NativeText(text, item.self_ref, page, section)


def reviewed_picture_origins(result: ParseResult) -> dict[str, PictureOrigin | None]:
    """仅为审校文字绑定唯一的原图；未绑定或存在歧义时保留文本。"""
    if result.editor is None:
        return {}
    descriptions: dict[str, list[str]] = {}
    for picture_ref, text_ref in result.editor.picture_description_refs.items():
        descriptions.setdefault(text_ref, []).append(picture_ref)
    assets: dict[str, set[tuple[str, int | None, str]]] = {}
    for asset in result.assets:
        if asset.role in {"image", "crop"}:
            assets.setdefault(asset.element_ref, set()).add(
                (asset.asset_id, asset.page, asset.media_type)
            )
    origins: dict[str, PictureOrigin | None] = {}
    for text_ref, references in descriptions.items():
        origins[text_ref] = None
        if len(references) != 1:
            continue
        bindings = assets.get(references[0], set())
        if len(bindings) == 1:
            asset_id, page, _media_type = next(iter(bindings))
            origins[text_ref] = PictureOrigin(
                element_ref=references[0], asset_id=asset_id, page=page
            )
    return origins


def related_asset_ids(
    result: ParseResult,
    item: NativeText,
    picture_origins: dict[str, PictureOrigin | None],
) -> list[str]:
    """人工图片说明只引用其绑定图片，其余元素维持既有附件关联。"""
    if item.element_ref in picture_origins:
        picture = picture_origins[item.element_ref]
        return [picture.asset_id] if picture is not None else []
    return sorted(
        {
            asset.asset_id
            for asset in result.assets
            if asset.element_ref == item.element_ref or asset.page == item.page or item.page is None
        }
    )


def result_documents(
    result: ParseResult,
    document_for: Callable[[int, NativeText, ParseOrigin], Document],
    *,
    original_name: str | None = None,
    section: str | None = None,
) -> list[Document]:
    """共享解析来源、审校和表格上下文；调用方补自己的资料身份。"""
    picture_origins = reviewed_picture_origins(result)
    locators = {origin.element_ref: origin.locator for origin in result.origins}
    documents: list[Document] = []
    for ordinal, item in enumerate(iter_native_texts(result, section)):
        origin = ParseOrigin(
            result_id=result.result_id,
            revision=result.revision,
            original_name=original_name or result.source.name,
            original_sha256=result.source.sha256,
            element_ref=item.element_ref,
            element_locator=locators.get(item.element_ref),
            source_asset_id=result.source.asset_id,
            related_asset_ids=related_asset_ids(result, item, picture_origins),
            picture_origin=picture_origins.get(item.element_ref),
        )
        documents.append(item.apply_table_context(document_for(ordinal, item, origin)))
    if not documents:
        raise Error("empty_document", "没有可检索正文")
    return documents


def prepare_result_nodes(
    result: ParseResult,
    config: ProcessingConfig,
    document_for: Callable[[int, NativeText, ParseOrigin], Document],
    *,
    original_name: str | None = None,
    section: str | None = None,
    tokenizer: Callable[[str], list[int]] | None = None,
    max_tokens: int | None = None,
) -> list[BaseNode]:
    """SDK、Worker 和预览共用的片段准备流程，不接触存储或嵌入模型。"""
    documents = result_documents(result, document_for, original_name=original_name, section=section)
    return split_native_documents(documents, config, tokenizer=tokenizer, max_tokens=max_tokens)


def _local_document(
    source: Provenance, ordinal: int, item: NativeText, origin: ParseOrigin
) -> Document:
    """为共享来源补充本地资料版本，并保持表格行各自独立的文档身份。"""
    provenance = source.model_copy(
        update={"page": item.page, "section": item.section, "parse_origin": origin}
    )
    document = make_document(item.text, provenance)
    if item.table_context is not None:
        document.id_ += f":{ordinal}"
    return document


def read_documents(
    path: Path,
    source: Provenance,
    result: ParseResult | None = None,
    original_name: str | None = None,
) -> list[Document]:
    """复用独立解析核心，并仅在RAG边界补充作用域、资料版本及元素来源。

    与生产搜索一样，正文与原生表格参与检索，图片仅作为来源附件。
    新的结构元素未适配时明确拒绝，来源载体与原始文档摘要分开记录。
    """
    try:
        parsed = (
            result
            if result is not None
            else Parser().parse(
                ParseRequest(
                    source=FileSource(path=str(path)), options=ParseOptions(format=source.format)
                )
            )
        )
        parsed = ParseResult.model_validate_json(parsed.model_dump_json())
    except ParseError as exc:
        raise Error(exc.code, str(exc)) from None
    return result_documents(
        parsed,
        partial(_local_document, source),
        original_name=original_name or (source.source.name if result is None else None),
        section=source.section,
    )


def make_document(text: str, source: Provenance) -> Document:
    """把统一来源写入原生文档，文档身份区分资料版本与物理页。"""
    return Document(
        id_=f"{source.resource_id}:{source.resource_revision}:{source.page or 0}:{source.parse_origin.element_ref if source.parse_origin else ''}",
        text=text,
        metadata=source.model_dump(mode="json"),
    )


def split_native_documents(
    documents: list[Document],
    config: ProcessingConfig,
    *,
    tokenizer: Callable[[str], list[int]] | None = None,
    max_tokens: int | None = None,
) -> list[BaseNode]:
    """复用库切分每个解析元素，标题增强与原文通过元数据区分。

    不跨元素或物理页合并。标题优先取最近章节，否则使用资料名称；标题前缀
    占用同一 tokenizer 的预算，原始元素来源保持不变。参数无法容纳标题与
    重叠时明确拒绝。此函数不调用嵌入，可同时服务预览、SDK及正式索引。
    """
    if not documents:
        raise Error("empty_document", "没有可切分正文")
    markdown = config.chunking == "markdown" or (
        config.chunking == "auto" and documents[0].metadata["format"] == "markdown"
    )
    sections: list[BaseNode] = []
    markdown_parser = MarkdownNodeParser() if markdown else None
    for document in documents:
        if markdown_parser is not None and "_table_context" not in document.metadata:
            sections.extend(markdown_parser.get_nodes_from_documents([document]))
        else:
            sections.append(document.model_copy(update={"metadata": dict(document.metadata)}))
    tokenize = tokenizer or get_tokenizer()
    chunk_size = min(config.chunk_size, max_tokens) if max_tokens is not None else config.chunk_size
    if config.chunk_overlap >= chunk_size:
        raise Error("invalid_processing", "重叠预算必须小于实际模型片段上限")
    nodes: list[BaseNode] = []
    for section in sections:
        parent = section.metadata.pop("header_path", None)
        if isinstance(parent, str):
            heading = re.match(r"^#+\s+(.+)", section.get_content())
            derived = (parent + (heading[1] if heading else "")).strip("/")
            if derived:
                section.metadata["section"] = derived
        prefix = ""
        if config.include_title:
            title = section.metadata.get("section")
            source = section.metadata.get("source")
            if not isinstance(title, str) or not title.strip():
                title = (
                    TypeAdapter(dict[str, object]).validate_python(source).get("name")
                    if isinstance(source, dict)
                    else None
                )
            if isinstance(title, str) and title.strip():
                section.metadata["indexed_title"] = title.strip()
                prefix = f"[标题] {title.strip()}\n\n"
        else:
            section.metadata.pop("indexed_title", None)
        table_context = section.metadata.pop("_table_context", None)
        table_row_key = section.metadata.pop("_table_row_key", None)
        if isinstance(table_context, str):
            remaining = chunk_size - len(tokenize(prefix)) - config.chunk_overlap
            table_prefix_budget = min(64, remaining // 3)
            if table_context and len(tokenize(table_context)) <= table_prefix_budget:
                prefix += table_context + "\n"
            if (
                isinstance(table_row_key, str)
                and table_row_key
                and len(tokenize(table_row_key)) <= table_prefix_budget
                and len(tokenize(prefix + table_row_key + "\n")) < chunk_size - config.chunk_overlap
            ):
                body = section.get_content().removeprefix(table_row_key + "\n")
                section = TextNode(text=body, metadata=section.metadata)
                prefix += table_row_key + "\n"
        section.excluded_embed_metadata_keys = list(section.metadata)
        section.excluded_llm_metadata_keys = list(section.metadata)
        body_budget = chunk_size - len(tokenize(prefix)) if prefix else chunk_size
        if config.chunk_overlap >= body_budget:
            raise Error(
                "invalid_processing", "标题占用后正文预算不足，请缩短标题、增大片段或减小重叠"
            )
        fragments = SentenceSplitter(
            chunk_size=body_budget, chunk_overlap=config.chunk_overlap, tokenizer=tokenize
        )([section])
        for fragment in fragments:
            if prefix:
                if not isinstance(fragment, TextNode):
                    raise Error("invalid_document", "切分器未返回可索引文本")
                fragment.set_content(prefix + fragment.get_content())
            if len(tokenize(fragment.get_content())) > chunk_size:
                raise Error("invalid_document", "切分结果超过实际分词器上限，未截断或丢弃正文")
            nodes.append(fragment)
    if not nodes or len(nodes) > 20000:
        raise Error("invalid_document", "正文未产生片段或片段超过 20000 个，请拆分资料")
    return nodes


def split_documents(documents: list[Document], config: ProcessingConfig) -> list[BaseNode]:
    """本地SDK复用共享切分，再校验其来源契约和确定性节点身份。"""
    return _identify_local_nodes(split_native_documents(documents, config))


def local_result_nodes(
    result: ParseResult,
    source: Provenance,
    config: ProcessingConfig,
    *,
    original_name: str | None = None,
) -> list[BaseNode]:
    """用与 Worker 相同的流程准备片段，再绑定本地快照所需的节点身份。"""
    parsed = ParseResult.model_validate_json(result.model_dump_json())
    nodes = prepare_result_nodes(
        parsed,
        config,
        partial(_local_document, source),
        original_name=original_name,
        section=source.section,
    )
    return _identify_local_nodes(nodes)


def _identify_local_nodes(nodes: list[BaseNode]) -> list[BaseNode]:
    """保持本地索引已用的确定性身份和严格来源形状。"""
    for ordinal, node in enumerate(nodes):
        origin = Provenance.model_validate(node.metadata)
        identity = f"{origin.resource_id}:{origin.resource_revision}:{origin.processing_revision}:{ordinal}:{node.get_content()}"
        node.id_ = hashlib.sha256(identity.encode()).hexdigest()
        node.metadata = origin.model_dump(mode="json")
        node.excluded_embed_metadata_keys = list(node.metadata)
        node.excluded_llm_metadata_keys = list(node.metadata)
    return nodes
