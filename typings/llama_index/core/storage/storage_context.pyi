"""补齐实际使用的原生签名，修正0.14.25持久化签名缺失PathLike泛型的问题。"""

from typing import Self

from llama_index.core.storage.index_store.types import BaseIndexStore
from llama_index.core.vector_stores.types import BasePydanticVectorStore

class StorageContext:
    """声明本包使用的原生存储能力，运行实现全部来自 LlamaIndex。"""

    index_store: BaseIndexStore
    vector_store: BasePydanticVectorStore

    @classmethod
    def from_defaults(cls, *, persist_dir: str | None = None) -> Self:
        """声明原生存储的目录加载入口。"""
        ...
    def persist(self, persist_dir: str) -> None:
        """声明将原生存储完整写入目录的能力。"""
        ...
