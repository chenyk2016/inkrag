"""编排本地快照的关键词、向量和混合检索，复用 LlamaIndex 召回及融合。"""

from pathlib import Path

from llama_index.core import VectorStoreIndex
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.llms.mock import MockLLM
from llama_index.core.retrievers import QueryFusionRetriever
from llama_index.core.retrievers.fusion_retriever import FUSION_MODES
from llama_index.core.schema import NodeWithScore, QueryBundle

from .models import HybridConfig
from .retrieval import RetrievalQuery, check_vector, retrieval_mode
from .sqlite_keyword import KEYWORD_INDEX, KeywordRetriever, keyword_profile, sqlite_analyzer


def _vector_bundle(
    query: RetrievalQuery, embedding: BaseEmbedding, dimensions: int | None
) -> QueryBundle:
    """向量模型接收原始问题，向量通过快照维度校验后进入召回。"""
    vector = embedding.get_query_embedding(query.text)
    check_vector(vector, dimensions)
    return QueryBundle(query_str=query.text, embedding=vector)


def retrieve_local(
    snapshot: Path,
    index: VectorStoreIndex,
    embedding: BaseEmbedding,
    dimensions: int | None,
    query: RetrievalQuery,
    hybrid_config: HybridConfig | None,
) -> list[NodeWithScore]:
    """从指定快照召回；仅需要关键词的请求无需调用向量模型。"""
    if query.mode == "vector":
        bundle = _vector_bundle(query, embedding, dimensions)
        return index.as_retriever(similarity_top_k=query.top_k).retrieve(bundle)

    path = snapshot / KEYWORD_INDEX
    profile = keyword_profile(path)
    keywords = query.keywords(sqlite_analyzer(profile))
    if retrieval_mode(query, keywords) == "keyword":
        return KeywordRetriever(path, index, query.text, keywords, profile, query.top_k).retrieve(
            query.text
        )

    bundle = _vector_bundle(query, embedding, dimensions)
    hybrid = hybrid_config or HybridConfig()
    candidate_k = max(query.top_k, 20)
    keyword = KeywordRetriever(
        path, index, query.text, keywords, profile, hybrid.keyword_top_k or candidate_k
    )
    semantic = index.as_retriever(similarity_top_k=hybrid.vector_top_k or candidate_k)
    keyword_weight = hybrid.keyword_weight if hybrid.keyword_weight is not None else 0.5
    return QueryFusionRetriever(
        [keyword, semantic],
        # 原生融合构造需要 LLM，单次原查询的设置不会调用它。
        llm=MockLLM(),
        mode=FUSION_MODES.RECIPROCAL_RANK
        if hybrid.fusion == "rrf"
        else FUSION_MODES.RELATIVE_SCORE,
        retriever_weights=[keyword_weight, 1 - keyword_weight],
        similarity_top_k=query.top_k,
        num_queries=1,
        use_async=False,
    ).retrieve(bundle)
