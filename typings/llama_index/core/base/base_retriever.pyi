"""补全原生召回适配器使用的公开签名，明确对象映射值的类型边界。"""

from abc import abstractmethod

from llama_index.core.callbacks.base import CallbackManager
from llama_index.core.schema import IndexNode, NodeWithScore, QueryBundle

class BaseRetriever:
    """描述原生召回器的构造、调用及子类扩展入口。"""
    def __init__(
        self,
        callback_manager: CallbackManager | None = None,
        object_map: dict[str, object] | None = None,
        objects: list[IndexNode] | None = None,
        verbose: bool = False,
    ) -> None: ...
    def retrieve(self, str_or_query_bundle: str | QueryBundle) -> list[NodeWithScore]: ...
    async def aretrieve(self, str_or_query_bundle: str | QueryBundle) -> list[NodeWithScore]: ...
    @abstractmethod
    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]: ...
