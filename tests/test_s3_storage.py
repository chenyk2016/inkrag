"""验证S3适配器的条件写、完整性与安全失败边界，不把SDK桩当成真实云兼容。"""

import base64
import hashlib
import io
from collections.abc import Generator

import pytest
from boto3.session import Session
from botocore.response import StreamingBody
from botocore.stub import Stubber
from inkstore import StorageError, StoredRecord
from inkstore.s3 import S3AssetStore
from mypy_boto3_s3 import S3Client

BUCKET = "inkstore-test"


@pytest.fixture
def client() -> Generator[S3Client, None, None]:
    """使用专用占位凭据构造SDK，测试不读取真实凭据链或访问网络。"""
    value = Session().client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="unit-test-only",
        aws_secret_access_key="unit-test-only",
    )
    yield value
    value.close()


def object_response(data: bytes) -> dict[str, object]:
    """构造真实StreamingBody，用实际SDK响应类型验证有界读取。"""
    return {
        "Body": StreamingBody(io.BytesIO(data), len(data)),
        "ContentLength": len(data),
        "ResponseMetadata": {
            "RequestId": "test",
            "HostId": "test",
            "HTTPStatusCode": 200,
            "HTTPHeaders": {},
            "RetryAttempts": 0,
        },
    }


def add_read(stub: Stubber, identity: str, data: bytes, collection: str = "objects") -> None:
    """预期一次精确键读取，防止适配器改到错误桶或越过前缀。"""
    stub.add_response(
        "get_object",
        object_response(data),
        {"Bucket": BUCKET, "Key": f"inkstore/v1/{collection}/{identity}"},
    )


def test_put_assets_are_conditional_and_deduplicated(client: S3Client) -> None:
    """初次写入必须带条件和传输校验，重复提交不能覆盖已有对象。"""
    data = b"immutable original"
    identity = hashlib.sha256(data).hexdigest()
    parameters = {
        "Bucket": BUCKET,
        "Key": f"inkstore/v1/objects/{identity}",
        "Body": data,
        "ContentType": "application/octet-stream",
        "IfNoneMatch": "*",
        "ContentMD5": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode(),
    }
    with Stubber(client) as stub:
        stub.add_response("put_object", {}, parameters)
        add_read(stub, identity, data)
        stub.add_client_error(
            "put_object", "PreconditionFailed", http_status_code=412, expected_params=parameters
        )
        add_read(stub, identity, data)
        store = S3AssetStore(client, bucket=BUCKET)
        assert store.put_bytes(data) == store.put_bytes(data)
        stub.assert_no_pending_responses()


def test_existing_corrupt_asset_is_not_overwritten(client: S3Client) -> None:
    """既有摘要键下的字节损坏时明确失败，不能重写来掩盖存储异常。"""
    data = b"correct original"
    identity = hashlib.sha256(data).hexdigest()
    with Stubber(client) as stub:
        stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
        add_read(stub, identity, b"damaged")
        with pytest.raises(StorageError) as caught:
            S3AssetStore(client, bucket=BUCKET).put_bytes(data)
        assert caught.value.code == "corrupt_asset"
        stub.assert_no_pending_responses()


def test_record_identity_is_immutable_and_missing_reference_prevents_commit(
    client: S3Client,
) -> None:
    """资源校验先于发布；已有同ID不同内容的记录不能被覆盖。"""
    source = b"source"
    source_id = hashlib.sha256(source).hexdigest()
    record_id = hashlib.sha256(b"record").hexdigest()
    old = StoredRecord(record_id=record_id, source_asset_id=source_id, payload='{"v":1}')
    new = StoredRecord(record_id=record_id, source_asset_id=source_id, payload='{"v":2}')
    with Stubber(client) as stub:
        add_read(stub, source_id, source)
        stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
        add_read(stub, record_id, old.model_dump_json().encode(), "records")
        add_read(stub, source_id, source)
        with pytest.raises(StorageError) as caught:
            S3AssetStore(client, bucket=BUCKET).save_record(new)
        assert caught.value.code == "record_conflict"
        stub.add_client_error("get_object", "NoSuchKey", http_status_code=404)
        with pytest.raises(StorageError) as missing:
            S3AssetStore(client, bucket=BUCKET).save_record(old)
        assert missing.value.code == "missing_asset"
        stub.assert_no_pending_responses()


def test_conflicts_retry_boundedly_and_provider_details_are_hidden(client: S3Client) -> None:
    """409只有限重试；权限失败不暴露供应商消息、桶路径或凭据。"""
    store = S3AssetStore(client, bucket=BUCKET)
    with Stubber(client) as stub:
        for _ in range(3):
            stub.add_client_error("put_object", "ConditionalRequestConflict", http_status_code=409)
        with pytest.raises(StorageError) as busy:
            store.put_bytes(b"source")
        assert busy.value.code == "storage_busy"
        stub.add_client_error(
            "put_object", "AccessDenied", service_message="secret endpoint", http_status_code=403
        )
        with pytest.raises(StorageError) as forbidden:
            store.put_bytes(b"source")
        assert forbidden.value.code == "storage_failed"
        assert "secret" not in str(forbidden.value)
        stub.assert_no_pending_responses()


def test_corrupt_record_and_unsafe_prefix_are_rejected(client: S3Client) -> None:
    """不接受越界对象前缀，损坏JSON不会被包装成空记录。"""
    with pytest.raises(StorageError, match="前缀"):
        S3AssetStore(client, bucket=BUCKET, prefix="../elsewhere")
    identity = hashlib.sha256(b"record").hexdigest()
    with Stubber(client) as stub:
        add_read(stub, identity, b"not-json", "records")
        with pytest.raises(StorageError) as caught:
            S3AssetStore(client, bucket=BUCKET).load_record(identity)
        assert caught.value.code == "corrupt_record"
        stub.assert_no_pending_responses()
