"""资产服务边界；不导入解析、Docling、RAG或任何具体存储实现。"""

import json
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

AssetId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class StorageError(Exception):
    """存储失败的安全错误分类，由业务包或HTTP宿主决定呈现方式。"""

    def __init__(self, code: str, message: str) -> None:
        """记录不包含底层凭证或SQL内容的说明。"""
        super().__init__(message)
        self.code = code


class StrictModel(BaseModel):
    """拒绝错误类型与未知字段，存储契约与业务模型分离。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class AssetInfo(StrictModel):
    """按字节摘要寻址的资产元数据。"""

    asset_id: AssetId
    size_bytes: int = Field(ge=1, le=20 * 1024 * 1024)


class ResourceReference(StrictModel):
    """通用资源使用关系，存储不解释locator或role的业务含义。"""

    asset_id: AssetId
    locator: str
    role: str
    page: int | None = Field(default=None, ge=1)


class StoredRecord(StrictModel):
    """不透明JSON记录和其引用；由业务层定义payload结构与摘要算法。"""

    record_id: AssetId
    source_asset_id: AssetId
    payload: str = Field(min_length=1)
    references: list[ResourceReference] = Field(default_factory=list[ResourceReference])

    @model_validator(mode="after")
    def check_payload(self) -> "StoredRecord":
        """限制记录大小并拒绝非法JSON，不解析任何业务类型。"""
        if len(self.payload.encode()) > 32 * 1024 * 1024:
            raise ValueError("记录超过32MiB")
        json.loads(self.payload)
        return self


class AssetStorage(Protocol):
    """可由本地、对象存储或远程客户端实现的最小业务所需能力。"""

    def put_bytes(self, data: bytes) -> AssetInfo:
        """持久保存完整字节后返回真实摘要；失败不能返回成功身份。"""
        ...

    def get(self, asset_id: str) -> AssetInfo:
        """验证资产存在且完整，缺失/损坏必须明确失败。"""
        ...

    def read(self, asset_id: str) -> bytes:
        """返回与ID一致的完整字节，不静默访问其他后端。"""
        ...

    def save_record(self, record: StoredRecord) -> None:
        """验证所有引用后原子提交不可变记录，重复提交应幂等。"""
        ...

    def load_record(self, record_id: str) -> StoredRecord:
        """读取完整记录并验证关联资源，失败不返回删减后的记录。"""
        ...
