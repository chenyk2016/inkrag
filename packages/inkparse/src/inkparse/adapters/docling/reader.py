"""调用Docling统一读取，负责离线配置、资源交接与安全结果边界。"""

import base64
import binascii
import io
import os
import warnings
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

from bs4 import BeautifulSoup
from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.backend_options import HTMLBackendOptions, MarkdownBackendOptions
from docling.datamodel.base_models import (
    ConversionStatus,
    DocumentStream,
    FormatToExtensions,
    InputFormat,
)
from docling.datamodel.pipeline_options import (
    OcrMode,
    PdfPipelineOptions,
    RapidOcrOptions,
    TableFormerMode,
    TableStructureOptions,
)
from docling.document_converter import (
    DocumentConverter,
    FormatOption,
    HTMLFormatOption,
    ImageFormatOption,
    MarkdownFormatOption,
    PdfFormatOption,
)
from docling_core.types.doc.document import DoclingDocument
from inkstore import AssetStorage
from PIL import Image, UnidentifiedImageError

from ...models import AssetBinding, ElementOrigin, FileFormat, ParseError, Warning
from .. import DOCLING_INPUT_FORMATS
from .models import models_ready

INPUT_FORMATS = {
    format_name: InputFormat[name] for format_name, name in DOCLING_INPUT_FORMATS.items()
}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


def read_document(
    data: bytes,
    name: str,
    format_name: FileFormat,
    assets: AssetStorage | None,
    *,
    ocr_enabled: bool = True,
    ocr_mode: Literal["auto", "full_page"] = "auto",
    table_structure: bool = True,
    table_mode: Literal["fast", "accurate"] = "accurate",
) -> tuple[DoclingDocument, list[AssetBinding], list[ElementOrigin], list[Warning]]:
    """转换冻结输入并移交图片；失败不返回部分成功，不联网或选择资产后端。

    PDF必须显式提供预先下载的模型目录。图片写入失败会向调用方传播，已写入但
    尚未被结果引用的资源由宿主清理；原件和完整结果提交仍由Parser负责。
    """
    if format_name in {"docx", "pptx", "xlsx"}:
        from ..builtin.office import checked_archive

        with checked_archive(data):
            pass
    if format_name == "image":
        validate_image(data)
    input_format = INPUT_FORMATS.get(format_name)
    if input_format is None:
        raise ParseError("unsupported_format", "Docling 不支持此文件格式")
    options: dict[InputFormat, FormatOption] = {
        InputFormat.HTML: HTMLFormatOption(
            backend_options=HTMLBackendOptions.model_validate(
                {
                    "fetch_images": False,
                    "render_page": False,
                    "enable_remote_fetch": False,
                    "enable_local_fetch": False,
                }
            )
        ),
        InputFormat.MD: MarkdownFormatOption(
            backend_options=MarkdownBackendOptions.model_validate(
                {"fetch_images": False, "enable_remote_fetch": False, "enable_local_fetch": False}
            )
        ),
    }
    if format_name in {"pdf", "image"}:
        pdf = pdf_option(
            ocr_enabled=ocr_enabled,
            ocr_mode=ocr_mode,
            table_structure=table_structure,
            table_mode=table_mode,
        )
        options[input_format] = (
            pdf
            if format_name == "pdf"
            else ImageFormatOption(pipeline_options=pdf.pipeline_options)
        )
    converter = DocumentConverter(allowed_formats=[input_format], format_options=options)
    # Docling 按流名称推断文本格式。展示名不能覆盖 Parser 已选择的格式。
    stream_name = "source." + FormatToExtensions[input_format][0]
    # 固定Docling版本仍读取自身已废弃OCR字体字段，仅隔离这一条上游弃用提示。
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="^deprecated$",
            category=DeprecationWarning,
            module=r"docling\.models\.stages\.ocr\.rapid_ocr_model",
        )
        result = converter.convert(
            DocumentStream(name=stream_name, stream=io.BytesIO(data)),
            raises_on_error=False,
            max_file_size=20 * 1024 * 1024,
            max_num_pages=20000,
        )
    if result.status != ConversionStatus.SUCCESS:
        raise ParseError(
            "parse_failed",
            "Docling未完整完成文档转换",
            suggestion="请检查文件是否损坏、资源预算或模型工件配置；旧结果保持不变。",
        )
    document = result.document
    document.name = name
    document.origin = None
    notes: list[Warning] = []
    if format_name == "csv":
        notes.append(
            Warning(
                code="docling_csv_inference",
                message="Docling CSV会探测分隔符并将首行作为表头；需要严格逗号与无表头推断时请选择内置引擎",
            )
        )
    bindings: list[AssetBinding] = []
    origins: list[ElementOrigin] = []
    resources = original_images(data, format_name)
    html_sources = html_image_sources(data) if format_name == "html" else []
    for item in [*document.texts, *document.tables, *document.pictures]:
        locator = "docling:" + item.self_ref
        if item.prov:
            locator = f"page:{item.prov[0].page_no}/" + locator
        origins.append(ElementOrigin(element_ref=item.self_ref, locator=locator))
    if format_name == "html" and len(html_sources) != len(document.pictures):
        raise ParseError(
            "unsupported_html_structure", "HTML图片无法与Docling元素完整映射，拒绝错误绑定资源"
        )
    for ordinal, picture in enumerate(document.pictures):
        page = picture.prov[0].page_no if picture.prov else None
        raw: bytes | None = None
        if ordinal < len(html_sources):
            source = html_sources[ordinal]
            raw = inline_image(source)
            if raw is None:
                origin_index = len(document.texts) + len(document.tables) + ordinal
                origins[origin_index] = origins[origin_index].model_copy(
                    update={"resource_uri": source[:4096]}
                )
                notes.append(
                    Warning(
                        code="external_image_not_fetched",
                        message="图片外链仅记录，不访问网络或本地附件",
                        page=page,
                    )
                )
        image = picture.get_image(document)
        if raw is None and image is not None:
            raw = preserve_image_bytes(image, resources)
        if raw is not None:
            validate_image(raw)
            if assets is None:
                notes.append(
                    Warning(
                        code="images_not_extracted",
                        message="未配置资产仓库，图片字节未保存",
                        page=page,
                    )
                )
            else:
                asset = assets.put_bytes(raw)
                with Image.open(io.BytesIO(raw)) as decoded:
                    media_type = Image.MIME.get(decoded.format or "", "application/octet-stream")
                bindings.append(
                    AssetBinding(
                        asset_id=asset.asset_id,
                        element_ref=picture.self_ref,
                        role="image",
                        page=page,
                        media_type=media_type,
                    )
                )
        elif not html_sources:
            notes.append(
                Warning(
                    code="image_content_unavailable",
                    message="Docling未提供此图片字节；保留图片元素及原件",
                    page=page,
                )
            )
        picture.image = None
    for table in document.tables:
        table.image = None
    for page_item in document.pages.values():
        page_item.image = None
    if format_name in {"docx", "html", "markdown"}:
        notes.append(
            Warning(
                code="logical_origin_only",
                message="来源使用Docling元素引用；未伪造原件页码或DOM/OOXML路径",
            )
        )
    if format_name in {"pdf", "image"}:
        notes.append(
            Warning(
                code="docling_model_extraction",
                message=(
                    "经本地 Docling 布局处理；"
                    + ("OCR 已开启；" if ocr_enabled else "OCR 已关闭，图片文字可能缺失；")
                    + ("表格结构识别已开启；" if table_structure else "表格结构识别已关闭；")
                    + "模型结果仍需审校"
                ),
            )
        )
    return document, bindings, origins, notes


def pdf_option(
    *,
    ocr_enabled: bool = True,
    ocr_mode: Literal["auto", "full_page"] = "auto",
    table_structure: bool = True,
    table_mode: Literal["fast", "accurate"] = "accurate",
) -> PdfFormatOption:
    """只接受显式离线模型目录，禁止解析时触发模型下载或远程推理。"""
    configured = os.environ.get("INKPARSE_MODELS_PATH")
    if not models_ready(configured) or configured is None:
        raise ParseError(
            "models_unavailable",
            "PDF解析需要预先配置本地Docling模型目录",
            suggestion="下载模型工件后设置INKPARSE_MODELS_PATH，再重新解析。",
        )
    return PdfFormatOption(
        pipeline_options=PdfPipelineOptions(
            artifacts_path=Path(configured),
            enable_remote_services=False,
            allow_external_plugins=False,
            accelerator_options=AcceleratorOptions(num_threads=1, device="cpu"),
            generate_picture_images=True,
            ocr_options=RapidOcrOptions(
                backend="onnxruntime",
                lang=["ch"],
                mode=OcrMode.FULL_PAGE if ocr_mode == "full_page" else OcrMode.DEFAULT,
            ),
            do_ocr=ocr_enabled,
            do_table_structure=table_structure,
            table_structure_options=TableStructureOptions(
                mode=TableFormerMode.FAST if table_mode == "fast" else TableFormerMode.ACCURATE
            ),
        )
    )


def html_image_sources(data: bytes) -> list[str]:
    """只读静态图片来源，不执行HTML、脚本或抓取附件。"""
    soup = BeautifulSoup(data.decode("utf-8-sig"), "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "template", "head"]):
        tag.decompose()
    return [
        value if isinstance(value := tag.get("src"), str) else "" for tag in soup.find_all("img")
    ]


def inline_image(source: str) -> bytes | None:
    """解码受限base64位图，其他来源始终不读取。"""
    if not source.startswith("data:"):
        return None
    header, separator, payload = source.partition(",")
    if not separator or header.lower() not in {
        "data:image/png;base64",
        "data:image/jpeg;base64",
        "data:image/webp;base64",
        "data:image/gif;base64",
        "data:image/bmp;base64",
        "data:image/tiff;base64",
    }:
        raise ParseError("invalid_asset", "不支持的内嵌图片类型")
    if len(payload) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise ParseError("resource_budget_exceeded", "内嵌图片超过20MiB预算")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        raise ParseError("invalid_asset", "内嵌图片base64无效") from None
    validate_image(raw)
    return raw


def validate_image(data: bytes) -> None:
    """完整验证位图和像素预算，不将任意附件伪装为图片。"""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ParseError("resource_budget_exceeded", "图片为空或超过20MiB预算")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ParseError("resource_budget_exceeded", "图片超过4000万像素预算")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, ValueError, UnidentifiedImageError):
        raise ParseError("invalid_asset", "图片内容无效") from None


def original_images(data: bytes, format_name: FileFormat) -> list[bytes]:
    """提取已预检Office容器中的原始位图，用于保留字节身份，不负责读取正文。"""
    if format_name not in {"docx", "pptx"}:
        return []
    prefix = "word/media/" if format_name == "docx" else "ppt/media/"
    result: list[bytes] = []
    with ZipFile(io.BytesIO(data)) as archive:
        for entry in archive.infolist():
            if entry.filename.startswith(prefix) and not entry.is_dir():
                raw = archive.read(entry)
                try:
                    validate_image(raw)
                except ParseError:
                    continue
                result.append(raw)
    return result


def preserve_image_bytes(image: Image.Image, originals: list[bytes]) -> bytes:
    """像素完全匹配时复用原图字节，否则保存Docling生成的PNG衍生图。"""
    target = image.convert("RGBA")
    for raw in originals:
        with Image.open(io.BytesIO(raw)) as candidate:
            if (
                candidate.size == image.size
                and candidate.convert("RGBA").tobytes() == target.tobytes()
            ):
                return raw
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
