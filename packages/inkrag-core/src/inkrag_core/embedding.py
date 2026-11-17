"""可选的 HTTP 向量提供者，索引本身无需模型供应商 SDK。"""

import hashlib
import http.client
import json
import math
from collections.abc import Sequence
from urllib.parse import urlsplit

from .contracts import validate_vector
from .document import object_list, object_map

_MAX_RESPONSE = 32 * 1024 * 1024


class OpenAICompatibleEmbedding:
    """连接兼容的 `/embeddings` 接口，按索引恢复输入顺序。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        dimensions: int,
        *,
        api_key: str | None = None,
        revision: str = "default",
        timeout: float = 20,
    ) -> None:
        """冻结服务地址、模型版本、预期维度和请求时限。"""
        url = urlsplit(base_url)
        if (
            not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
            or (
                url.scheme != "https"
                and (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"})
            )
        ):
            raise ValueError("嵌入服务需要 HTTPS，仅回环地址接受 HTTP")
        if not model.strip() or not revision.strip() or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("需要模型、修订和有效超时")
        if type(dimensions) is not int or not 1 <= dimensions <= 4096:
            raise ValueError("向量维度必须在 1 至 4096 之间")
        self._host = url.hostname
        self._port = url.port
        self._secure = url.scheme == "https"
        self._path = url.path.rstrip("/") + "/embeddings"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self.dimensions = dimensions
        identity = [url.scheme, self._host, self._port, self._path, model, revision]
        self.fingerprint = (
            "http-embeddings:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        )

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """请求生成向量。凭据不进入索引、异常正文或重定向请求。"""
        if not texts:
            return ()
        payload = json.dumps({"input": list(texts), "model": self._model}).encode()
        headers = {"Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        connection_type = (
            http.client.HTTPSConnection if self._secure else http.client.HTTPConnection
        )
        connection = connection_type(self._host, self._port, timeout=self._timeout)
        try:
            connection.request("POST", self._path, body=payload, headers=headers)
            with connection.getresponse() as response:
                if response.status != 200:
                    raise RuntimeError(f"嵌入服务返回 HTTP {response.status}")
                data = response.read(_MAX_RESPONSE + 1)
        except (OSError, http.client.HTTPException):
            raise RuntimeError("嵌入服务连接失败") from None
        finally:
            connection.close()
        if len(data) > _MAX_RESPONSE:
            raise RuntimeError("嵌入响应超过 32 MiB")
        try:
            return _decode(data, len(texts), self.dimensions)
        except (ValueError, UnicodeError):
            raise RuntimeError("嵌入服务结果不符合预期的数量、索引或维度") from None


def _decode(payload: bytes, count: int, dimensions: int) -> tuple[tuple[float, ...], ...]:
    """拒绝缺失、重复索引和维度不匹配，避免将向量放到错误的片段。"""
    raw: object = json.loads(payload)
    if not object_map(raw):
        raise ValueError("响应不是对象")
    data = raw.get("data")
    if not object_list(data) or len(data) != count:
        raise ValueError("响应条数不符")
    results: dict[int, tuple[float, ...]] = {}
    for row in data:
        if not object_map(row):
            raise ValueError("响应条目不是对象")
        index, vector = row.get("index"), row.get("embedding")
        if type(index) is not int or not 0 <= index < count or index in results:
            raise ValueError("响应条目索引无效")
        if not object_list(vector):
            raise ValueError("响应缺少向量")
        components: list[float] = []
        for value in vector:
            if type(value) not in {int, float} or not isinstance(value, int | float):
                raise ValueError("向量分量不是数字")
            components.append(float(value))
        results[index] = validate_vector(components, dimensions)
    return tuple(results[index] for index in range(count))
