"""验证在线图意的端点、模型配置和错误边界。"""

import json

import httpx
import pytest
from inkrag_platform import siliconflow_vision
from inkrag_platform.models import DomainError
from inkrag_platform.settings import load_settings
from inkrag_platform.siliconflow_vision import SiliconFlowVision
from pydantic import JsonValue, SecretStr


def test_vision_model_override_and_secret_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型名可独立替换，密钥配置在类型层保密。"""
    for name, value in {
        "CAPABILITIES": "parse",
        "DATABASE_URL": "postgresql+psycopg://dev@localhost/test",
        "RABBIT_URL": "amqp://localhost/",
        "S3_ENDPOINT": "http://localhost:9000",
        "OIDC_ISSUER": "http://localhost",
        "VISION_MODEL_ID": "Qwen/Qwen3-VL-32B-Instruct",
        "SILICONFLOW_API_KEY": "example-vision-key",
    }.items():
        monkeypatch.setenv("INKRAG_" + name, value)
    monkeypatch.delenv("INKRAG_VISION_PROVIDER", raising=False)
    settings = load_settings()
    assert settings.vision_provider == "siliconflow"
    assert settings.vision_model_id == "Qwen/Qwen3-VL-32B-Instruct"
    assert isinstance(settings.siliconflow_api_key, SecretStr)
    assert "example-vision-key" not in repr(settings)


@pytest.mark.parametrize(
    ("status", "error"),
    [(200, None), (429, "vision_model_busy"), (401, "vision_model_unavailable")],
)
def test_siliconflow_model_forwards_structured_image_request(
    monkeypatch: pytest.MonkeyPatch, status: int, error: str | None
) -> None:
    """使用独立模型标识和凭据；限流、鉴权失败保持安全错误。"""
    messages: list[dict[str, JsonValue]] = [
        {"role": "system", "content": "核对图中关系。"},
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,TEST"}}],
        },
    ]

    async def complete(
        url: str,
        payload: dict[str, JsonValue],
        timeout_seconds: float,
        invalid_code: str,
        *,
        authorization: str | None = None,
    ) -> bytes:
        """替换网络传输，只读取发送内容和上游错误映射。"""
        assert url == "https://api.siliconflow.cn/v1/chat/completions"
        assert payload["model"] == "Qwen/Qwen3-VL-32B-Instruct"
        assert payload["messages"] == [
            {
                "role": "system",
                "content": '核对图中关系。\n按下列字段和类型生成紧凑JSON：{"type":"object"}',
            },
            messages[1],
        ]
        assert payload["max_tokens"] == 768
        assert payload["enable_thinking"] is False
        assert authorization == "Bearer example-vision-key"
        assert invalid_code == "vision_model_invalid" and timeout_seconds == 45
        assert payload["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "inkrag_vision", "strict": True, "schema": {"type": "object"}},
        }
        if status != 200:
            httpx.Response(
                status, request=httpx.Request("POST", url), text="private body"
            ).raise_for_status()
        return json.dumps(
            {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        ).encode()

    monkeypatch.setattr(siliconflow_vision, "request_completion", complete)
    adapter = SiliconFlowVision("Qwen/Qwen3-VL-32B-Instruct", SecretStr("example-vision-key"))
    if error is None:
        assert adapter.complete(messages, {"type": "object"}, max_tokens=1024) == "{}"
    else:
        with pytest.raises(DomainError) as failure:
            adapter.complete(messages, {"type": "object"}, max_tokens=1024)
        assert failure.value.code == error
        assert "private body" not in str(failure.value)
