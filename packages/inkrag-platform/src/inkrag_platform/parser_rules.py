"""提交前核对高级解析参数适用范围，避免接受已知必失败的预览与替换任务。"""

from inkparse import ParseOptions
from inkparse.adapters import ParserEngine, check_engine_format
from pydantic import JsonValue

from .models import DomainError


def validate_parser_rules(engine: ParserEngine, filename: str, rules: dict[str, JsonValue]) -> None:
    """保留显式选项语义；跨格式不适用或父开关关闭时明确拒绝，不静默删参数。"""
    advanced = {
        key: value
        for key, value in rules.items()
        if key in {"ocr_mode", "table_mode", "table_structure"} and value is not None
    }
    if advanced and (
        engine != "docling" or check_engine_format(engine, filename) not in {"pdf", "image"}
    ):
        raise DomainError(
            "invalid_request",
            "已保存的OCR与表格高级参数仅适用于Docling PDF及图片；请重新导入并选择适用参数",
        )
    if rules.get("ocr_enabled") is False and "ocr_mode" in advanced:
        raise DomainError("invalid_request", "关闭OCR时不能设置OCR识别模式")
    if rules.get("table_structure") is False and "table_mode" in advanced:
        raise DomainError("invalid_request", "关闭表格结构识别时不能设置表格识别模式")
    ParseOptions.model_validate(
        {"engine": engine, "ocr_enabled": rules.get("ocr_enabled"), **advanced}
    )
