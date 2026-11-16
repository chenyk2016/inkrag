"""成熟Office库的原生结构适配，保留表格跨度、图片资源及文档/幻灯片定位。"""

import io
import posixpath
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from docling_core.types.doc.base import BoundingBox, CoordOrigin, Size
from docling_core.types.doc.common.reference import ProvenanceItem
from docling_core.types.doc.items.table.table_data import TableCell
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pptx import Presentation
from pptx.shapes.autoshape import Shape
from pptx.shapes.base import BaseShape
from pptx.shapes.graphfrm import GraphicFrame
from pptx.shapes.group import GroupShape
from pptx.shapes.picture import Picture

from ...models import ParseError
from ...structure import MAX_CELLS, MAX_ELEMENTS, StructureBuilder, table_cell

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CONTENT = "{http://schemas.openxmlformats.org/package/2006/content-types}"


def checked_archive(data: bytes) -> ZipFile:
    """先限制ZIP解压预算、条目和加密，Office库仅接收通过预算的冻结字节。"""
    archive = ZipFile(io.BytesIO(data))
    entries = archive.infolist()
    if (
        len(entries) > 10000
        or sum(item.file_size for item in entries) > 100 * 1024 * 1024
        or any(item.file_size > 20 * 1024 * 1024 or item.flag_bits & 1 for item in entries)
        or len({item.filename for item in entries}) != len(entries)
    ):
        archive.close()
        raise ParseError("archive_budget_exceeded", "Office压缩包超过预算、条目重复或被加密")
    try:
        for entry in entries:
            if entry.filename.endswith((".xml", ".rels")):
                safe_xml(archive.read(entry))
    except Exception:
        archive.close()
        raise
    return archive


def safe_xml(data: bytes) -> ET.Element:
    """Office XML禁用DTD与实体声明，不能借关系文件读取宿主资源。"""
    text = data.decode("utf-8-sig")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ParseError("unsafe_xml", "Office XML不允许DTD或实体声明")
    root = ET.fromstring(text)
    stack, visited = [(root, 0)], 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if depth > 128 or visited > MAX_ELEMENTS * 8:
            raise ParseError("structure_budget_exceeded", "Office XML元素数量或层级超过预算")
        stack.extend((child, depth + 1) for child in item)
    return root


def read_docx(data: bytes, builder: StructureBuilder) -> None:
    """顺正文原生段落/表格读取；页码由Word渲染决定，因此只提供OOXML逻辑定位。"""
    with checked_archive(data) as archive:
        root = safe_xml(archive.read("word/document.xml"))
        for item in root.iter():
            if item.tag in {W + "ins", W + "del", W + "txbxContent", W + "altChunk"}:
                raise ParseError(
                    "unsupported_docx_structure", "DOCX含修订、文本框或嵌入文档，需先接受修订或转换"
                )
        document = Document(io.BytesIO(data))
        body = root.find(W + "body")
        if body is None:
            raise ParseError("invalid_office", "DOCX缺少正文")
        xml_blocks = [item for item in body if item.tag in {W + "p", W + "tbl"}]
        native_blocks = list(document.iter_inner_content())
        if len(native_blocks) != len(xml_blocks):
            raise ParseError("unsupported_docx_structure", "DOCX正文结构不能完整映射")
        relations, media_types = word_image_index(archive)
        for ordinal, (block, xml) in enumerate(zip(native_blocks, xml_blocks, strict=True), 1):
            locator = f"ooxml:word/document.xml/body/block[{ordinal}]"
            if isinstance(block, Paragraph):
                style = block.style
                heading = (
                    re.fullmatch(r"Heading\s*(\d+)", style.style_id) if style is not None else None
                )
                level = min(int(heading[1]), 100) if heading else None
                builder.text(block.text, locator, heading_level=level)
            else:
                read_word_table(block, xml, locator, builder)
            word_images(xml, locator, archive, relations, media_types, builder)
        builder.warning(
            "docx_logical_order", "DOCX按正文逻辑顺序读取，不推测Word渲染页码或文字坐标"
        )
        if any(
            re.fullmatch(r"word/(header\d+|footer\d+|footnotes|endnotes|comments)\.xml", name)
            for name in archive.namelist()
        ):
            builder.warning(
                "docx_auxiliary_parts",
                "当前正文适配未纳入页眉、页脚、脚注、尾注或批注，原件保留这些内容",
            )


def read_word_table(table: Table, xml: ET.Element, locator: str, builder: StructureBuilder) -> None:
    """以Office原生单元格文字和XML合并标记生成表格，不重复输出合并单元格。"""
    rows = xml.findall(W + "tr")
    columns = len(table.columns)
    if len(rows) * columns > MAX_CELLS:
        raise ParseError("structure_budget_exceeded", "DOCX表格超过单元格预算")
    cells: list[TableCell] = []
    active: dict[int, TableCell] = {}
    for row_number, row in enumerate(rows):
        if (
            row.find(".//" + W + "gridBefore") is not None
            or row.find(".//" + W + "gridAfter") is not None
        ):
            raise ParseError("unsupported_table", "DOCX非矩形缺省网格尚未接入")
        column, next_active = 0, dict[int, TableCell]()
        for cell_xml in row.findall(W + "tc"):
            if cell_xml.find(W + "tbl") is not None:
                raise ParseError("unsupported_table", "DOCX嵌套表格尚未接入")
            span_xml, merge = (
                cell_xml.find(".//" + W + "gridSpan"),
                cell_xml.find(".//" + W + "vMerge"),
            )
            span = int(span_xml.get(W + "val", "1")) if span_xml is not None else 1
            if not 1 <= span <= columns or column + span > columns:
                raise ParseError("unsupported_table", "DOCX表格合并范围无效")
            if merge is not None and merge.get(W + "val") != "restart":
                previous = active.get(column)
                if previous is None or previous.col_span != span:
                    raise ParseError("unsupported_table", "DOCX纵向合并缺少匹配的起始单元格")
                previous.row_span += 1
                previous.end_row_offset_idx += 1
                next_active[column] = previous
            else:
                text = "\n".join(
                    "".join(
                        node.text or ""
                        if node.tag == W + "t"
                        else "\t"
                        if node.tag == W + "tab"
                        else "\n"
                        for node in paragraph.iter()
                        if node.tag in {W + "t", W + "tab", W + "br", W + "cr"}
                    )
                    for paragraph in cell_xml.findall(W + "p")
                )
                cell = table_cell(text, row_number, column, columns=span)
                cells.append(cell)
                if merge is not None:
                    next_active[column] = cell
            column += span
        active = next_active
    builder.table(cells, len(rows), columns, locator)


def word_image_index(archive: ZipFile) -> tuple[dict[str, ET.Element], dict[str, str]]:
    """每份文档只读一次关系与媒体类型，避免段落数乘关系数的重复解析。"""
    relationship_path = "word/_rels/document.xml.rels"
    if relationship_path not in archive.namelist():
        return {}, {}
    relations = {
        item.get("Id", ""): item
        for item in safe_xml(archive.read(relationship_path))
        if item.tag == REL + "Relationship"
    }
    types = safe_xml(archive.read("[Content_Types].xml"))
    media_types = {
        item.get("Extension", "")
        if item.tag == CONTENT + "Default"
        else item.get("PartName", ""): item.get("ContentType", "application/octet-stream")
        for item in types
    }
    return relations, media_types


def word_images(
    xml: ET.Element,
    locator: str,
    archive: ZipFile,
    relations: dict[str, ET.Element],
    media_types: dict[str, str],
    builder: StructureBuilder,
) -> None:
    """通过文档关系定位实际嵌入图片；外链只记录，绝不打开宿主文件或网络。"""
    for ordinal, image in enumerate(xml.iter(A + "blip"), 1):
        relation_id = image.get(R + "embed") or image.get(R + "link") or ""
        relation = relations.get(relation_id)
        if relation is None:
            raise ParseError("invalid_office", "DOCX图片关系不存在")
        target = relation.get("Target", "")
        image_locator = locator + f"/image[{ordinal}]"
        if relation.get("TargetMode") == "External":
            builder.image(None, "application/octet-stream", image_locator, resource_uri=target)
        else:
            path = posixpath.normpath(
                target.lstrip("/") if target.startswith("/") else posixpath.join("word", target)
            )
            try:
                archive.getinfo(path)
            except KeyError:
                raise ParseError("invalid_office", "DOCX图片关系指向无效条目") from None
            media_type = media_types.get(
                "/" + path, media_types.get(path.rsplit(".", 1)[-1], "application/octet-stream")
            )
            builder.image(archive.read(path), media_type, image_locator)


def shape_provenance(shape: BaseShape, page: int, text: str = "") -> ProvenanceItem:
    """将原生幻灯片形状矩形由EMU换成点，矩形是形状范围而非文字识别框。"""
    return ProvenanceItem(
        page_no=page,
        bbox=BoundingBox(
            l=shape.left / 12700,
            t=shape.top / 12700,
            r=(shape.left + shape.width) / 12700,
            b=(shape.top + shape.height) / 12700,
            coord_origin=CoordOrigin.TOPLEFT,
        ),
        charspan=(0, len(text)),
    )


def read_pptx(data: bytes, builder: StructureBuilder) -> None:
    """读取原生文本、合并表格、图片和备注，保留幻灯片编号且不伪造图表理解。"""
    with checked_archive(data) as archive:
        root = safe_xml(archive.read("ppt/presentation.xml"))
        size = root.find("{http://schemas.openxmlformats.org/presentationml/2006/main}sldSz")
        if size is None:
            raise ParseError("invalid_office", "PPTX缺少幻灯片大小")
        width, height = int(size.get("cx", "0")) / 12700, int(size.get("cy", "0")) / 12700
        if width <= 0 or height <= 0:
            raise ParseError("invalid_office", "PPTX幻灯片大小无效")
        presentation = Presentation(io.BytesIO(data))
        for number, slide in enumerate(presentation.slides, 1):
            builder.document.add_page(
                page_no=number,
                size=Size(
                    width=width,
                    height=height,
                ),
            )
            locator = "ooxml:" + str(slide.part.partname).lstrip("/")
            for shape in slide.shapes:
                read_shape(shape, number, locator, builder)
            if slide.has_notes_slide:
                frame = slide.notes_slide.notes_text_frame
                if frame is not None:
                    builder.text(
                        frame.text,
                        locator + "/notes",
                        prov=ProvenanceItem(
                            page_no=number,
                            bbox=BoundingBox(
                                l=0, t=0, r=width, b=height, coord_origin=CoordOrigin.TOPLEFT
                            ),
                            charspan=(0, len(frame.text)),
                        ),
                    )
        builder.warning(
            "pptx_shape_order",
            "幻灯片按原生形状顺序读取，保留形状范围；未推断视觉阅读顺序、连线关系或图片含义",
        )


def read_shape(
    shape: BaseShape,
    page: int,
    parent_locator: str,
    builder: StructureBuilder,
    group_prov: ProvenanceItem | None = None,
) -> None:
    """递归读取组合形状，组内坐标使用组范围并明确提示，不把局部坐标冒充页面坐标。"""
    locator = parent_locator + f"/shape[{shape.shape_id}]"
    prov = group_prov or shape_provenance(shape, page)
    if isinstance(shape, GroupShape):
        builder.warning(
            "pptx_group_bounds", "组合形状内的内容定位到组合范围，未重建每个子形状的坐标变换", page
        )
        for child in shape.shapes:
            read_shape(child, page, locator, builder, prov)
    elif isinstance(shape, Picture):
        image = shape.image
        builder.image(image.blob, image.content_type, locator, prov=prov)
    elif isinstance(shape, GraphicFrame) and shape.has_table:
        table = shape.table
        height, width = len(table.rows), len(table.columns)
        if height * width > MAX_CELLS:
            raise ParseError("structure_budget_exceeded", "幻灯片表格超过单元格预算")
        cells = [
            table_cell(cell.text, r, c, rows=cell.span_height, columns=cell.span_width)
            for r in range(height)
            for c in range(width)
            if not (cell := table.cell(r, c)).is_spanned
        ]
        builder.table(cells, height, width, locator, prov)
    elif isinstance(shape, Shape) and shape.has_text_frame:
        text = shape.text_frame.text
        text_prov = prov.model_copy(update={"charspan": (0, len(text))})
        builder.text(text, locator, prov=text_prov)
    else:
        builder.warning(
            "pptx_visual_not_interpreted",
            "存在图表、连线、SmartArt、媒体或其他视觉形状；已保留原件，未将其伪装为文本理解",
            page,
        )
