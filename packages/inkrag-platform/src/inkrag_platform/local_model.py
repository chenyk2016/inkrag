"""本机兼容模型的有界请求通道，由显式回答与图意任务共享。"""

import asyncio
from dataclasses import dataclass
from ipaddress import ip_address
from threading import BoundedSemaphore
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from .chat_completion import completion_text, request_completion
from .models import DomainError

# 本机运行时共享同一GPU/内存预算，繁忙直接返回，避免线程池积压。
_MODEL_SLOT = BoundedSemaphore(1)
_LOCAL_HOSTS = {"localhost", "host.docker.internal", "host.orbstack.internal"}


def _local_host(host: str) -> bool:
    """仅识别回环地址和受控开发容器的宿主别名。"""
    if host in _LOCAL_HOSTS:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class LocalChatModel:
    """仅连配置的本机模型，限制总等待、输出及并行请求。"""

    base_url: str
    model_id: str
    timeout_seconds: float = 90
    disable_reasoning: bool = False

    def __post_init__(self) -> None:
        """部署时拒绝公网、URL内凭证、重定向入口与无效参数。"""
        parts = urlsplit(self.base_url)
        if (
            parts.scheme not in {"http", "https"}
            or not _local_host(parts.hostname or "")
            or parts.username is not None
            or parts.password is not None
            or parts.path.rstrip("/") != "/v1"
            or parts.query
            or parts.fragment
            or parts.port == 0
        ):
            raise ValueError("模型地址必须是本机或开发容器宿主的 /v1 地址")
        if not self.model_id.strip() or len(self.model_id) > 200:
            raise ValueError("模型标识不能为空或超过200字符")
        if not 1 <= self.timeout_seconds <= 300:
            raise ValueError("本机模型超时必须在1到300秒之间")

    async def _request(self, payload: dict[str, JsonValue]) -> bytes:
        """在绝对时间和字节预算内读取模型返回，不使用系统代理。"""
        return await request_completion(
            self.base_url.rstrip("/") + "/chat/completions",
            payload,
            self.timeout_seconds,
            "local_model_invalid",
        )

    def complete(
        self,
        messages: list[dict[str, JsonValue]],
        schema: dict[str, JsonValue],
        *,
        max_tokens: int,
    ) -> str:
        """在线程池或Worker调用；校验模型信封后返回待领域验证的JSON正文。"""
        if not 1 <= max_tokens <= 4096:
            raise ValueError("模型输出token预算超出范围")
        if not _MODEL_SLOT.acquire(blocking=False):
            raise DomainError("local_model_busy", "本机模型正在处理其他请求，请稍后重试")
        try:
            payload: dict[str, JsonValue] = {
                "model": self.model_id,
                "messages": [{**message} for message in messages],
                "temperature": 0,
                "max_tokens": max_tokens,
                "stream": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "inkrag_result", "strict": True, "schema": schema},
                },
            }
            if self.disable_reasoning:
                payload["reasoning_effort"] = "none"
            return completion_text(asyncio.run(self._request(payload)), "local_model_invalid")
        except (TimeoutError, httpx.TimeoutException):
            raise DomainError("local_model_timeout", "本机模型处理超时，请稍后重试") from None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise DomainError(
                    "local_model_busy", "本机模型正在处理其他请求，请稍后重试"
                ) from None
            if exc.response.status_code in {408, 504}:
                raise DomainError("local_model_timeout", "本机模型处理超时，请稍后重试") from None
            raise DomainError(
                "local_model_unavailable", "本机模型暂不可用，请检查服务与已加载模型"
            ) from None
        except httpx.HTTPError:
            raise DomainError(
                "local_model_unavailable", "本机模型暂不可用，请检查服务与已加载模型"
            ) from None
        finally:
            _MODEL_SLOT.release()
