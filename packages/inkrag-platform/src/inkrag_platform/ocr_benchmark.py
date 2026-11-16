"""OCR对照的透明锚点评分；不将稀疏标注冒称全文识别准确率。"""

import re
import unicodedata

from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.table.table_data import TableCell
from docling_core.types.doc.items.text import TextItem
from inkparse import ParseResult, effective_document
from pydantic import JsonValue


def obj(value: JsonValue) -> dict[str, JsonValue]:
    """严格收窄JSON对象，不将错误响应伪装为空字典。"""
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def rows(value: JsonValue) -> list[JsonValue]:
    """严格收窄列表。"""
    if not isinstance(value, list):
        raise ValueError("expected JSON list")
    return value


def string(value: JsonValue) -> str:
    """严格读取身份和文本。"""
    if not isinstance(value, str):
        raise ValueError("expected JSON string")
    return value


def normalized(text: str) -> str:
    """统一宽度和空白，保留大小写、标点和数值，避免宽松匹配掩盖错误。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def contains(text: str, expected: str, kind: str = "text") -> bool:
    """数值锚点禁止匹配较长数字的子串；文本仅作关键证据覆盖检查。"""
    actual, gold = normalized(text), normalized(expected)
    if not gold:
        raise ValueError("empty gold assertion")
    if kind == "number":
        return re.search(r"(?<![\d.])" + re.escape(gold) + r"(?![\d.])", actual) is not None
    return gold in actual


def score_case(case: dict[str, JsonValue], text: str) -> dict[str, JsonValue]:
    """逐锚点和阅读顺序评分；失败解析传空文本，仍保留全部分母。"""
    assertions: list[JsonValue] = []
    for item in rows(case.get("assertions", [])):
        gold = obj(item)
        assertions.append(
            {
                **gold,
                "passed": contains(text, string(gold["text"]), string(gold.get("kind", "text"))),
            }
        )
    order = [string(item) for item in rows(case.get("reading_order", []))]
    cursor = 0
    ordered: bool | None = None
    if order:
        ordered = True
        for item in order:
            position = normalized(text).find(normalized(item), cursor)
            if position < 0:
                ordered = False
                break
            cursor = position + len(normalized(item))
    return {
        "assertions": assertions,
        "passed": sum(obj(a)["passed"] is True for a in assertions),
        "total": len(assertions),
        "reading_order_passed": ordered,
        "characters": len(text),
    }


def score_tables(case: dict[str, JsonValue], document: DoclingDocument | None) -> list[JsonValue]:
    """检查已标注表格坐标和单元格内容，失败解析保留结构错误分母。"""
    results: list[JsonValue] = []
    for raw in rows(case.get("table_cells", [])):
        gold = obj(raw)
        table, row, col = gold["table"], gold["row"], gold["col"]
        if not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (table, row, col)
        ):
            raise ValueError("表格坐标必须为非负整数")
        if not isinstance(table, int) or not isinstance(row, int) or not isinstance(col, int):
            raise ValueError("表格坐标无效")
        actual = ""
        if document is not None and table < len(document.tables):
            actual = " ".join(
                cell.text
                for cell in document.tables[table].data.table_cells
                if cell.start_row_offset_idx <= row < cell.end_row_offset_idx
                and cell.start_col_offset_idx <= col < cell.end_col_offset_idx
            )
        results.append(
            {
                **gold,
                "actual": actual,
                "passed": normalized(actual) == normalized(string(gold["text"])),
            }
        )
    return results


def summarize(cases: list[JsonValue], queries: list[JsonValue]) -> dict[str, JsonValue]:
    """固定全样本分母汇总，并把数值、结构和运行失败单列。"""
    anchors = [obj(a) for case in cases for a in rows(obj(case)["assertions"])]
    numbers = [a for a in anchors if a.get("kind") == "number"]
    cells = [obj(c) for case in cases for c in rows(obj(case)["table_cells"])]
    orders = [
        obj(case)["reading_order_passed"]
        for case in cases
        if obj(case)["reading_order_passed"] is not None
    ]
    reciprocal_ranks = [obj(q)["reciprocal_rank"] for q in queries]
    ranks: list[float] = []
    for rank in reciprocal_ranks:
        if not isinstance(rank, (float, int)):
            raise ValueError("非法排名指标")
        ranks.append(float(rank))
    return {
        "documents_total": len(cases),
        "parse_succeeded": sum(obj(c)["status"] == "succeeded" for c in cases),
        "searchable_succeeded": sum(obj(c).get("searchable") is True for c in cases),
        "anchors_total": len(anchors),
        "anchors_passed": sum(a["passed"] is True for a in anchors),
        "numeric_anchors_total": len(numbers),
        "numeric_anchors_passed": sum(a["passed"] is True for a in numbers),
        "table_cells_total": len(cells),
        "table_cells_passed": sum(c["passed"] is True for c in cells),
        "reading_orders_total": len(orders),
        "reading_orders_passed": sum(value is True for value in orders),
        "queries_total": len(queries),
        "queries_failed": sum(obj(q).get("status") == "failed" for q in queries),
        "hit_at_5": sum(obj(q)["hit_at_5"] is True for q in queries) / len(queries)
        if queries
        else None,
        "mrr": sum(ranks) / len(ranks) if ranks else None,
        "evidence_queries_total": sum(obj(q)["evidence_passed"] is not None for q in queries),
        "evidence_queries_passed": sum(obj(q)["evidence_passed"] is True for q in queries),
    }


def evidence_matches(hit_texts: list[str], alternatives: list[str]) -> bool | None:
    """任一正确资料片段包含任一期待同义证据即通过，不能跨片段拼凑文字。"""
    if not alternatives:
        return None
    return any(
        contains(
            text.casefold(),
            gold.casefold(),
            "number" if re.fullmatch(r"\d+(?:\.\d+)?", gold) else "text",
        )
        for text in hit_texts
        for gold in alternatives
    )


def infrastructure_invalid(status: JsonValue, error_code: JsonValue) -> bool:
    """只把明确内容空缺视为有效质量结果；未知、资源或运行故障不能归因OCR。"""
    if status == "succeeded":
        return False
    return not (
        status == "failed"
        and isinstance(error_code, str)
        and error_code in {"empty_document", "ocr_required"}
    )


def _header_path(cells: list[TableCell], column: int, before: int, path: list[str]) -> bool:
    """在同表同列上方验证有序表头路径，保留合并列跨度，不只按列号猜测。"""
    headers = sorted(
        (
            c
            for c in cells
            if c.start_col_offset_idx <= column < c.end_col_offset_idx
            and c.end_row_offset_idx <= before
        ),
        key=lambda c: c.start_row_offset_idx,
    )
    if any(normalized(c.text) == normalized(" ".join(path)) for c in headers):
        return True
    cursor = -1
    for label in path:
        found = next(
            (
                c
                for c in headers
                if c.start_row_offset_idx > cursor and normalized(c.text) == normalized(label)
            ),
            None,
        )
        if found is None:
            return False
        cursor = found.start_row_offset_idx
    return True


def score_structure(
    case_id: str, gold: dict[str, JsonValue], document: DoclingDocument | None
) -> list[dict[str, JsonValue]]:
    """逐条验证同一表内的行键、完整列头路径及交叉值；不借全文文本补结构缺口。

    跨行合并或按行展开都可满足关联；另报告原合并跨度是否覆盖全部金标行。
    找不到表、行键、父层列头或交叉值均保留失败分母，不使用结果反向调整金标。
    """
    checks: list[dict[str, JsonValue]] = []
    for raw in rows(gold["assertions"]):
        assertion = obj(raw)
        if case_id not in rows(assertion["case_ids"]):
            continue
        row_key = obj(assertion["row_key"])
        path = [string(value) for value in rows(assertion["column_header_path"])]
        candidates: list[JsonValue] = []
        if document is not None:
            for table_index, table in enumerate(document.tables):
                cells = table.data.table_cells
                keys = [
                    c
                    for c in cells
                    if normalized(c.text) == normalized(string(row_key["text"]))
                    and _header_path(
                        cells,
                        c.start_col_offset_idx,
                        c.start_row_offset_idx,
                        [string(row_key["column_header"])],
                    )
                ]
                for key in keys:
                    for column in range(table.data.num_cols):
                        if not _header_path(cells, column, key.start_row_offset_idx, path):
                            continue
                        values = [
                            c
                            for c in cells
                            if c.start_row_offset_idx
                            <= key.start_row_offset_idx
                            < c.end_row_offset_idx
                            and c.start_col_offset_idx <= column < c.end_col_offset_idx
                        ]
                        actual = " ".join(c.text for c in values)
                        merged_labels = [
                            string(value) for value in rows(assertion.get("merged_rows", []))
                        ]
                        merged_preserved: bool | None = None
                        if merged_labels:
                            related_rows = [
                                c.start_row_offset_idx
                                for c in cells
                                if normalized(c.text)
                                in {normalized(label) for label in merged_labels}
                                and c.start_col_offset_idx == key.start_col_offset_idx
                                and _header_path(
                                    cells,
                                    c.start_col_offset_idx,
                                    c.start_row_offset_idx,
                                    [string(row_key["column_header"])],
                                )
                            ]
                            merged_preserved = len(related_rows) == len(merged_labels) and any(
                                c.start_row_offset_idx <= min(related_rows)
                                and c.end_row_offset_idx > max(related_rows)
                                for c in values
                            )
                        candidates.append(
                            {
                                "table": table_index,
                                "row": key.start_row_offset_idx,
                                "column": column,
                                "actual": actual,
                                "passed": normalized(actual)
                                == normalized(string(assertion["expected_text"])),
                                "merged_span_preserved": merged_preserved,
                            }
                        )
        passed = any(obj(candidate)["passed"] is True for candidate in candidates)
        checks.append(
            {
                "id": assertion["id"],
                "case_id": case_id,
                "row_key": row_key,
                "column_header_path": assertion["column_header_path"],
                "expected_text": assertion["expected_text"],
                "passed": passed,
                "candidates": candidates,
                "failure_reason": None
                if passed
                else "no_matching_table_association"
                if not candidates
                else "intersection_text_mismatch",
            }
        )
    return checks


def has_searchable_content(result: ParseResult) -> bool:
    """按正式索引投影检查有效正文；图片资源成功保存不等于存在可检索文本。"""
    document = effective_document(result)
    for item, _level in document.iterate_items(
        included_content_layers={ContentLayer.BODY, ContentLayer.NOTES}
    ):
        if isinstance(item, TextItem) and item.text.strip():
            return True
        if isinstance(item, TableItem) and item.export_to_markdown(doc=document).strip():
            return True
    return False
