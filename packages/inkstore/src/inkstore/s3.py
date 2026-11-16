"""通过调用方借入的boto3客户端访问S3/MinIO，资产和记录均采用条件写。"""

import base64
import hashlib
import re
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import TypeAdapter, ValidationError

from .contracts import AssetId, AssetInfo, StorageError, StoredRecord

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

MAX_ASSET_BYTES = 20 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024 * 1024


class S3AssetStore:
    """已有普通桶中的独立资产仓库；不创建桶、不保存凭证、不关闭借入客户端。

    endpoint必须正确实现If-None-Match条件写和读后写一致性。服务只新增对象，
    不提供删除/GC；外部删除或覆盖须由IAM/桶策略限制，不能由引用记录阻止。
    """

    def __init__(self, client: "S3Client", *, bucket: str, prefix: str = "inkstore/v1") -> None:
        """校验命名范围后持有客户端；构造不发请求或探测调用方的凭证来源。"""
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise StorageError("invalid_storage", "请提供普通S3桶名称")
        prefix = prefix.strip("/")
        if not prefix or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
            raise StorageError("invalid_storage", "资产前缀只能包含字母、数字、横线及目录分隔")
        self._client, self._bucket, self._prefix = client, bucket, prefix

    def _key(self, collection: str, identity: str) -> str:
        """只允许严格摘要形成对象键，外部路径不能决定存储范围。"""
        value = TypeAdapter(AssetId).validate_python(identity, strict=True)
        return f"{self._prefix}/{collection}/{value}"

    def _put_new(self, key: str, data: bytes, content_type: str) -> bool:
        """条件新增对象；已存在返回False，有限重试409，其他异常不隐式换后端。"""
        for _ in range(3):
            try:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                    ContentMD5=base64.b64encode(
                        hashlib.md5(data, usedforsecurity=False).digest()
                    ).decode("ascii"),
                    IfNoneMatch="*",
                )
                return True
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"PreconditionFailed", "412"}:
                    return False
                if code in {"ConditionalRequestConflict", "409"}:
                    continue
                raise StorageError("storage_failed", "对象存储写入失败") from None
            except (BotoCoreError, OSError):
                raise StorageError("storage_failed", "对象存储连接或写入失败") from None
        raise StorageError("storage_busy", "对象存储存在并发写入冲突，请稍后重试")

    def _read_object(self, key: str, *, limit: int, missing: str) -> bytes:
        """有界读取并关闭响应流，避免不可信对象长度导致内存无界增长。"""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
            try:
                data = body.read(limit + 1)
            finally:
                body.close()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise StorageError(missing, "对象存储中不存在此资源") from None
            raise StorageError("storage_failed", "对象存储读取失败") from None
        except (BotoCoreError, OSError):
            raise StorageError("storage_failed", "对象存储连接或读取失败") from None
        if not data or len(data) > limit:
            raise StorageError("corrupt_object", "存储对象为空或超过允许大小")
        return data

    def put_bytes(self, data: bytes) -> AssetInfo:
        """按SHA256新增资产并校验回读；相同字节复用，损坏对象不会被覆盖。"""
        if not data or len(data) > MAX_ASSET_BYTES:
            raise StorageError("invalid_asset", "资产为空或超过20MiB")
        identity = hashlib.sha256(data).hexdigest()
        self._put_new(self._key("objects", identity), data, "application/octet-stream")
        if self.read(identity) != data:
            raise StorageError("corrupt_asset", "对象存储中的已有资产与输入不一致")
        return AssetInfo(asset_id=identity, size_bytes=len(data))

    def read(self, asset_id: str) -> bytes:
        """读取资产并核对完整SHA256，不把ETag假定为内容摘要。"""
        data = self._read_object(
            self._key("objects", asset_id), limit=MAX_ASSET_BYTES, missing="missing_asset"
        )
        if hashlib.sha256(data).hexdigest() != asset_id:
            raise StorageError("corrupt_asset", "资产完整性校验失败")
        return data

    def get(self, asset_id: str) -> AssetInfo:
        """返回经实际字节完整性验证的元数据，不仅依赖HEAD或用户元数据。"""
        return AssetInfo(asset_id=asset_id, size_bytes=len(self.read(asset_id)))

    def save_record(self, record: StoredRecord) -> None:
        """全部资产校验后条件提交单个不可变记录；失败可能留下未引用对象。

        单个PutObject是记录的发布点，不与多个对象构成分布式事务。同ID不同
        记录明确冲突；网络失败不伪造成功，重复调用可按不变记录核对并恢复。
        """
        record = StoredRecord.model_validate_json(record.model_dump_json())
        for asset_id in sorted({record.source_asset_id, *(r.asset_id for r in record.references)}):
            self.get(asset_id)
        payload = record.model_dump_json().encode()
        if len(payload) > MAX_RECORD_BYTES:
            raise StorageError("invalid_record", "序列化资产记录超过64MiB")
        created = self._put_new(self._key("records", record.record_id), payload, "application/json")
        if not created and self.load_record(record.record_id) != record:
            raise StorageError("record_conflict", "已有记录内容冲突")

    def load_record(self, record_id: str) -> StoredRecord:
        """还原不透明记录并核对其身份与全部资源，损坏不返回删减后的结果。"""
        data = self._read_object(
            self._key("records", record_id), limit=MAX_RECORD_BYTES, missing="missing_record"
        )
        try:
            record = StoredRecord.model_validate_json(data)
        except ValidationError:
            raise StorageError("corrupt_record", "存储记录格式不完整或已损坏") from None
        if record.record_id != record_id:
            raise StorageError("corrupt_record", "记录身份不一致")
        for asset_id in sorted({record.source_asset_id, *(r.asset_id for r in record.references)}):
            self.get(asset_id)
        return record
