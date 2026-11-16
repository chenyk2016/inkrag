"""为 RAG.open_published 提供平台连接，不启动 HTTP 进程。"""

from collections.abc import Callable, Generator
from contextlib import contextmanager

from pydantic import JsonValue

from .models import DomainError
from .settings import Settings, load_settings
from .wiring import open_runtime
from .workspace_models import TrialRequest
from .workspace_query import query_target


@contextmanager
def published_connection(
    kb_id: str, token: str, *, settings: Settings | None = None
) -> Generator[Callable[[dict[str, JsonValue]], dict[str, JsonValue]]]:
    """绑定知识库和可信凭证；每次查询复验凭证，并复用线上发布规则和轨迹。"""
    configured = settings if settings is not None else load_settings()
    if configured.capabilities == "parse":
        raise DomainError("capability_unavailable", "当前宿主没有启用检索能力")
    with open_runtime(configured) as runtime:

        def execute(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
            """与 HTTP 一样校验动态参数、身份和当前发布。"""
            principal = runtime.auth.verify(token)
            return query_target(
                runtime, principal, kb_id, TrialRequest.model_validate(payload), candidate=False
            )

        yield execute
