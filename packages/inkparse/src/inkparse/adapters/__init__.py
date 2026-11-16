"""解析工具的显式能力说明；新增工具在独立目录实现，不动态加载第三方代码。"""

from pathlib import Path

from ..models import FileFormat, ParseError
from ..models import ParserEngine as ParserEngine

BUILTIN_FORMATS = ("text", "markdown", "pdf", "html", "json", "xml", "csv", "docx", "pptx")
DOCLING_INPUT_FORMATS = {
    "pdf": "PDF",
    "docx": "DOCX",
    "pptx": "PPTX",
    "html": "HTML",
    "markdown": "MD",
    "csv": "CSV",
    "xlsx": "XLSX",
    "image": "IMAGE",
}
DOCLING_FORMATS = tuple(DOCLING_INPUT_FORMATS)


def check_engine_format(engine: ParserEngine, filename: str) -> FileFormat:
    """在读取/持久提交之前拒绝引擎不支持的扩展名，不自动切换解析工具。"""
    from ..parser import FORMATS

    format_name = FORMATS.get(Path(filename).suffix.lower())
    supported = BUILTIN_FORMATS if engine == "builtin" else DOCLING_FORMATS
    if format_name is None or format_name not in supported:
        raise ParseError(
            "unsupported_format",
            "所选解析工具不支持该文件格式",
            suggestion="重新导入时请选择界面为此文件提供的解析工具。",
        )
    return format_name
