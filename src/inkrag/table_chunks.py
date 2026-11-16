"""保留原生表格的行、列和合并关系，供检索切分使用。"""

import re
from collections.abc import Callable, Iterator

from docling_core.transforms.serializer.markdown import MarkdownDocSerializer
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.table.table_data import RichTableCell, TableCell

_FIELD_GROUP_CHARS = 180


def _field_groups(fields: list[str]) -> Iterator[list[str]]:
    """把长表行拆成少量相邻列组，减少无关单元格对召回的影响。"""
    group: list[str] = []
    size = 0
    for field in fields:
        if group and size + len(field) + 1 > _FIELD_GROUP_CHARS:
            yield group
            group = []
            size = 0
        group.append(field)
        size += len(field) + 1
    if group:
        yield group


def _leading_header_rows(grid: list[list[TableCell]], cell_text: Callable[[TableCell], str]) -> int:
    """只将连续的列头行用于标注值，避免把普通数据误当成后续列名。"""
    count = 0
    for row_number, row in enumerate(grid):
        started = [
            cell
            for column, cell in enumerate(row)
            if cell.start_row_offset_idx == row_number and cell.start_col_offset_idx == column
        ]
        marked = [cell for cell in started if cell.column_header]
        body_values = [
            cell
            for cell in started
            if not cell.column_header and cell.start_col_offset_idx > 0 and cell_text(cell)
        ]
        if not marked or (body_values and (row_number > 0 or marked[-1].start_col_offset_idx == 0)):
            break
        count += 1
    return count


def table_rows(
    item: TableItem, doc: DoclingDocument, section: str | None
) -> Iterator[tuple[str, str, str]]:
    """逐行返回单元格文本和来自原表的上下文，展开纵向合并格并保留多层列头。"""
    caption = " ".join(item.caption_text(doc).split())
    caption = re.split(r"(?<=[.!?。！？])\s+", caption, maxsplit=1)[0]
    context = f"[表题] {caption}" if caption else f"[章节] {section}" if section else ""
    serializer: MarkdownDocSerializer | None = None
    texts: dict[int, str] = {}

    def cell_text(cell: TableCell) -> str:
        """每个合并单元格只转写一次，复用富文本序列化器。"""
        nonlocal serializer
        if id(cell) not in texts:
            text = cell.text
            if isinstance(cell, RichTableCell):
                if serializer is None:
                    serializer = MarkdownDocSerializer(doc=doc)
                text = serializer.serialize(item=cell.ref.resolve(doc=doc)).text
            texts[id(cell)] = " ".join(text.split())
        return texts[id(cell)]

    grid = item.data.grid
    header_rows = _leading_header_rows(grid, cell_text)
    if not header_rows:
        text = item.export_to_markdown(doc=doc)
        if text.strip():
            yield text, "", ""
        return
    columns: list[str] = []
    for column in range(item.data.num_cols):
        names = dict.fromkeys(cell_text(row[column]) for row in grid[:header_rows])
        columns.append(" / ".join(name for name in names if name))
    headings_only: list[str] = []
    has_body = False
    for row_no, row in enumerate(grid):
        cells = [cell for column, cell in enumerate(row) if cell.start_col_offset_idx == column]
        if row_no < header_rows:
            headings = [cell_text(cell) for cell in cells if cell.start_row_offset_idx == row_no]
            if any(headings):
                headings_only.append(" | ".join(filter(None, headings)))
            continue
        fields: list[str] = []
        key = ""
        for cell in cells:
            value = cell_text(cell)
            if not value:
                continue
            headings = dict.fromkeys(columns[cell.start_col_offset_idx : cell.end_col_offset_idx])
            label = " / ".join(heading for heading in headings if heading)
            field = f"{label}: {value}" if label else value
            if cell.row_header or (not key and cell.start_col_offset_idx == 0 and label):
                key = field
            fields.append(field)
        if fields:
            has_body = True
            if key and len(fields) > 1:
                fields.remove(key)
                for group in _field_groups(fields):
                    yield "\n".join([key, *group]), context, key
            else:
                yield "\n".join(fields), context, ""
    if not has_body:
        for heading in headings_only:
            yield heading, context, ""
