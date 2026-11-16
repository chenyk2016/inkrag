"""显式启用的真实S3/MinIO网络验证，仅使用专用环境变量和随机前缀。"""

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from boto3.session import Session
from botocore.config import Config
from inkstore import StorageError, StoredRecord
from inkstore.s3 import S3AssetStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("INKSTORE_S3_INTEGRATION") != "1", reason="需要显式启用专用对象存储集成环境"
    ),
]


def test_real_object_store_conditional_write_integrity_and_reopen() -> None:
    """真实网络验证去重、不可变记录、并发、重开与损坏拒绝，清理仅本次前缀。"""
    client = Session().client(
        "s3",
        endpoint_url=os.environ["INKSTORE_TEST_ENDPOINT"],
        region_name="us-east-1",
        aws_access_key_id=os.environ["INKSTORE_TEST_ACCESS_KEY"],
        aws_secret_access_key=os.environ["INKSTORE_TEST_SECRET_KEY"],
        config=Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"mode": "standard", "total_max_attempts": 3},
            s3={"addressing_style": "path"},
        ),
    )
    bucket = os.environ["INKSTORE_TEST_BUCKET"]
    prefix = f"inkstore-integration/{uuid4().hex}"
    store = S3AssetStore(client, bucket=bucket, prefix=prefix)
    try:
        original = b"independent object storage original"
        with ThreadPoolExecutor(max_workers=4) as pool:
            assets = list(pool.map(store.put_bytes, [original] * 8))
        assert len({a.asset_id for a in assets}) == 1
        source_id = assets[0].asset_id
        record = StoredRecord(
            record_id=hashlib.sha256(b"record").hexdigest(),
            source_asset_id=source_id,
            payload='{"kind":"independent"}',
        )
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(store.save_record, [record] * 8))
        reopened = S3AssetStore(client, bucket=bucket, prefix=prefix)
        assert reopened.load_record(record.record_id) == record
        assert reopened.read(source_id) == original
        conflict = StoredRecord(record_id=record.record_id, source_asset_id=source_id, payload="{}")
        with pytest.raises(StorageError) as caught:
            reopened.save_record(conflict)
        assert caught.value.code == "record_conflict"
        assert reopened.load_record(record.record_id) == record
        client.put_object(Bucket=bucket, Key=f"{prefix}/objects/{source_id}", Body=b"corruption")
        with pytest.raises(StorageError) as damaged:
            reopened.read(source_id)
        assert damaged.value.code == "corrupt_asset"
        print("真实对象存储：8路资产去重、8路记录幂等、不可变冲突、重开和损坏拒绝通过")
    finally:
        objects = client.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/").get("Contents", [])
        for entry in objects:
            key = entry.get("Key")
            if key is not None:
                client.delete_object(Bucket=bucket, Key=key)
        client.close()
