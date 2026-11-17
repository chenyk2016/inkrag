"""将现有 InkParse JSON 接到独立的检索核心。"""

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .document import ParsedDocument, Part

if TYPE_CHECKING:
    from inkparse import ParseResult


class InkparseAdapter:
    """按配置使用内置解析或 Docling，保留阅读顺序、源元素与页码。"""

    def __init__(
        self, *, engine: Literal["builtin", "docling"] = "builtin", ocr: bool | None = None
    ) -> None:
        """保存解析选择。Docling 权重由实际部署者装配。"""
        self.engine: Literal["builtin", "docling"] = engine
        self.ocr = ocr

    def parse(self, path: Path) -> ParsedDocument:
        """读取文件并转换为可直接入库的证据单元。"""
        from inkparse import FileSource, ParseOptions, Parser, ParseRequest

        result = Parser().parse(
            ParseRequest(
                source=FileSource(path=str(path)),
                options=ParseOptions(engine=self.engine, ocr_enabled=self.ocr),
            )
        )
        return _convert(result)

    def load(self, path: Path) -> ParsedDocument:
        """读取已解析或人工审校后保存的原生 InkParse JSON。"""
        from inkparse import load_result

        return _convert(load_result(path))


def _convert(result: "ParseResult") -> ParsedDocument:
    """按可见正文抽取文本与表格，保留可跳转的原件位置。"""
    from docling_core.types.doc.common.content_layer import ContentLayer
    from docling_core.types.doc.items.table.table import TableItem
    from docling_core.types.doc.items.text import TextItem
    from inkparse import effective_document

    document = effective_document(result)
    parts: list[Part] = []
    for item, _ in document.iterate_items(
        included_content_layers={ContentLayer.BODY, ContentLayer.NOTES}
    ):
        if isinstance(item, TableItem):
            text = item.export_to_markdown(doc=document)
        elif isinstance(item, TextItem):
            text = item.text
        else:
            continue
        if text.strip():
            parts.append(Part(text, item.self_ref, item.prov[0].page_no if item.prov else None))
    return ParsedDocument(result.source.name, tuple(parts), result.result_id, result.source.sha256)
