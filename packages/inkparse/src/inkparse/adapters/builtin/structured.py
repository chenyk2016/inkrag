"""使用标准库保留JSON键路径、XML层级与CSV单元格，不猜测业务语义。"""

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from decimal import Decimal

from docling_core.types.doc.items.table.table_data import TableCell
from pydantic import TypeAdapter

from ...models import ParseError
from ...structure import MAX_CELLS, MAX_ELEMENTS, StructureBuilder, table_cell


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝重复JSON键，避免解析器覆盖值后把数据缺失当作成功。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ParseError("duplicate_json_key", "JSON包含重复键，无法无损解析")
        result[key] = value
    return result


def read_json(data: bytes, builder: StructureBuilder) -> None:
    """逐叶保存RFC6901指针及JSON类型文字，空容器也保留，不合并同名字段。"""
    value: object = json.loads(
        data.decode("utf-8-sig"), object_pairs_hook=unique_object, parse_float=Decimal
    )

    def visit(item: object, pointer: str, depth: int) -> None:
        """按源对象顺序遍历，限制深度，正文保留路径与标量的JSON表示。"""
        if depth > 128:
            raise ParseError("structure_budget_exceeded", "JSON层级超过128")
        if isinstance(item, dict) and item:
            for key, child in (
                TypeAdapter(dict[str, object]).validate_python(item, strict=True).items()
            ):
                token = key.replace("~", "~0").replace("/", "~1")
                visit(child, pointer + "/" + token, depth + 1)
        elif isinstance(item, list) and item:
            for ordinal, child in enumerate(
                TypeAdapter(list[object]).validate_python(item, strict=True)
            ):
                visit(child, pointer + "/" + str(ordinal), depth + 1)
        else:
            text = (
                str(item)
                if isinstance(item, Decimal)
                else json.dumps(item, ensure_ascii=False, allow_nan=False)
            )
            builder.text(f"{pointer or '/'} = {text}", "json:" + pointer)

    visit(value, "", 0)


def read_xml(data: bytes, builder: StructureBuilder) -> None:
    """保留命名空间、属性、元素路径和混合文字；拒绝DTD和实体声明且不解析外链。"""
    text = data.decode("utf-8-sig")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ParseError("unsafe_xml", "XML不允许DTD或实体声明")
    root = ET.fromstring(text)
    visited = 0

    def visit(item: ET.Element, path: str, depth: int) -> None:
        """为每个同级同名元素保留一基序号，尾随文字有独立定位。"""
        nonlocal visited
        visited += 1
        if depth > 128 or visited > MAX_ELEMENTS:
            raise ParseError("structure_budget_exceeded", "XML层级或元素数量超过预算")
        for key, value in item.attrib.items():
            builder.text(f"{path}/@{key} = {value}", "xml:" + path + "/@" + key)
        if item.text and item.text.strip():
            builder.text(f"{path} = {item.text.strip()}", "xml:" + path + "/text()")
        if not len(item) and not item.attrib and not (item.text or "").strip():
            builder.text(f"{path} = <empty/>", "xml:" + path)
        ordinals: dict[str, int] = defaultdict(int)
        for child in item:
            ordinals[child.tag] += 1
            child_path = f"{path}/{child.tag}[{ordinals[child.tag]}]"
            visit(child, child_path, depth + 1)
            if child.tail and child.tail.strip():
                builder.text(
                    f"{child_path}/tail() = {child.tail.strip()}", "xml:" + child_path + "/tail()"
                )

    visit(root, f"/{root.tag}[1]", 0)


def read_csv(data: bytes, builder: StructureBuilder) -> None:
    """严格读取UTF-8逗号CSV，保留引号内换行和空单元格，不猜测表头与数据类型。"""
    rows = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
    cells: list[TableCell] = []
    width, height = 0, 0
    try:
        for row_number, row in enumerate(rows):
            if not row:
                row = [""]
            width, height = max(width, len(row)), row_number + 1
            if len(cells) + len(row) > MAX_CELLS or height * width > MAX_CELLS:
                raise ParseError("structure_budget_exceeded", "CSV超过100000个单元格预算")
            cells.extend(table_cell(value, row_number, column) for column, value in enumerate(row))
    except csv.Error:
        raise ParseError(
            "invalid_csv",
            "CSV语法或单元格大小无效",
            location=f"第{rows.line_num}行",
            suggestion="请检查引号、换行和单元格大小后重新保存为UTF-8逗号CSV。",
        ) from None
    builder.table(cells, height, width, "csv:rows[1:" + str(height) + "]")
    builder.warning("csv_comma_dialect", "CSV按UTF-8逗号分隔解析，未猜测表头、区域数字或其他分隔符")
