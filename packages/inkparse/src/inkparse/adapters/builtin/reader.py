"""内置读取器统一适配，不改变已有解析语义或自动下载模型。"""

import io
import mimetypes

from docling_core.types.doc.base import BoundingBox, CoordOrigin, Size
from docling_core.types.doc.common.reference import ProvenanceItem
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.labels import DocItemLabel
from inkstore import AssetStorage
from pypdf import PdfReader

from ...models import AssetBinding, ElementOrigin, FileFormat, ParseError, Warning
from ...structure import StructureBuilder
from .html import read_html
from .office import read_docx, read_pptx
from .structured import read_csv, read_json, read_xml


def read_document(
    data: bytes, name: str, format_name: FileFormat, assets: AssetStorage | None
) -> tuple[DoclingDocument, list[AssetBinding], list[ElementOrigin], list[Warning]]:
    """将冻结输入转为统一文档；资产仅进入调用方注入仓库，不管理业务任务。"""
    document = DoclingDocument(name=name)
    builder = StructureBuilder(document, assets)
    if format_name == "pdf":
        read_pdf(data, document, builder.warnings, builder.bindings, assets)
    elif format_name in {"text", "markdown"}:
        text = data.decode("utf-8-sig")
        if text.strip():
            document.add_text(label=DocItemLabel.TEXT, text=text)
        if format_name == "markdown":
            builder.warning("raw_markdown", "保留Markdown原文，未重建结构层级")
    elif format_name == "html":
        read_html(data, builder)
    elif format_name == "json":
        read_json(data, builder)
    elif format_name == "xml":
        read_xml(data, builder)
    elif format_name == "csv":
        read_csv(data, builder)
    elif format_name == "docx":
        read_docx(data, builder)
    elif format_name == "pptx":
        read_pptx(data, builder)
    else:
        raise ParseError("unsupported_format", "内置解析不支持该文件格式，请选择适用的解析引擎")
    return document, builder.bindings, builder.origins, builder.warnings


def read_pdf(
    data: bytes,
    document: DoclingDocument,
    warnings: list[Warning],
    bindings: list[AssetBinding],
    assets: AssetStorage | None,
) -> None:
    """通过pypdf逐物理页读文本，保留空白页编号并显式列出视觉内容缺口。"""
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ParseError(
            "encrypted_pdf",
            "当前解析器不支持加密PDF",
            suggestion="请提供已获授权解密的PDF后替换原件。",
        )
    warnings.append(
        Warning(
            code="pdf_text_only", message="仅提取文本；表格、图片与复杂阅读顺序未重建，区域为整页"
        )
    )
    for number, page in enumerate(reader.pages, 1):
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        document.add_page(page_no=number, size=Size(width=width, height=height))
        text = page.extract_text() or ""
        if page.images:
            if not text.strip():
                raise ParseError(
                    "ocr_required",
                    f"PDF第{number}页需要OCR",
                    location=f"第{number}页",
                    suggestion="当前未启用OCR；请上传具有文本层的PDF。",
                )
            if assets is None:
                warnings.append(
                    Warning(
                        code="images_not_extracted",
                        message="未配置资产仓库，未提取图片内容",
                        page=number,
                    )
                )
            else:
                for image in page.images:
                    asset = assets.put_bytes(image.data)
                    item = document.add_picture()
                    bindings.append(
                        AssetBinding(
                            asset_id=asset.asset_id,
                            element_ref=item.self_ref,
                            role="image",
                            page=number,
                            media_type=mimetypes.guess_type(image.name)[0]
                            or "application/octet-stream",
                        )
                    )
                warnings.append(
                    Warning(
                        code="image_page_only",
                        message="保存嵌入位图并关联物理页，未恢复图片区域/矢量图或理解图片",
                        page=number,
                    )
                )
        if text.strip():
            document.add_text(
                label=DocItemLabel.TEXT,
                text=text,
                prov=ProvenanceItem(
                    page_no=number,
                    bbox=BoundingBox(l=0, t=0, r=width, b=height, coord_origin=CoordOrigin.TOPLEFT),
                    charspan=(0, len(text)),
                ),
            )
