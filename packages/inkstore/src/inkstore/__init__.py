"""独立资产存储契约和本地适配器，不依赖解析或RAG。"""

from .contracts import (
    AssetId,
    AssetInfo,
    AssetStorage,
    ResourceReference,
    StorageError,
    StoredRecord,
)

__all__ = [
    "AssetId",
    "AssetInfo",
    "AssetStorage",
    "ResourceReference",
    "StorageError",
    "StoredRecord",
]
