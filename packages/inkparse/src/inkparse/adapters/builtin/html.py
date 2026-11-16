"""使用BeautifulSoup读取静态HTML正文、表格与嵌入图片，不执行脚本或抓取外链。"""

import base64
import binascii
import re

from bs4 import BeautifulSoup
from bs4.element import Comment, Doctype, NavigableString, Tag
from docling_core.types.doc.items.table.table_data import TableCell
from docling_core.types.doc.labels import DocItemLabel

from ...models import ParseError
from ...structure import MAX_CELLS, MAX_ELEMENTS, StructureBuilder, table_cell

BLOCKS = {"p", "li", "pre", "blockquote", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6"}
STRUCTURES = BLOCKS | {"table", "img"}


def html_locator(tag: Tag) -> str:
    """形成与DOM同名兄弟序号一致的逻辑路径，不暴露机器文件路径。"""
    parts: list[str] = []
    current: Tag | None = tag
    while current is not None and current.name != "[document]":
        ordinal = 1 + sum(
            1
            for sibling in current.previous_siblings
            if isinstance(sibling, Tag) and sibling.name == current.name
        )
        parts.append(f"{current.name}[{ordinal}]")
        current = current.parent
        if len(parts) > 128:
            raise ParseError("structure_budget_exceeded", "HTML层级超过128")
    return "html:/" + "/".join(reversed(parts))


def closest_structure(node: Tag | NavigableString) -> Tag | None:
    """找文字所属的最近语义块，避免嵌套列表重复计入父块。"""
    return next((parent for parent in node.parents if parent.name in STRUCTURES), None)


def attribute(tag: Tag, name: str) -> str:
    """只接受单值HTML属性，不把多值列表自动拼成资源地址。"""
    value = tag.get(name)
    return value if isinstance(value, str) else ""


def read_html(data: bytes, builder: StructureBuilder) -> None:
    """顺DOM顺序保留语义块与合并表格；布局CSS、脚本和视觉阅读顺序不作推断。"""
    soup = BeautifulSoup(data.decode("utf-8-sig"), "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "template", "head"]):
        tag.decompose()
    visited = 0
    for node in soup.descendants:
        visited += 1
        if visited > MAX_ELEMENTS * 8:
            raise ParseError("structure_budget_exceeded", "HTML节点超过解析预算")
        if isinstance(node, Tag):
            if node.name == "img":
                read_image(node, builder)
            elif node.name == "table" and node.find_parent("table") is None:
                read_table(node, builder)
            elif node.name in BLOCKS and node.find_parent("table") is None:
                strings = [
                    str(text)
                    for text in node.descendants
                    if isinstance(text, NavigableString)
                    and not isinstance(text, (Comment, Doctype))
                    and closest_structure(text) is node
                ]
                text = "".join(strings).strip()
                level = int(node.name[1]) if re.fullmatch(r"h[1-6]", node.name) else None
                builder.text(
                    text,
                    html_locator(node),
                    heading_level=level,
                    label=DocItemLabel.CODE if node.name == "pre" else DocItemLabel.TEXT,
                )
        elif isinstance(node, NavigableString) and not isinstance(node, (Comment, Doctype)):
            if closest_structure(node) is None and node.strip():
                parent = node.parent
                if parent is not None:
                    builder.text(str(node).strip(), html_locator(parent) + "/text()")
    builder.warning("html_dom_order", "按静态DOM语义顺序读取；未执行脚本、应用CSS布局或访问外链")


def read_image(tag: Tag, builder: StructureBuilder) -> None:
    """保存受控data URI图片，其他资源只记录原地址，不触发文件或网络访问。"""
    source, alt = attribute(tag, "src"), attribute(tag, "alt")
    if len(source) > 4096 and not source.startswith("data:"):
        raise ParseError("resource_budget_exceeded", "HTML图片地址超过4096字符")
    if source.startswith("data:"):
        header, separator, encoded = source.partition(",")
        media_type = header.removeprefix("data:").removesuffix(";base64")
        if (
            not separator
            or not header.endswith(";base64")
            or media_type
            not in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"}
        ):
            raise ParseError(
                "unsupported_image", "HTML只支持base64位图data URI，SVG和脚本内容未接入"
            )
        try:
            payload = base64.b64decode(encoded, validate=True)
        except binascii.Error:
            raise ParseError("invalid_image", "HTML嵌入图片base64无效") from None
        builder.image(payload, media_type, html_locator(tag), alt=alt)
    else:
        builder.image(
            None, "application/octet-stream", html_locator(tag), alt=alt, resource_uri=source
        )


def span_value(tag: Tag, name: str) -> int:
    """限制行列合并跨度；浏览器特有的rowspan=0语义明确拒绝。"""
    value = attribute(tag, name) or "1"
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 1000:
        raise ParseError("unsupported_table", "HTML表格合并跨度必须为1到1000")
    return int(value)


def read_table(tag: Tag, builder: StructureBuilder) -> None:
    """将显式rowspan/colspan映射到原生表格，歧义或嵌套表格拒绝部分成功。"""
    if tag.find("table") is not None:
        raise ParseError("unsupported_table", "当前不支持嵌套HTML表格")
    rows = [row for row in tag.find_all("tr") if row.find_parent("table") is tag]
    occupied: set[tuple[int, int]] = set()
    cells: list[TableCell] = []
    width = 0
    for row_number, row in enumerate(rows):
        column = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            while (row_number, column) in occupied:
                column += 1
            row_span, col_span = span_value(cell, "rowspan"), span_value(cell, "colspan")
            if row_number + row_span > len(rows) or (column + col_span) * len(rows) > MAX_CELLS:
                raise ParseError("unsupported_table", "HTML表格跨度越界或超过单元格预算")
            for r in range(row_number, row_number + row_span):
                for c in range(column, column + col_span):
                    if (r, c) in occupied:
                        raise ParseError("unsupported_table", "HTML表格合并区域重叠")
                    occupied.add((r, c))
            cells.append(
                table_cell(
                    cell.get_text(" ", strip=True),
                    row_number,
                    column,
                    rows=row_span,
                    columns=col_span,
                    header=cell.name == "th",
                )
            )
            column += col_span
            width = max(width, column)
    builder.table(cells, len(rows), width, html_locator(tag))
