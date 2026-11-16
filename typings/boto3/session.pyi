"""补充仅用于S3装配的类型，不参与运行或增加SDK包装。"""

from typing import Literal

from botocore.config import Config
from mypy_boto3_s3 import S3Client

class Session:
    """原生boto3会话的最小类型面。"""
    def __init__(
        self,
        aws_access_key_id: str | None = ...,
        aws_secret_access_key: str | None = ...,
        aws_session_token: str | None = ...,
        region_name: str | None = ...,
        profile_name: str | None = ...,
    ) -> None: ...
    def client(
        self,
        service_name: Literal["s3"],
        *,
        region_name: str | None = ...,
        endpoint_url: str | None = ...,
        aws_access_key_id: str | None = ...,
        aws_secret_access_key: str | None = ...,
        aws_session_token: str | None = ...,
        config: Config | None = ...,
    ) -> S3Client: ...
