"""OpenSearch 关键词适配器，负责原生分析器、词项字段和匹配语法。"""

from pydantic import JsonValue

from .lexical import (
    IDENTIFIER_BOOST,
    LEGACY_LEXICAL,
    LexicalAnalyzer,
    LexicalInput,
    LexicalProfile,
    NativeLexicalText,
)
from .lexical_adapters import NativeAnalyzer, lexical_analyzer

_TERMS = "lexical_terms"
_IDENTIFIERS = "identifiers"


def opensearch_analyzer(profile: LexicalProfile) -> LexicalAnalyzer:
    """历史索引用原生 standard 分析器，显式版本使用共用规则。"""
    return lexical_analyzer(profile, legacy=NativeAnalyzer())


def keyword_mapping() -> dict[str, JsonValue]:
    """定义原文、预切词项和精确编号的后端映射。"""
    return {
        "text": {"type": "text", "analyzer": "standard"},
        _TERMS: {"type": "text", "analyzer": "whitespace"},
        _IDENTIFIERS: {"type": "keyword"},
    }


def keyword_fields(value: LexicalInput) -> dict[str, JsonValue]:
    """将分析结果转成补充索引字段；原文由节点写入始终保留。"""
    if isinstance(value, NativeLexicalText):
        return {}
    return {_TERMS: " ".join(value.terms), _IDENTIFIERS: list(value.identifiers)}


def terms_field_exists(profile: LexicalProfile) -> dict[str, JsonValue] | None:
    """预切版本必须有词项字段，空字段值同样代表分析完成。"""
    if profile == LEGACY_LEXICAL:
        return None
    return {"exists": {"field": _TERMS}}


def keyword_query(query: LexicalInput, scope: list[JsonValue]) -> dict[str, JsonValue]:
    """编译共享词项和编号约束，所有候选都须满足可信范围。"""
    if isinstance(query, NativeLexicalText):
        return {"bool": {"must": [{"match": {"text": query.text}}], "filter": scope}}
    if query.exact_identifier is not None:
        return {
            "bool": {
                "must": [{"term": {_IDENTIFIERS: query.exact_identifier}}],
                "filter": scope,
            }
        }
    matches: list[JsonValue] = []
    if query.terms:
        matches.append({"match": {_TERMS: " ".join(query.terms)}})
    if query.identifiers:
        matches.append(
            {
                "constant_score": {
                    "filter": {"terms": {_IDENTIFIERS: list(query.identifiers)}},
                    "boost": IDENTIFIER_BOOST,
                }
            }
        )
    if not matches:
        return {"match_none": {}}
    return {"bool": {"should": matches, "minimum_should_match": 1, "filter": scope}}
