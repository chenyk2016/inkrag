"""共享 OpenAI 兼容推理的有界传输和响应校验。"""

import asyncio

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from .models import DomainError

_MODEL_REPLY_BYTES = 256 * 1024


class _Message(BaseModel):
    """只读取模型正文；拒绝工具调用及不可用回复。"""

    model_config = ConfigDict(strict=True)
    content: str | None = None
    refusal: str | None = None
    tool_calls: list[JsonValue] | None = None


class _Choice(BaseModel):
    """需要正常完成的单个回答，不消费截断结果。"""

    model_config = ConfigDict(strict=True)
    finish_reason: str
    message: _Message


class _Completion(BaseModel):
    """OpenAI-compatible非流式响应的必要字段。"""

    model_config = ConfigDict(strict=True)
    choices: list[_Choice]


async def request_completion(
    url: str,
    payload: dict[str, JsonValue],
    timeout_seconds: float,
    invalid_code: str,
    *,
    authorization: str | None = None,
) -> bytes:
    """从明确端点读取有时间和字节上限的响应；禁止代理、重定向和解压放大。"""
    headers = {"Accept-Encoding": "identity"}
    if authorization is not None:
        headers["Authorization"] = authorization
    async with asyncio.timeout(timeout_seconds):
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=timeout_seconds, headers=headers
        ) as client:
            async with client.stream("POST", url, json=payload) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise DomainError(invalid_code, "模型回复使用了不支持的压缩")
                body = bytearray()
                async for chunk in response.aiter_raw(chunk_size=16 * 1024):
                    if len(body) + len(chunk) > _MODEL_REPLY_BYTES:
                        raise DomainError(invalid_code, "模型回复超过大小预算")
                    body.extend(chunk)
    return bytes(body)


def completion_text(raw: bytes, invalid_code: str) -> str:
    """只返回单条正常终止的正文，领域模型继续检查内容。"""
    try:
        result = _Completion.model_validate_json(raw)
    except ValidationError:
        raise DomainError(invalid_code, "模型回复缺少有效结构") from None
    if len(result.choices) != 1:
        raise DomainError(invalid_code, "模型未返回唯一回复")
    choice = result.choices[0]
    message = choice.message
    if choice.finish_reason != "stop" or message.tool_calls or message.refusal:
        raise DomainError(invalid_code, "模型未生成可验证的完整回复")
    if not message.content or not message.content.strip():
        raise DomainError(invalid_code, "模型未生成有效正文")
    return message.content
