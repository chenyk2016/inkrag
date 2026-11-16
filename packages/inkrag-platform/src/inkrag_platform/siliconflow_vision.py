"""硅基流动图片理解适配，仅向官方端点提交当前选中的图片。"""

import asyncio
import json
from dataclasses import dataclass

import httpx
from pydantic import JsonValue, SecretStr

from .chat_completion import completion_text, request_completion
from .models import DomainError

DEFAULT_VISION_MODEL_ID = "Qwen/Qwen3-VL-32B-Instruct"
_CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
_TIMEOUT_SECONDS = 45


@dataclass(frozen=True)
class SiliconFlowVision:
    """使用独立凭据访问固定 HTTPS 地址；模型标识由部署方选择。"""

    model_id: str
    api_key: SecretStr

    def complete(
        self,
        messages: list[dict[str, JsonValue]],
        schema: dict[str, JsonValue],
        *,
        max_tokens: int,
    ) -> str:
        """按结构约定读取结果，外部失败仅返回安全类别。"""
        format_instruction = "按下列字段和类型生成紧凑JSON：" + json.dumps(
            schema, ensure_ascii=False, separators=(",", ":")
        )
        payload: dict[str, JsonValue] = {
            "model": self.model_id,
            "messages": [
                {**message, "content": f"{message['content']}\n{format_instruction}"}
                if message.get("role") == "system"
                else {**message}
                for message in messages
            ],
            "temperature": 0,
            "max_tokens": min(max_tokens, 768),
            "stream": False,
            "enable_thinking": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "inkrag_vision", "strict": True, "schema": schema},
            },
        }
        try:
            raw = asyncio.run(
                request_completion(
                    _CHAT_URL,
                    payload,
                    _TIMEOUT_SECONDS,
                    "vision_model_invalid",
                    authorization="Bearer " + self.api_key.get_secret_value(),
                )
            )
        except (TimeoutError, httpx.TimeoutException):
            raise DomainError("vision_model_timeout", "硅基流动图片理解超时，请稍后重试") from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                raise DomainError("vision_model_busy", "硅基流动图片理解繁忙，请稍后重试") from None
            if status in {408, 504}:
                raise DomainError(
                    "vision_model_timeout", "硅基流动图片理解超时，请稍后重试"
                ) from None
            if status in {401, 403}:
                raise DomainError(
                    "vision_model_unavailable", "硅基流动拒绝调用，请核对服务凭据和模型权限"
                ) from None
            if status in {400, 404, 422}:
                raise DomainError(
                    "vision_model_unavailable", "请核对模型是否可用，并支持图片输入和结构化输出"
                ) from None
            raise DomainError(
                "vision_model_unavailable", "硅基流动图片理解暂不可用，请稍后重试"
            ) from None
        except httpx.HTTPError:
            raise DomainError("vision_model_unavailable", "无法连接硅基流动图片理解服务") from None
        return completion_text(raw, "vision_model_invalid")
