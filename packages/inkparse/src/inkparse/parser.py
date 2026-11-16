"""统一同步解析入口，内置与Docling适配共享冻结输入和结果提交，文件只读取一次后以冻结字节处理。"""

import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from importlib.metadata import version
from pathlib import Path
from zipfile import BadZipFile

from inkstore import AssetStorage, StorageError
from pydantic import ValidationError
from pypdf.errors import PdfReadError

from .adapters import BUILTIN_FORMATS, DOCLING_FORMATS
from .adapters.builtin.reader import read_document as read_builtin
from .models import (
    EngineInfo,
    FileFormat,
    ParsedPayload,
    ParseError,
    ParseRequest,
    ParseResult,
    SourceInfo,
    fingerprint,
)
from .persistence import save_stored_result

FORMATS: dict[str, FileFormat] = {
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".pdf": "pdf",
    ".html": "html",
    ".htm": "html",
    ".json": "json",
    ".xml": "xml",
    ".csv": "csv",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    **{
        extension: "image"
        for extension in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
    },
}


class Parser:
    """独立解析核心；实例不持有连接，导入与构造均不下载模型或创建文件。"""

    def parse(self, request: ParseRequest, *, assets: AssetStorage | None = None) -> ParseResult:
        """按明确引擎解析冻结字节；默认内置保持原行为，Docling失败不回退。

        图片仅进入注入仓库，完整结果校验后才持久提交；失败可能留下未引用资产。
        Docling PDF/图片要求预置模型，输入工具选择和实际读取版本随结果保存。
        """
        request = ParseRequest.model_validate_json(request.model_dump_json())
        path = Path(request.source.path)
        try:
            if not path.is_file():
                raise ParseError("invalid_source", "资料路径不是可读取文件")
            with path.open("rb") as stream:
                data = stream.read(request.options.max_bytes + 1)
        except OSError:
            raise ParseError("invalid_source", "无法读取资料文件") from None
        if not data or len(data) > request.options.max_bytes:
            raise ParseError("invalid_source", "资料为空或超过读取预算")
        detected = FORMATS.get(path.suffix.lower())
        format_name = request.options.format if request.options.format != "auto" else detected
        if format_name is None:
            raise ParseError(
                "unsupported_format", "支持TXT、Markdown、文本PDF、HTML、JSON、XML、CSV、DOCX和PPTX"
            )
        supported = BUILTIN_FORMATS if request.options.engine == "builtin" else DOCLING_FORMATS
        if format_name not in supported:
            raise ParseError("unsupported_format", "所选解析工具不支持该文件格式")
        if request.options.engine == "builtin" and request.options.ocr_enabled is True:
            raise ParseError("unsupported_option", "内置解析工具不支持 OCR，请显式选择 Docling")
        options = request.options
        if any(
            value is not None
            for value in (options.ocr_mode, options.table_structure, options.table_mode)
        ) and (options.engine != "docling" or format_name not in {"pdf", "image"}):
            raise ParseError(
                "unsupported_option", "OCR 范围和表格识别参数仅适用于 Docling PDF/图片"
            )
        if options.ocr_enabled is False and options.ocr_mode is not None:
            raise ParseError("unsupported_option", "OCR 关闭时不能设置识别范围")
        if options.table_structure is False and options.table_mode is not None:
            raise ParseError("unsupported_option", "表格结构识别关闭时不能设置识别精度")
        source_asset = assets.put_bytes(data) if assets is not None else None
        display_name = request.source.name or path.name
        try:
            if request.options.engine == "builtin":
                document, bindings, origins, warnings = read_builtin(
                    data, display_name, format_name, assets
                )
            else:
                try:
                    from .adapters.docling.reader import read_document as read_docling
                except ModuleNotFoundError:
                    raise ParseError(
                        "engine_unavailable", "Docling解析依赖未安装，请安装inkparse[docling]"
                    ) from None

                document, bindings, origins, warnings = read_docling(
                    data,
                    display_name,
                    format_name,
                    assets,
                    ocr_enabled=request.options.ocr_enabled is not False,
                    ocr_mode=request.options.ocr_mode or "auto",
                    table_structure=request.options.table_structure is not False,
                    table_mode=request.options.table_mode or "accurate",
                )
        except (ParseError, StorageError):
            raise
        except UnicodeDecodeError as exc:
            raise ParseError(
                "parse_failed",
                "文本编码不是受支持的UTF-8",
                location=f"字节偏移 {exc.start}",
                suggestion="请将文件另存为UTF-8后替换原件。",
            ) from None
        except json.JSONDecodeError as exc:
            raise ParseError(
                "invalid_json",
                "JSON语法无效",
                location=f"第{exc.lineno}行，第{exc.colno}列",
                suggestion="请修复该位置附近的JSON语法后替换原件；直接重试不会改变输入。",
            ) from None
        except ET.ParseError as exc:
            line, column = exc.position
            raise ParseError(
                "invalid_xml",
                "XML语法无效",
                location=f"第{line}行，第{column + 1}列",
                suggestion="请检查标签闭合、属性引号或转义字符后替换原件。",
            ) from None
        except PdfReadError:
            raise ParseError(
                "invalid_pdf",
                "PDF结构损坏或无法读取",
                suggestion="请用原编辑工具重新导出PDF后替换原件。",
            ) from None
        except BadZipFile:
            raise ParseError(
                "invalid_office",
                "Office文件容器损坏或扩展名与内容不匹配",
                suggestion="请用Office重新另存为DOCX或PPTX后替换原件。",
            ) from None
        except Exception as exc:
            raise ParseError("parse_failed", "读取正文失败：" + type(exc).__name__) from None
        # OCR关闭时Docling可能只返回零维空表检测框；这不是预算超限或可检索内容。
        empty_tables = all(
            table.data.num_rows == 0 and table.data.num_cols == 0 and not table.data.table_cells
            for table in document.tables
        )
        if not document.texts and not document.pictures and empty_tables:
            raise ParseError(
                "empty_document",
                "没有读取到内容",
                suggestion="请检查原件是否为空；扫描件需要单独配置OCR能力。",
            )
        source = SourceInfo.model_validate(
            {
                "name": display_name,
                "format": format_name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "asset_id": source_asset.asset_id if source_asset is not None else None,
                "size_bytes": len(data),
            }
        )
        try:
            payload = ParsedPayload(
                source=source,
                options=request.options,
                document=document,
                warnings=warnings,
                assets=bindings,
                origins=origins,
                coverage="text_only"
                if format_name == "text"
                or (request.options.engine == "builtin" and format_name in {"markdown", "pdf"})
                else "native_structure",
                engine=engine_info(format_name, request.options.engine),
            )
        except ValidationError:
            raise ParseError(
                "result_budget_exceeded",
                "解析结构超过安全约束或展开预算",
                suggestion="请拆分资料；原生表格最多100000格，完整结果上限32MiB。",
            ) from None
        result = ParseResult.model_validate(
            {**payload.model_dump(), "result_id": fingerprint(payload)}
        )
        if len(result.model_dump_json().encode()) > 32 * 1024 * 1024:
            raise ParseError("result_budget_exceeded", "解析结果超过32MiB交换预算")
        if assets is not None:
            save_stored_result(result, assets)
        return result


def engine_info(format_name: FileFormat, engine: str = "builtin") -> EngineInfo:
    """记录实际执行的成熟读取器和版本，结构模型版本单独保存。"""
    if engine == "docling" and format_name not in {"text", "json", "xml"}:
        return EngineInfo(
            name="docling",
            version=version("docling-slim"),
            docling_core_version=version("docling-core"),
        )
    names = {"pdf": "pypdf", "html": "beautifulsoup4", "docx": "python-docx", "pptx": "python-pptx"}
    if format_name in names:
        name = names[format_name]
        return EngineInfo.model_validate(
            {
                "name": name,
                "version": version(name),
                "docling_core_version": version("docling-core"),
            }
        )
    name = "stdlib-" + format_name if format_name in {"json", "xml", "csv"} else "utf8"
    return EngineInfo.model_validate(
        {
            "name": name,
            "version": sys.version.split()[0] if name != "utf8" else "utf-8-sig",
            "docling_core_version": version("docling-core"),
        }
    )
