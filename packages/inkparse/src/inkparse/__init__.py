"""独立文档解析入口，不依赖InkRAG、LlamaIndex或嵌入模型。"""

from .bundle import export_bundle, save_bundle
from .editing import (
    AppendTextOperation,
    ExcludeOperation,
    PictureCaptionOperation,
    PictureDescriptionOperation,
    StructureOperation,
    TableCellOperation,
    TextOperation,
    effective_document,
    revise_structure,
)
from .io import export_markdown, load_result, save_result
from .models import (
    AssetBinding,
    ElementOrigin,
    FileFormat,
    FileSource,
    ParseDiagnostic,
    ParseError,
    ParseOptions,
    ParserEngine,
    ParseRequest,
    ParseResult,
)
from .parser import Parser
from .persistence import copy_result, load_stored_result, save_stored_result
from .revision import revise_result

__all__ = [
    "AppendTextOperation",
    "AssetBinding",
    "ElementOrigin",
    "ExcludeOperation",
    "FileFormat",
    "FileSource",
    "ParseDiagnostic",
    "ParseError",
    "ParseOptions",
    "ParseRequest",
    "ParseResult",
    "Parser",
    "ParserEngine",
    "PictureCaptionOperation",
    "PictureDescriptionOperation",
    "StructureOperation",
    "TableCellOperation",
    "TextOperation",
    "copy_result",
    "effective_document",
    "export_bundle",
    "export_markdown",
    "load_result",
    "load_stored_result",
    "revise_result",
    "revise_structure",
    "save_bundle",
    "save_result",
    "save_stored_result",
]
