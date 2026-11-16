"""线上回答HTTP入口，沿用同一身份验证与检索策略。"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from starlette.concurrency import run_in_threadpool

from .answers import GroundedAnswer, answer_question
from .local_model import LocalChatModel
from .models import DomainError, Principal
from .operations import Runtime
from .workspace_models import TrialRequest


def install_answer_routes(
    app: FastAPI,
    runtime: Runtime,
    principal: Callable[..., Awaitable[Principal]],
    model: LocalChatModel | None,
) -> None:
    """安装显式请求的问答入口，不影响原检索接口或独立解析宿主。"""

    @app.post("/api/v2/knowledge-bases/{kb_id}/answers")
    async def answer(
        kb_id: str,
        request: TrialRequest,
        http: Request,
        actor: Annotated[Principal, Depends(principal)],
    ) -> GroundedAnswer:
        """只有已授权调用者可以对当前线上知识发起回答。"""
        if runtime.search is None or model is None:
            raise DomainError("answer_unavailable", "当前宿主尚未启用本机回答模型")

        # 首次鉴权由通用依赖完成，耗时生成后再验证原凭证，避免撤销/过期后仍返回。
        token = http.headers.get("authorization", "").partition(" ")[2]

        def reauthorize() -> Principal:
            """重新验证未持久化的同一请求凭证。"""
            return runtime.auth.verify(token)

        return await run_in_threadpool(
            answer_question, runtime, actor, kb_id, request, model, reauthorize
        )
