"""提供一个正式入口，直接使用原生模型和节点完成知识入库与检索。"""

from inkparse import ParseOptions

from .models import Error, HybridConfig, IngestionResult, ProcessingConfig, RetrievalResult
from .rag import RAG
from .siliconflow import SiliconFlowEmbedding

__all__ = [
    "RAG",
    "Error",
    "HybridConfig",
    "IngestionResult",
    "ParseOptions",
    "ProcessingConfig",
    "RetrievalResult",
    "SiliconFlowEmbedding",
]
