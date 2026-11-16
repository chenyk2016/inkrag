"""解析独立产品的审校、原文结构、离线交付与原件恢复HTTP边界。"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse
from inkparse import (
    ParseResult,
    StructureOperation,
    revise_structure,
    save_bundle,
    save_stored_result,
)
from inkparse.parser import FORMATS
from pydantic import Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .models import DomainError, Job, Principal
from .operations import DocumentView, Runtime, StrictModel, document_view


class StructureRevisionRequest(StrictModel):
    """按预期版本原子提交类型化审校操作，拒绝覆盖别人的新版本。"""

    expected_result_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: list[StructureOperation] = Field(min_length=1, max_length=1000)
    activate: bool = False
    expected_document_revision: int | None = Field(default=None, ge=0)


class ReparseRequest(StrictModel):
    """重新运行当前原件；首次解析失败的资料版本为0，同样可创建恢复意图。"""

    expected_document_revision: int = Field(ge=0)


class MachinePreview(StrictModel):
    """只读机器结构；Office逻辑定位可对照，不宣称还原印刷排版。"""

    result: ParseResult
    source_kind: Literal["native_structure"] = "native_structure"


def save_structure(
    runtime: Runtime, principal: Principal, document_id: str, request: StructureRevisionRequest
) -> DocumentView:
    """先验证写权限和当前结果，再保存不可变资产，最后PG条件提交新指针。"""
    if not {"admin", "editor"}.intersection(principal.roles):
        raise DomainError("forbidden", "当前账号没有修改权限")
    view = document_view(runtime, principal, document_id)
    if view.result is None:
        raise DomainError("not_ready", "资料尚未解析完成")
    if view.result.result_id != request.expected_result_id:
        raise DomainError("version_conflict", "当前版本已变化，请刷新后检查")
    result = revise_structure(view.result, request.operations)
    save_stored_result(result, runtime.assets_for(principal))
    runtime.repo.save_revision(
        principal,
        document_id,
        request.expected_result_id,
        result.result_id,
        result.revision,
        result.previous_result_id,
        [asset.asset_id for asset in result.assets],
        activate=request.activate,
        expected_document_revision=request.expected_document_revision,
    )
    return document_view(runtime, principal, document_id, result.result_id)


def machine_preview(
    runtime: Runtime, principal: Principal, document_id: str, result_id: str | None
) -> MachinePreview:
    """沿本资料已授权版本链读取同一原件的机器首版，不把修订文字当作原文。"""
    view = document_view(runtime, principal, document_id, result_id)
    if view.result is None:
        raise DomainError("not_ready", "资料尚未解析完成，请先查看已保存原件")
    result = view.result
    total = len(result.model_dump_json().encode())
    for _ in range(100):
        if result.previous_result_id is None:
            return MachinePreview(result=result)
        previous = document_view(runtime, principal, document_id, result.previous_result_id).result
        if previous is None or previous.source.sha256 != result.source.sha256:
            raise DomainError("invalid_artifact", "机器原文版本链不完整，请查看已保存原件")
        result = previous
        total += len(result.model_dump_json().encode())
        if total > 64 * 1024 * 1024:
            break
    raise DomainError("preview_budget_exceeded", "原文结构版本链超过读取预算，请下载原件查看")


def bundle_document(
    runtime: Runtime,
    principal: Principal,
    document_id: str,
    result_id: str | None,
    include_original: bool,
    destination: Path,
) -> None:
    """对明确版本导出完整离线包，资源缺失时不交付残缺的成功结果。"""
    view = document_view(runtime, principal, document_id, result_id)
    if view.result is None:
        raise DomainError("not_ready", "资料尚未解析完成")
    save_bundle(
        view.result, runtime.assets_for(principal), destination, include_original=include_original
    )


def build_parse_router(
    runtime: Runtime,
    principal_dependency: Callable[..., Principal | Awaitable[Principal]],
) -> APIRouter:
    """借入平台运行时和身份依赖；宿主负责身份验证、统一错误和正文预算。"""
    router = APIRouter(prefix="/api/v2/documents", tags=["文档解析"])

    @router.patch("/{document_id}/structure")
    async def structure(
        document_id: str,
        value: StructureRevisionRequest,
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> DocumentView:
        """保存文字、表格、图片说明、排除和人工补充的一个审校版本。"""
        return await run_in_threadpool(save_structure, runtime, principal, document_id, value)

    @router.get("/{document_id}/preview")
    async def preview(
        document_id: str,
        principal: Annotated[Principal, Depends(principal_dependency)],
        result_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
    ) -> MachinePreview:
        """返回机器原始结构，PDF实际页面仍由原件渲染器显示。"""
        return await run_in_threadpool(machine_preview, runtime, principal, document_id, result_id)

    @router.get(
        "/{document_id}/bundle",
        response_class=FileResponse,
        responses={
            200: {
                "content": {
                    "application/zip": {
                        "schema": {"type": "string", "format": "binary"},
                    }
                }
            }
        },
    )
    async def bundle(
        document_id: str,
        principal: Annotated[Principal, Depends(principal_dependency)],
        result_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
        include_original: bool = False,
    ) -> FileResponse:
        """交付受权限控制的ZIP，文件内容可脱离服务器查看。"""
        temporary = TemporaryDirectory(prefix="inkparse-export-")
        path = Path(temporary.name) / "document.zip"
        try:
            await run_in_threadpool(
                bundle_document, runtime, principal, document_id, result_id, include_original, path
            )
        except BaseException:
            temporary.cleanup()
            raise
        return FileResponse(
            path,
            media_type="application/zip",
            filename="inkparse-document.zip",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(temporary.cleanup),
        )

    @router.post("/{document_id}/reparse", status_code=202)
    async def reparse(
        document_id: str,
        value: ReparseRequest,
        principal: Annotated[Principal, Depends(principal_dependency)],
        idempotency_key: Annotated[str, Header(min_length=8, max_length=128)],
    ) -> Job:
        """用户显式重新解析；机器故障自动重试规则仍由任务层控制。"""
        return await run_in_threadpool(
            runtime.repo.submit_reparse,
            principal,
            document_id,
            value.expected_document_revision,
            idempotency_key,
        )

    @router.put(
        "/{document_id}/source",
        status_code=202,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )
    async def replace_source(
        document_id: str,
        request: Request,
        principal: Annotated[Principal, Depends(principal_dependency)],
        filename: Annotated[str, Query(min_length=1, max_length=255, pattern=r"^[^/\\\x00]+$")],
        expected_document_revision: Annotated[int, Query(ge=0)],
        idempotency_key: Annotated[str, Header(min_length=8, max_length=128)],
    ) -> Job:
        """有界读取替换原件；解析成功之前，旧原件和可用结果保持可读取。"""
        if not {"admin", "editor"}.intersection(principal.roles):
            raise DomainError("forbidden", "当前账号没有修改权限")
        await run_in_threadpool(runtime.repo.get_document, principal, document_id)
        if Path(filename).suffix.lower() not in FORMATS:
            raise DomainError(
                "unsupported_format",
                "请选择服务能力中列出的文件格式；替换原件沿用已选解析工具",
            )
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 20 * 1024 * 1024:
                raise DomainError("invalid_upload", "原件超过20MiB")
            data.extend(chunk)
        if not data:
            raise DomainError("invalid_upload", "原件不能为空")
        storage = runtime.assets_for(principal)
        asset = await run_in_threadpool(storage.put_bytes, bytes(data))
        return await run_in_threadpool(
            runtime.repo.submit_reparse,
            principal,
            document_id,
            expected_document_revision,
            idempotency_key,
            asset.asset_id,
            filename,
        )

    return router
