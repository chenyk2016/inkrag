"""对照评分反例：数字误识别、顺序错误和解析失败不可被指标隐藏。"""

from pathlib import Path

from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table_data import TableCell, TableData
from docling_core.types.doc.labels import DocItemLabel
from inkparse import FileSource, Parser, ParseRequest
from inkrag_platform.ocr_benchmark import (
    contains,
    evidence_matches,
    has_searchable_content,
    infrastructure_invalid,
    score_case,
    score_structure,
    score_tables,
)
from pydantic import JsonValue


def test_numeric_anchor_does_not_accept_longer_or_decimal_number() -> None:
    """30不能因为识别成130、30.5或3.30而被判成功。"""
    assert contains("保修３０ 天", "30", "number")
    assert not contains("130", "30", "number")
    assert not contains("30.5", "30", "number")
    assert not contains("3.30", "30", "number")


def test_failed_parse_preserves_all_anchor_and_order_denominators() -> None:
    """空正文仍产生完整失败锚点，不能只计算成功解析文件。"""
    case: dict[str, JsonValue] = {
        "assertions": [{"text": "合同", "kind": "text"}, {"text": "30", "kind": "number"}],
        "reading_order": ["合同", "期限"],
    }
    result = score_case(case, "")
    assert result["passed"] == 0
    assert result["total"] == 2
    assert result["reading_order_passed"] is False
    assert score_case(case, "期限 合同 30")["reading_order_passed"] is False
    assert score_case(case, "合同 期限 30")["reading_order_passed"] is True


def test_table_score_requires_correct_position_not_text_anywhere() -> None:
    """相同数字出现在错列不能通过单元格结构评分。"""
    document = DoclingDocument(name="table")
    document.add_table(
        label=DocItemLabel.TABLE,
        data=TableData(
            num_rows=1,
            num_cols=2,
            table_cells=[
                TableCell(
                    text="70.0",
                    start_row_offset_idx=0,
                    end_row_offset_idx=1,
                    start_col_offset_idx=0,
                    end_col_offset_idx=1,
                ),
                TableCell(
                    text="8.9",
                    start_row_offset_idx=0,
                    end_row_offset_idx=1,
                    start_col_offset_idx=1,
                    end_col_offset_idx=2,
                ),
            ],
        ),
    )
    case: dict[str, JsonValue] = {"table_cells": [{"table": 0, "row": 0, "col": 1, "text": "70.0"}]}
    result = score_tables(case, document)
    assert isinstance(result[0], dict) and result[0]["passed"] is False
    failed = score_tables(case, None)
    assert isinstance(failed[0], dict) and failed[0]["passed"] is False


def test_query_evidence_accepts_alternative_language_without_cross_hit_join() -> None:
    """同义中英文任一即可，数值误识别和跨片段拼接不通过。"""
    assert evidence_matches(["温度传感器异常"], ["温度传感器异常", "temperature sensor fault"])
    assert evidence_matches(["TEMPERATURE SENSOR FAULT"], ["temperature sensor fault"])
    assert not evidence_matches(["tempera", "ture sensor fault"], ["temperature sensor fault"])
    assert not evidence_matches(["170.0"], ["70.0"])


def _cell(text: str, row: int, col: int, *, rows: int = 1, cols: int = 1) -> TableCell:
    """构造明确跨度的测试单元格。"""
    return TableCell(
        text=text,
        start_row_offset_idx=row,
        end_row_offset_idx=row + rows,
        start_col_offset_idx=col,
        end_col_offset_idx=col + cols,
    )


def _structure_gold() -> dict[str, JsonValue]:
    """同表行列关联测试金标，不从待测结果推导。"""
    return {
        "assertions": [
            {
                "id": "a",
                "case_ids": ["sample"],
                "row_key": {"column_header": "class", "text": "Caption"},
                "column_header_path": ["Total", "Train"],
                "expected_text": "2.04",
            }
        ]
    }


def test_structure_requires_same_table_row_and_full_parent_header() -> None:
    """全局文本齐全或正确数值位于另一行，均不能弥补关联错误。"""
    gold = _structure_gold()
    document = DoclingDocument(name="association")
    document.add_table(
        label=DocItemLabel.TABLE,
        data=TableData(
            num_rows=4,
            num_cols=3,
            table_cells=[
                _cell("class", 0, 0, rows=2),
                _cell("Total", 0, 1, cols=2),
                _cell("Train", 1, 1),
                _cell("Test", 1, 2),
                _cell("Caption", 2, 0),
                _cell("2.04", 2, 1),
                _cell("1.77", 2, 2),
                _cell("Table", 3, 0),
                _cell("9.99", 3, 1),
            ],
        ),
    )
    assert score_structure("sample", gold, document)[0]["passed"] is True
    document.tables[0].data.table_cells[1].text = "Other"
    assert score_structure("sample", gold, document)[0]["passed"] is False
    document.tables[0].data.table_cells[1].text = "Total"
    document.tables[0].data.table_cells[5].text = "9.99"
    document.tables[0].data.table_cells[8].text = "2.04"
    assert score_structure("sample", gold, document)[0]["passed"] is False
    assert score_structure("sample", gold, None)[0]["passed"] is False


def test_merged_row_association_accepts_span_and_reports_expansion() -> None:
    """跨行跨度与按行展开均保留关联，但展开不能冒称保留原跨度。"""
    gold: dict[str, JsonValue] = {
        "assertions": [
            {
                "id": "merge",
                "case_ids": ["sample"],
                "row_key": {"column_header": "序号", "text": "2"},
                "column_header_path": ["任务"],
                "expected_text": "公开",
                "merged_rows": ["1", "2"],
            }
        ]
    }
    document = DoclingDocument(name="merged")
    document.add_table(
        label=DocItemLabel.TABLE,
        data=TableData(
            num_rows=3,
            num_cols=2,
            table_cells=[
                _cell("序号", 0, 0),
                _cell("任务", 0, 1),
                _cell("1", 1, 0),
                _cell("2", 2, 0),
                _cell("公开", 1, 1, rows=2),
            ],
        ),
    )
    first = score_structure("sample", gold, document)[0]
    assert first["passed"] is True
    candidates = first["candidates"]
    assert isinstance(candidates, list) and isinstance(candidates[0], dict)
    assert candidates[0]["merged_span_preserved"] is True
    document.tables[0].data.table_cells[4].end_row_offset_idx = 2
    document.tables[0].data.table_cells.append(_cell("公开", 2, 1))
    second = score_structure("sample", gold, document)[0]
    assert second["passed"] is True
    candidates = second["candidates"]
    assert isinstance(candidates, list) and isinstance(candidates[0], dict)
    assert candidates[0]["merged_span_preserved"] is False


def test_infrastructure_failure_is_not_ocr_quality_failure() -> None:
    """仅明确空内容可用于OCR效果对比，运行故障和未知错误使对照失效。"""
    assert not infrastructure_invalid("succeeded", None)
    assert not infrastructure_invalid("failed", "empty_document")
    assert infrastructure_invalid("failed", "timeout")
    assert infrastructure_invalid("failed", "models_unavailable")
    assert infrastructure_invalid("failed", "parse_failed")
    assert infrastructure_invalid("cancelled", None)


def test_picture_only_parse_is_not_searchable_but_text_or_table_is(tmp_path: Path) -> None:
    """合法图片资源不能进入纯文本索引，Markdown图片占位符不能算正文。"""
    source = tmp_path / "source.md"
    source.write_text("设备保修三年")
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    assert has_searchable_content(result)
    picture_only = DoclingDocument(name="picture")
    picture_only.add_picture()
    resource_result = result.model_copy(update={"document": picture_only})
    assert "image" in picture_only.export_to_markdown()
    assert not has_searchable_content(resource_result)
    picture_only.add_table(
        label=DocItemLabel.TABLE,
        data=TableData(num_rows=1, num_cols=1, table_cells=[_cell("三年", 0, 0)]),
    )
    assert has_searchable_content(resource_result)
