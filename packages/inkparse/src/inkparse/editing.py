"""类型化结构审校；稳定元素身份、原件与机器结果独立保存。"""

from typing import Annotated, Literal

from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.picture.picture import PictureItem
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.table.table_data import RichTableCell, TableCell
from docling_core.types.doc.items.text import TextItem
from docling_core.types.doc.labels import DocItemLabel
from pydantic import Field, TypeAdapter, ValidationError

from .models import EditorState, ParsedPayload, ParseError, ParseResult, StrictModel, fingerprint
from .structure import MAX_ELEMENTS


class TextOperation(StrictModel):
    """替换现有文字，空串表示清空此块，不删除其来源。"""

    kind: Literal["text"]
    element_ref: str = Field(pattern=r"^#/texts/[0-9]+$")
    text: str = Field(max_length=1024 * 1024)


class TableCellOperation(StrictModel):
    """按零基坐标修改原生格；合并格只能通过左上角地址修改。"""

    kind: Literal["table_cell"]
    element_ref: str = Field(pattern=r"^#/tables/[0-9]+$")
    row: int = Field(ge=0, le=100000)
    column: int = Field(ge=0, le=100000)
    text: str = Field(max_length=1024 * 1024)


class PictureCaptionOperation(StrictModel):
    """替换图片审校标题；原始机器文字留在前序不可变结果。"""

    kind: Literal["picture_caption"]
    element_ref: str = Field(pattern=r"^#/pictures/[0-9]+$")
    text: str = Field(max_length=1024 * 1024)


class PictureDescriptionOperation(StrictModel):
    """添加人工图片说明，明确作为人工正文保存，不宣称执行OCR或图片理解。"""

    kind: Literal["picture_description"]
    element_ref: str = Field(pattern=r"^#/pictures/[0-9]+$")
    text: str = Field(max_length=1024 * 1024)


class ExcludeOperation(StrictModel):
    """保留原始块和定位，只控制后续导出、检索消费的有效投影。"""

    kind: Literal["exclude"]
    element_ref: str = Field(pattern=r"^#/(texts|pictures|tables)/[0-9]+$")
    excluded: bool


class AppendTextOperation(StrictModel):
    """在文末人工补充文字或标题，不伪造原件位置和页码。"""

    kind: Literal["append_text"]
    text: str = Field(min_length=1, max_length=1024 * 1024)
    label: Literal["text", "section_header"] = "text"


StructureOperation = Annotated[
    TextOperation
    | TableCellOperation
    | PictureCaptionOperation
    | PictureDescriptionOperation
    | ExcludeOperation
    | AppendTextOperation,
    Field(discriminator="kind"),
]


def effective_document(result: ParseResult) -> DoclingDocument:
    """克隆有效结构树，保留原生数组和稳定引用，原结果及原始定位不变。

    被排除的块不再可从正文树遍历；数组不能重排，否则所有来源引用失效。
    消费者须用原生iterate_items遍历，不可直接遍历texts数组代替有效正文。
    """
    document = result.document.model_copy(deep=True)
    if result.editor is None:
        return document
    excluded = set(result.editor.excluded_refs)
    excluded.update(
        reference
        for picture, reference in result.editor.picture_description_refs.items()
        if picture in excluded
    )
    nodes = [
        document.body,
        *document.groups,
        *document.texts,
        *document.pictures,
        *document.tables,
    ]
    for node in nodes:
        node.children = [ref for ref in node.children if ref.cref not in excluded]
    for item in [*document.pictures, *document.tables]:
        item.captions = [ref for ref in item.captions if ref.cref not in excluded]
    for table in document.tables:
        for index, cell in enumerate(table.data.table_cells):
            if isinstance(cell, RichTableCell) and cell.ref.cref in excluded:
                # 直接引用被排除元素的格保留位置，避免导出器读到已断开的子树。
                table.data.table_cells[index] = _plain_cell(cell, "")
    return document


def _plain_cell(cell: RichTableCell, text: str) -> TableCell:
    """替换富格内容时保留单元格定位、表头标志及合并范围。"""
    return TableCell.model_validate({**cell.model_dump(exclude={"ref"}), "text": text})


def revise_structure(result: ParseResult, operations: list[StructureOperation]) -> ParseResult:
    """从已校验版本创建v4不可变审校版本；操作有界且原子，失败不修改输入。"""
    result = ParseResult.model_validate_json(result.model_dump_json())
    operations = TypeAdapter(list[StructureOperation]).validate_python(operations, strict=True)
    if not 1 <= len(operations) <= 1000:
        raise ParseError("invalid_revision", "每次支持1到1000项结构修订")
    document = result.document.model_copy(deep=True)
    texts = {item.self_ref: item for item in document.texts}
    tables = {item.self_ref: item for item in document.tables}
    pictures = {item.self_ref: item for item in document.pictures}
    references = set(texts) | set(tables) | set(pictures)
    state = result.editor or EditorState()
    excluded = set(state.excluded_refs)
    descriptions = dict(state.picture_descriptions)
    description_refs = dict(state.picture_description_refs)
    manual_refs = set(state.manual_refs)
    for operation in operations:
        if (
            not isinstance(operation, ExcludeOperation)
            and len(operation.text.encode()) > 1024 * 1024
        ):
            raise ParseError("invalid_revision", "单项修订超过1MiB")
        if isinstance(operation, AppendTextOperation):
            if not operation.text.strip():
                raise ParseError("invalid_revision", "人工补充不能为空白")
            if operation.label == "section_header":
                appended = document.add_heading(text=operation.text, level=2)
            else:
                appended = document.add_text(label=DocItemLabel.TEXT, text=operation.text, orig="")
            manual_refs.add(appended.self_ref)
        elif operation.element_ref not in references:
            raise ParseError("invalid_revision", "修订引用的元素不存在")
        elif isinstance(operation, TextOperation):
            texts[operation.element_ref].text = operation.text
            for picture, reference in description_refs.items():
                if reference == operation.element_ref:
                    descriptions[picture] = operation.text
        elif isinstance(operation, TableCellOperation):
            table = tables[operation.element_ref]
            for index, cell in enumerate(table.data.table_cells):
                if (
                    cell.start_row_offset_idx != operation.row
                    or cell.start_col_offset_idx != operation.column
                ):
                    continue
                if isinstance(cell, RichTableCell):
                    # 富文本格的原生导出读取子树。解除旧子树连接，保留元素编号及原件定位。
                    table.data.table_cells[index] = _plain_cell(cell, operation.text)
                    table.children = [ref for ref in table.children if ref.cref != cell.ref.cref]
                else:
                    cell.text = operation.text
                break
            else:
                raise ParseError("invalid_revision", "单元格不存在；合并格请使用左上角坐标")
        elif isinstance(operation, PictureCaptionOperation):
            picture = pictures[operation.element_ref]
            if picture.captions:
                caption = texts.get(picture.captions[0].cref)
                if caption is None:
                    raise ParseError("invalid_revision", "图片标题引用无效")
                caption.text = operation.text
                for ref in picture.captions[1:]:
                    excluded.add(ref.cref)
                picture.captions = picture.captions[:1]
            else:
                caption = document.add_text(
                    label=DocItemLabel.CAPTION, text=operation.text, orig="", parent=picture
                )
                picture.captions = [caption.get_ref()]
                texts[caption.self_ref] = caption
                manual_refs.add(caption.self_ref)
        elif isinstance(operation, PictureDescriptionOperation):
            if operation.element_ref in description_refs:
                texts[description_refs[operation.element_ref]].text = operation.text
            else:
                description = document.add_text(
                    label=DocItemLabel.TEXT, text=operation.text, orig=""
                )
                texts[description.self_ref] = description
                description_refs[operation.element_ref] = description.self_ref
                manual_refs.add(description.self_ref)
            descriptions[operation.element_ref] = operation.text
        else:
            if operation.excluded:
                excluded.add(operation.element_ref)
            else:
                excluded.discard(operation.element_ref)
    if len(document.texts) + len(document.tables) + len(document.pictures) > MAX_ELEMENTS:
        raise ParseError("invalid_revision", "修订后元素超过20000项")
    editor = EditorState(
        excluded_refs=sorted(excluded),
        picture_descriptions=descriptions,
        picture_description_refs=description_refs,
        manual_refs=sorted(manual_refs),
    )
    if document == result.document and editor == (result.editor or EditorState()):
        raise ParseError("invalid_revision", "没有实际修改")
    try:
        payload = ParsedPayload.model_validate(
            {
                **result.model_dump(exclude={"result_id"}),
                "schema_version": "inkparse.result.v4",
                "document": document,
                "editor": editor,
                "revision": result.revision + 1,
                "previous_result_id": result.result_id,
            }
        )
    except ValidationError:
        raise ParseError(
            "invalid_revision",
            "修订后结构超出安全约束或展开预算",
            suggestion="请减少单次文字量或合并表格格数；完整结果上限32MiB。",
        ) from None
    revised = ParseResult.model_validate(
        {**payload.model_dump(), "result_id": fingerprint(payload)}
    )
    if not any(
        isinstance(item, (PictureItem, TableItem))
        or (isinstance(item, TextItem) and item.text.strip())
        for item, _ in effective_document(revised).iterate_items()
    ):
        raise ParseError("invalid_revision", "请至少保留一个有效内容块")
    if len(revised.model_dump_json().encode()) > 32 * 1024 * 1024:
        raise ParseError("invalid_revision", "修订后的结果超过32MiB")
    return revised
