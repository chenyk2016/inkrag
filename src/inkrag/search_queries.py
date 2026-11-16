"""把已声明的业务字段和条件编译为受控OpenSearch查询，不接受任意DSL。"""

import math
import re
from datetime import datetime

from pydantic import JsonValue

from .models import Error
from .search_models import FieldType, Scalar, SearchFilter

RRF_PIPELINE = "inkrag-rrf-v1"


def rrf_pipeline(*, explain: bool = False) -> dict[str, JsonValue]:
    """同一份原生 RRF 配置服务正式查询和同响应诊断。"""
    pipeline: dict[str, JsonValue] = {
        "phase_results_processors": [
            {"score-ranker-processor": {"combination": {"technique": "rrf", "rank_constant": 60}}}
        ],
    }
    if explain:
        pipeline["response_processors"] = [{"hybrid_score_explanation": {}}]
    return pipeline


def vector_query(vector: list[float], top_k: int, scope: list[JsonValue]) -> dict[str, JsonValue]:
    """使用 OpenSearch 原生向量查询，在候选阶段执行可信过滤。"""
    json_vector: list[JsonValue] = [value for value in vector]
    return {
        "knn": {
            "embedding": {
                "vector": json_vector,
                "k": max(50, top_k * 5),
                "filter": {"bool": {"filter": scope}},
            }
        }
    }


def validate_schema(schema: dict[str, FieldType]) -> None:
    """拒绝路径字段、保留字段和过多字段，防止动态映射膨胀。"""
    if len(schema) > 100 or any(
        re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) is None for name in schema
    ):
        raise Error("invalid_fields", "字段名无效或超过100个")


def validate_value(value: JsonValue, field_type: FieldType) -> Scalar:
    """按实际字段类型校验标量，禁止bool冒充数字和非有限向量元数据。"""
    if field_type == "number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise Error("invalid_filter", "数值字段需要有限数字")
        return value
    if field_type == "boolean":
        if not isinstance(value, bool):
            raise Error("invalid_filter", "布尔字段需要boolean")
        return value
    if not isinstance(value, str) or len(value) > 512:
        raise Error("invalid_filter", "文字或日期字段需要最多512字符的字符串")
    if field_type == "date":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise Error("invalid_filter", "日期字段需要ISO8601值") from None
    return value


def validate_attributes(
    attributes: dict[str, Scalar], schema: dict[str, FieldType]
) -> dict[str, Scalar]:
    """禁止写入未声明字段，避免索引映射与产品字段规则分离。"""
    if set(attributes) - set(schema):
        raise Error("invalid_fields", "资料包含未定义字段")
    return {name: validate_value(value, schema[name]) for name, value in attributes.items()}


def scope_filters(tenant_id: str, knowledge_base_id: str) -> list[JsonValue]:
    """所有通道都附带可信范围，即使调用方传错物理索引也不能跨范围命中。"""
    return [
        {"term": {"tenant_id": tenant_id}},
        {"term": {"knowledge_base_id": knowledge_base_id}},
    ]


def compile_filters(
    tenant_id: str,
    knowledge_base_id: str,
    filters: list[SearchFilter],
    schema: dict[str, FieldType],
) -> list[JsonValue]:
    """把AND条件编译为Top-K之前的过滤；缺失字段不满足ne。"""
    if len(filters) > 20:
        raise Error("invalid_filter", "过滤条件最多20项")
    clauses = scope_filters(tenant_id, knowledge_base_id)
    for item in filters:
        if item.field not in schema:
            raise Error("invalid_filter", "过滤字段未定义")
        name, field_type = "attributes." + item.field, schema[item.field]
        if item.operator == "in":
            if not isinstance(item.value, list) or not 1 <= len(item.value) <= 50:
                raise Error("invalid_filter", "in条件需要1到50个值")
            values: list[JsonValue] = [validate_value(value, field_type) for value in item.value]
            clauses.append({"terms": {name: values}})
            continue
        value = validate_value(item.value, field_type)
        if item.operator == "eq":
            clauses.append({"term": {name: value}})
        elif item.operator == "ne":
            clauses.append(
                {
                    "bool": {
                        "filter": [{"exists": {"field": name}}],
                        "must_not": [{"term": {name: value}}],
                    }
                }
            )
        elif item.operator == "contains":
            if field_type != "text" or not isinstance(value, str):
                raise Error("invalid_filter", "contains只适用于文字字段")
            literal = value.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")
            clauses.append({"wildcard": {name: {"value": "*" + literal + "*"}}})
        else:
            if field_type not in {"number", "date"}:
                raise Error("invalid_filter", "范围条件只适用于数字或日期")
            clauses.append({"range": {name: {item.operator: value}}})
    return clauses
