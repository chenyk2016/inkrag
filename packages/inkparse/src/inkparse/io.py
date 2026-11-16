"""严格读取和原子导出交换包；不读取交换内容中的任意本地附件路径。"""

import os
import tempfile
from pathlib import Path

from docling_core.types.doc.base import ImageRefMode
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.text import SectionHeaderItem, TextItem

from .editing import effective_document
from .models import ParseError, ParseResult

MAX_JSON_BYTES = 32 * 1024 * 1024


def load_result(path: str | Path) -> ParseResult:
    """从有大小上限的JSON读取已验证结果；损坏或未知版本不静默回退。"""
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise ParseError("invalid_result", "交换包超过32MiB")
        return ParseResult.model_validate_json(data)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("invalid_result", "交换包无效：" + type(exc).__name__) from None


def save_result(result: ParseResult, path: str | Path) -> None:
    """重新校验结果并原子保存JSON；失败不会把原文件截断成半份结果。"""
    validated = ParseResult.model_validate_json(result.model_dump_json())
    data = validated.model_dump_json(indent=2).encode()
    if len(data) > MAX_JSON_BYTES:
        raise ParseError("invalid_result", "交换包超过32MiB")
    atomic_write(Path(path), data)


def export_markdown(result: ParseResult, path: str | Path) -> None:
    """从同一已验证结构导出Markdown；原始Markdown直接保留语法。"""
    result = ParseResult.model_validate_json(result.model_dump_json())
    document = effective_document(result)
    text = render_markdown(result, document)
    atomic_write(Path(path), text.encode())


def render_markdown(
    result: ParseResult,
    document: DoclingDocument,
    *,
    image_mode: ImageRefMode = ImageRefMode.PLACEHOLDER,
) -> str:
    """共享普通导出与ZIP的有效正文，保留原始Markdown及人工标题层级。"""
    if result.source.format != "markdown" or result.engine.name == "docling":
        return document.export_to_markdown(
            image_mode=image_mode, included_content_layers={ContentLayer.BODY, ContentLayer.NOTES}
        )
    return "\n\n".join(
        "#" * item.level + " " + item.text if isinstance(item, SectionHeaderItem) else item.text
        for item, _ in document.iterate_items()
        if isinstance(item, TextItem)
    )


def atomic_write(path: Path, data: bytes) -> None:
    """在目标目录临时写入并fsync后替换；清理本次临时文件，不创建父目录。"""
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise ParseError("write_failed", "结果保存失败，原文件保留") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
