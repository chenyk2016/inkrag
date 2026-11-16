"""本地MinIO初始化严格区分已有桶、并发创建及真正的权限或所有权失败。"""

from typing import TYPE_CHECKING, Literal

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from inkrag_platform import bootstrap

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


@pytest.mark.parametrize("scenario", ["existing", "new", "owned_race", "other_owner", "forbidden"])
def test_bucket_initialization_is_idempotent_and_does_not_hide_denials(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """采用官方Stubber核对请求顺序；只允许自己的创建竞争，不吞403或他人同名桶。"""
    storage = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:1",
        region_name="us-east-1",
        aws_access_key_id="nonsecret-test",
        aws_secret_access_key="nonsecret-test",
    )
    stubber = Stubber(storage)
    expected = {"Bucket": "inkrag"}
    if scenario == "existing":
        stubber.add_response("head_bucket", {}, expected)
    elif scenario == "forbidden":
        stubber.add_client_error(
            "head_bucket", "403", "denied", http_status_code=403, expected_params=expected
        )
    else:
        stubber.add_client_error(
            "head_bucket", "404", "missing", http_status_code=404, expected_params=expected
        )
        if scenario == "new":
            stubber.add_response("create_bucket", {}, expected)
        else:
            stubber.add_client_error(
                "create_bucket",
                "BucketAlreadyOwnedByYou" if scenario == "owned_race" else "BucketAlreadyExists",
                "conflict",
                http_status_code=409,
                expected_params=expected,
            )
        if scenario != "other_owner":
            stubber.add_response("head_bucket", {}, expected)

    def client(
        service_name: Literal["s3"],
        *,
        endpoint_url: str,
        region_name: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
    ) -> "S3Client":
        """借入有官方请求校验的客户端，阻止任何网络请求。"""
        return storage

    def ready(url: str, *, timeout: int = 180) -> None:
        """等待边界与S3请求分开，本测试不模拟HTTP健康语义。"""

    monkeypatch.setattr(bootstrap.boto3, "client", client)
    monkeypatch.setattr(bootstrap, "wait_http", ready)
    values = {
        "S3_PORT": "1",
        "MINIO_ACCESS_KEY": "nonsecret-test",
        "MINIO_SECRET_KEY": "nonsecret-test",
    }
    with stubber:
        if scenario in {"forbidden", "other_owner"}:
            with pytest.raises(ClientError):
                bootstrap.initialize_bucket(values)
        else:
            bootstrap.initialize_bucket(values)
        stubber.assert_no_pending_responses()
