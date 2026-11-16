"""可携带离线交付包；只读取注入仓库内已绑定的内容摘要资源。"""

import hashlib
import io
import json
import os
import tempfile
import warnings
import zipfile
from pathlib import Path

from docling_core.types.doc.base import ImageRefMode, Size
from docling_core.types.doc.common.reference import ImageRef
from inkstore import AssetStorage
from PIL import Image, UnidentifiedImageError
from pydantic import JsonValue

from .editing import effective_document
from .io import render_markdown
from .models import ParseError, ParseResult

MAX_BUNDLE_BYTES = 128 * 1024 * 1024
IMAGE_TYPES = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "GIF": ("gif", "image/gif"),
    "WEBP": ("webp", "image/webp"),
    "BMP": ("bmp", "image/bmp"),
    "TIFF": ("tiff", "image/tiff"),
}
SOURCE_EXTENSIONS = {
    "text": "txt",
    "markdown": "md",
    "pdf": "pdf",
    "html": "html",
    "json": "json",
    "xml": "xml",
    "csv": "csv",
    "docx": "docx",
    "pptx": "pptx",
    "xlsx": "xlsx",
}


def export_bundle(
    result: ParseResult, storage: AssetStorage, *, include_original: bool = False
) -> bytes:
    """生成有界ZIP字节；大文件宿主应使用save_bundle避免保留完整响应副本。"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        _write_archive(archive, result, storage, include_original=include_original)
    if output.tell() > MAX_BUNDLE_BYTES:
        raise ParseError("output_limit", "压缩包超过128MiB")
    return output.getvalue()


def save_bundle(
    result: ParseResult, storage: AssetStorage, path: str | Path, *, include_original: bool = False
) -> None:
    """逐资源写临时ZIP后原子替换；失败清理临时文件，保留既有交付物。"""
    destination = Path(path)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = stream.name
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            _write_archive(archive, result, storage, include_original=include_original)
        if Path(temporary).stat().st_size > MAX_BUNDLE_BYTES:
            raise ParseError("output_limit", "压缩包超过128MiB")
        with Path(temporary).open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError:
        raise ParseError("write_failed", "离线包写入失败，原交付物保留") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _write_archive(
    archive: zipfile.ZipFile, result: ParseResult, storage: AssetStorage, *, include_original: bool
) -> None:
    """导出Markdown、完整JSON、图片与资源清单；原件需显式选择，缺资源整次失败。

    ZIP中的文件名只来自摘要和固定扩展名，不接受文档名称或外链作为路径。
    JSON保留所有原生来源及审校历史指针，manifest将资产身份映射到离线路径。
    """
    result = ParseResult.model_validate_json(result.model_dump_json())
    document = effective_document(result)
    for picture in document.pictures:
        picture.image = None
    files: dict[str, JsonValue] = {}
    total = 0

    def write_entry(name: str, data: bytes) -> None:
        """逐项核对总量并写入压缩流，不在内存积累全部图片。"""
        nonlocal total
        total += len(data)
        if total > MAX_BUNDLE_BYTES:
            raise ParseError("output_limit", "离线导出总量超过128MiB")
        archive.writestr(name, data)
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

    write_entry("document.json", result.model_dump_json().encode())
    resources: dict[str, JsonValue] = {}
    paths: dict[str, tuple[str, int, int, str]] = {}
    for binding in result.assets:
        if binding.asset_id not in paths:
            raw = storage.read(binding.asset_id)
            if (
                not raw
                or len(raw) > 20 * 1024 * 1024
                or hashlib.sha256(raw).hexdigest() != binding.asset_id
            ):
                raise ParseError("invalid_asset", "导出图片缺失、损坏或超过预算")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(raw)) as image:
                        extension, media_type = IMAGE_TYPES[image.format or ""]
                        width, height = image.size
                        if width * height > 40_000_000:
                            raise ParseError("resource_budget_exceeded", "导出图片像素超过4000万")
                        image.verify()
            except (
                UnidentifiedImageError,
                OSError,
                SyntaxError,
                KeyError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ):
                raise ParseError(
                    "invalid_asset",
                    "图片资源损坏或格式不受支持，请修复原件后重新解析再导出",
                    location=binding.element_ref,
                    suggestion="请在原编辑工具修复图片，替换原件并重新解析后再导出。",
                ) from None
            name = f"assets/{binding.asset_id}.{extension}"
            write_entry(name, raw)
            paths[binding.asset_id] = (name, width, height, media_type)
            resources[binding.asset_id] = {
                "path": name,
                "sha256": binding.asset_id,
                "media_type": media_type,
                "size_bytes": len(raw),
            }
        name, width, height, media_type = paths[binding.asset_id]
        if binding.role == "image" and binding.element_ref.startswith("#/pictures/"):
            picture = document.pictures[int(binding.element_ref.rsplit("/", 1)[1])]
            picture.image = ImageRef(
                mimetype=media_type, dpi=72, size=Size(width=width, height=height), uri=Path(name)
            )
    original: JsonValue = None
    if include_original:
        if result.source.asset_id is None:
            raise ParseError("missing_source_asset", "当前结果未绑定已保存原件")
        raw = storage.read(result.source.asset_id)
        if (
            len(raw) != result.source.size_bytes
            or hashlib.sha256(raw).hexdigest() != result.source.sha256
        ):
            raise ParseError("invalid_asset", "导出原件与解析来源不一致")
        if result.source.format == "image":
            try:
                with Image.open(io.BytesIO(raw)) as source_image:
                    extension = IMAGE_TYPES[source_image.format or ""][0]
                    if source_image.width * source_image.height > 40_000_000:
                        raise ParseError("resource_budget_exceeded", "原件图片超过像素预算")
                    source_image.verify()
            except (OSError, ValueError, SyntaxError, KeyError, Image.DecompressionBombError):
                raise ParseError("invalid_asset", "图片原件无效，不能导出含原件交付包") from None
        else:
            extension = SOURCE_EXTENSIONS[result.source.format]
        name = "original/source." + extension
        write_entry(name, raw)
        original = {
            "path": name,
            "sha256": result.source.sha256,
            "original_name": result.source.name,
        }
    markdown = render_markdown(result, document, image_mode=ImageRefMode.REFERENCED)
    write_entry("document.md", markdown.encode())
    manifest: dict[str, JsonValue] = {
        "schema_version": "inkparse.bundle.v1",
        "result_id": result.result_id,
        "source_sha256": result.source.sha256,
        "original": original,
        "resources": resources,
        "files": files,
        "external_resources": [
            origin.model_dump(mode="json")
            for origin in result.origins
            if origin.resource_uri is not None
        ],
        "notes": [
            "Markdown包含有效审校内容；完整JSON保留被排除块与来源。",
            "未提取的外链图片仅列出来源，不会自动下载。",
            "人工说明是用户输入，不代表执行OCR或图片理解。",
        ],
    }
    write_entry("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
