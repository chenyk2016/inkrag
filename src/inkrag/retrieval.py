"""检索输入和路由规则，独立于搜索引擎、权限范围及结果融合。"""

import math
from dataclasses import dataclass

from pydantic import TypeAdapter

from .lexical import LexicalAnalyzer, LexicalInput, LexicalTerms
from .models import Error, Query, RetrievalMode, TopK

_QUERY = TypeAdapter(Query)
_TOP_K = TypeAdapter(TopK)
_MODE = TypeAdapter(RetrievalMode)


@dataclass(frozen=True)
class RetrievalQuery:
    """已经校验的原始问题、返回数量和请求模式。"""

    text: str
    top_k: int
    mode: RetrievalMode

    def keywords(self, analyzer: LexicalAnalyzer) -> LexicalInput:
        """用绑定到索引版本的分析器生成关键词输入。"""
        return analyzer.analyze(self.text, query=True)


def validate_query(query: str, top_k: int, mode: RetrievalMode) -> RetrievalQuery:
    """在检索入口校验参数，保留后续向量计算所需的原问题。"""
    return RetrievalQuery(
        _QUERY.validate_python(query, strict=True),
        _TOP_K.validate_python(top_k, strict=True),
        _MODE.validate_python(mode, strict=True),
    )


def retrieval_mode(query: RetrievalQuery, keywords: LexicalInput) -> RetrievalMode:
    """关键词或混合请求的纯编号走精确匹配，显式向量模式保留其语义。"""
    if query.mode == "hybrid" and isinstance(keywords, LexicalTerms) and keywords.exact_identifier:
        return "keyword"
    return query.mode


def check_vector(vector: list[float], dimensions: int | None) -> None:
    """在入库及查询边界使用同一向量契约，避免错误向量产生伪命中。"""
    if (
        not vector
        or (dimensions is not None and len(vector) != dimensions)
        or not all(math.isfinite(value) for value in vector)
        or not any(vector)
    ):
        raise Error("invalid_embedding", "模型向量为空、维度不符或数值不合法")
