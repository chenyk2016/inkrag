"""仅声明实际使用的S3客户端入口，上游完整overload包含未安装的其它服务。"""

from typing import Literal

from botocore.config import Config
from mypy_boto3_s3 import S3Client

from .session import Session as Session

def client(
    service_name: Literal["s3"],
    *,
    region_name: str | None = ...,
    endpoint_url: str | None = ...,
    aws_access_key_id: str | None = ...,
    aws_secret_access_key: str | None = ...,
    aws_session_token: str | None = ...,
    config: Config | None = ...,
) -> S3Client: ...
