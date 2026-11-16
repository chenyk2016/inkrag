"""资料图意草稿的 HTTP 入口，审批保存沿用资料结构审校。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from starlette.concurrency import run_in_threadpool

from .models import DomainError, Principal
from .operations import Runtime
from .repository import require_role
from .vision import VisionModel, VisionRequest, VisionSuggestion, suggest_picture


def build_vision_router(
    runtime: Runtime,
    principal_dependency: Callable[..., Principal | Awaitable[Principal]],
    provider: VisionModel | None = None,
) -> APIRouter:
    """挂载受宿主统一身份、请求预算和错误处理保护的图意入口。"""
    router = APIRouter(prefix="/api/v2/documents", tags=["图意审校"])
    slots = asyncio.Semaphore(2)

    @router.post("/{document_id}/vision")
    async def picture_vision(
        document_id: str,
        value: VisionRequest,
        principal: Annotated[Principal, Depends(principal_dependency)],
        response: Response,
    ) -> VisionSuggestion:
        """为当前保存版本的图片生成待审核草稿。"""
        require_role(principal, write=True)
        if slots.locked():
            raise DomainError("vision_model_busy", "正在处理其他图片，请稍后重试")
        response.headers["Cache-Control"] = "no-store"
        async with slots:
            return await run_in_threadpool(
                suggest_picture, runtime, principal, document_id, value, provider
            )

    return router
