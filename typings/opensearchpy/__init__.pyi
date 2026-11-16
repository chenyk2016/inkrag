"""只补实际使用的OpenSearch客户端类型面，运行时仍直接调用官方SDK。"""

from collections.abc import Sequence

from pydantic import JsonValue

class TransportError(Exception):
    """官方SDK的网络与后端异常类型。"""

    status_code: int
    info: object

class Transport:
    """官方低层传输，响应在业务边界重新校验。"""

    def perform_request(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None = ...,
        body: JsonValue | str | None = ...,
        headers: dict[str, str] | None = ...,
    ) -> object: ...

class OpenSearch:
    """连接池由宿主拥有，适配器仅借用。"""

    transport: Transport

    def __init__(
        self,
        hosts: Sequence[str],
        *,
        http_auth: tuple[str, str] | None = ...,
        use_ssl: bool = ...,
        verify_certs: bool = ...,
        ssl_assert_hostname: bool = ...,
        ssl_show_warn: bool = ...,
        timeout: int = ...,
        max_retries: int = ...,
        retry_on_timeout: bool = ...,
        pool_maxsize: int = ...,
        ca_certs: str | None = ...,
    ) -> None: ...
    def close(self) -> None: ...
