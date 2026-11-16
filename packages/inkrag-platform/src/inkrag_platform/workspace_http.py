"""工作台HTTP适配；只做协议校验和线程卸载，所有领域范围由PG仓库判定。"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, status
from pydantic import JsonValue, TypeAdapter
from starlette.concurrency import run_in_threadpool

from . import workspace
from .models import Document, DomainError, EvaluationSample, Job, KnowledgeBase, Principal
from .operations import Runtime, document_view
from .quality import create_quality_sample
from .workspace_models import (
    ActivationSetting,
    AdoptionRequest,
    CandidateEvaluation,
    CollectionSummary,
    DocumentListStatus,
    DocumentPage,
    EvaluationRequest,
    Overview,
    Page,
    QualitySampleRequest,
    QueryTrace,
    ReviewRequest,
    SelectionRequest,
    ServiceDraft,
    ServiceUpdate,
    TrialRequest,
    WorkspaceJob,
)
from .workspace_publication import PublicationPreview, publication_preview
from .workspace_query import query_target


def install_workspace_routes(
    app: FastAPI, runtime: Runtime, principal: Callable[..., Awaitable[Principal]]
) -> None:
    """在静态页面兜底路由之前安装，不改变宿主认证、中间件或生命周期。"""

    @app.get("/api/v2/workspace")
    async def get_overview(actor: Annotated[Principal, Depends(principal)]) -> Overview:
        """读取跨库真实计数。"""
        return await run_in_threadpool(workspace.overview, runtime.repo, actor)

    @app.get("/api/v2/workspace/knowledge-bases")
    async def list_collections(
        actor: Annotated[Principal, Depends(principal)],
        kind: Literal["knowledge", "parse", "all"] = "all",
        search: str = Query(default="", max_length=255),
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ) -> Page[CollectionSummary]:
        """按集合类型和名称服务端分页，不截断总量。"""
        return await run_in_threadpool(
            workspace.collections,
            runtime.repo,
            actor,
            kind=kind,
            search=search,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v2/workspace/jobs")
    async def list_jobs(
        actor: Annotated[Principal, Depends(principal)],
        kb_id: str | None = Query(default=None, max_length=32),
        document_id: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
        collection_kind: Literal["knowledge", "parse", "all"] = "all",
        kind: str | None = Query(default=None, max_length=32),
        status: str | None = Query(default=None, max_length=32),
        search: str = Query(default="", max_length=255),
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ) -> Page[WorkspaceJob]:
        """全局任务的KB/类型/状态/名称过滤均发生在总数计算之前。"""
        return await run_in_threadpool(
            workspace.jobs,
            runtime.repo,
            actor,
            kb_id=kb_id,
            document_id=document_id,
            collection_kind=collection_kind,
            kind=kind,
            status=status,
            search=search,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v2/workspace/knowledge-bases/{kb_id}/documents")
    async def list_documents(
        kb_id: str,
        actor: Annotated[Principal, Depends(principal)],
        search: str = Query(default="", max_length=255),
        selected: bool | None = None,
        state: DocumentListStatus | None = None,
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ) -> DocumentPage:
        """解析集合与KB共用可筛选有总数的资料清单。"""
        return await run_in_threadpool(
            workspace.documents,
            runtime.repo,
            actor,
            kb_id,
            search=search,
            selected=selected,
            state=state,
            limit=limit,
            offset=offset,
        )

    @app.put("/api/v2/documents/{document_id}/selection")
    async def select_result(
        document_id: str, body: SelectionRequest, actor: Annotated[Principal, Depends(principal)]
    ) -> Document:
        """显式选用或撤回已存结果，不触发自动发布。"""
        return await run_in_threadpool(
            workspace.select_result, runtime.repo, actor, document_id, body
        )

    @app.put("/api/v2/knowledge-bases/{kb_id}/auto-activation")
    async def set_activation(
        kb_id: str, body: ActivationSetting, actor: Annotated[Principal, Depends(principal)]
    ) -> KnowledgeBase:
        """修改当前知识库的上传生效方式。"""
        return await run_in_threadpool(runtime.repo.set_auto_activate, actor, kb_id, body.enabled)

    @app.put("/api/v2/documents/{document_id}/reviews/{result_id}")
    async def review_result(
        document_id: str,
        result_id: str,
        body: ReviewRequest,
        actor: Annotated[Principal, Depends(principal)],
    ) -> Document:
        """审核不可变结果，状态在历史版本接口可查。"""
        return await run_in_threadpool(
            workspace.review_result,
            runtime.repo,
            actor,
            document_id,
            result_id,
            body.expected_document_revision,
            body.status,
        )

    @app.post("/api/v2/knowledge-bases/{kb_id}/adoptions", status_code=status.HTTP_201_CREATED)
    async def adopt_result(
        kb_id: str, body: AdoptionRequest, actor: Annotated[Principal, Depends(principal)]
    ) -> Document:
        """将独立解析/其他KB结果采用为目标KB资料，资产不重复复制。"""
        view = await run_in_threadpool(
            document_view, runtime, actor, body.source_document_id, body.result_id
        )
        if view.result is None:
            raise DomainError("not_ready", "所选解析结果尚未保存")
        if view.result.source.asset_id is None:
            raise DomainError("invalid_artifact", "所选结果没有持久原件身份")
        return await run_in_threadpool(
            workspace.adopt,
            runtime.repo,
            actor,
            kb_id,
            body,
            source_asset_id=view.result.source.asset_id,
        )

    @app.get("/api/v2/knowledge-bases/{kb_id}/service")
    async def get_service(
        kb_id: str, actor: Annotated[Principal, Depends(principal)]
    ) -> ServiceDraft:
        """读取未发布的服务草稿。"""
        return await run_in_threadpool(workspace.get_service, runtime.repo, actor, kb_id)

    @app.put("/api/v2/knowledge-bases/{kb_id}/service")
    async def update_service(
        kb_id: str, body: ServiceUpdate, actor: Annotated[Principal, Depends(principal)]
    ) -> ServiceDraft:
        """草稿修改仅使后续候选使用新配置，不篡改当前发布。"""
        return await run_in_threadpool(workspace.update_service, runtime.repo, actor, kb_id, body)

    @app.get("/api/v2/builds/{build_id}/preview")
    async def preview_publication(
        build_id: str,
        actor: Annotated[Principal, Depends(principal)],
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ) -> PublicationPreview:
        """发布前审阅冻结版本差异及服务策略，确认后携带预览发布指针执行CAS。"""
        return await run_in_threadpool(
            publication_preview, runtime.repo, actor, build_id, limit=limit, offset=offset
        )

    @app.post("/api/v2/builds/{build_id}/retrieve")
    async def candidate_query(
        build_id: str, body: TrialRequest, actor: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """候选构建就绪后即可真实试查，无需先发布。"""
        return await run_in_threadpool(query_target, runtime, actor, build_id, body)

    @app.post("/api/v2/knowledge-bases/{kb_id}/service/retrieve")
    async def service_query(
        kb_id: str, body: TrialRequest, actor: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """按已发布版本冻结的配置校验调用方与开放字段。"""
        return await run_in_threadpool(query_target, runtime, actor, kb_id, body, candidate=False)

    @app.get("/api/v2/knowledge-bases/{kb_id}/traces")
    async def list_traces(
        kb_id: str,
        actor: Annotated[Principal, Depends(principal)],
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ) -> Page[QueryTrace]:
        """非管理员只能查看自己的请求记录。"""
        return await run_in_threadpool(
            workspace.traces, runtime.repo, actor, kb_id, limit=limit, offset=offset
        )

    @app.get("/api/v2/traces/{trace_id}")
    async def get_trace(
        trace_id: str, actor: Annotated[Principal, Depends(principal)]
    ) -> QueryTrace:
        """trace身份可反查其实际候选/发布和来源身份。"""
        return await run_in_threadpool(workspace.get_trace, runtime.repo, actor, trace_id)

    @app.post("/api/v2/builds/{build_id}/evaluations", status_code=status.HTTP_202_ACCEPTED)
    async def submit_evaluation(
        build_id: str,
        body: EvaluationRequest,
        actor: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=256)
        ],
    ) -> Job:
        """评测交给持久任务，HTTP不占用工作线程逐题调用模型。"""
        build = await run_in_threadpool(runtime.repo.get_build, actor, build_id)
        if build.status != "ready":
            raise DomainError("not_ready", "候选构建尚未完成")
        # 同一用户意图重试返回同一PG任务，异内容复用幂等键明确拒绝。
        return await run_in_threadpool(
            runtime.repo.accept_job,
            actor,
            "evaluate",
            build.kb_id,
            None,
            {
                "build_id": build_id,
                "requested_by": actor.subject,
                "name": body.name,
                "sample_ids": TypeAdapter(list[JsonValue]).validate_python(body.sample_ids),
                "top_k": body.top_k,
            },
            idempotency_key,
        )

    @app.post(
        "/api/v2/knowledge-bases/{kb_id}/quality-samples", status_code=status.HTTP_201_CREATED
    )
    async def add_quality_sample(
        kb_id: str, body: QualitySampleRequest, actor: Annotated[Principal, Depends(principal)]
    ) -> EvaluationSample:
        """保存绑定明确解析版本的条款/元素/页码金标或无答案样本。"""
        return await run_in_threadpool(create_quality_sample, runtime, actor, kb_id, body)

    @app.get("/api/v2/knowledge-bases/{kb_id}/quality-samples")
    async def list_quality_samples(
        kb_id: str, actor: Annotated[Principal, Depends(principal)]
    ) -> dict[str, list[EvaluationSample]]:
        """兼容原samples标注集合，并返回不可变标注修订链。"""
        return {"items": await run_in_threadpool(runtime.repo.list_samples, actor, kb_id)}

    @app.get("/api/v2/knowledge-bases/{kb_id}/candidate-evaluations")
    async def list_evaluations(
        kb_id: str, actor: Annotated[Principal, Depends(principal)]
    ) -> dict[str, list[CandidateEvaluation]]:
        """逐题结果通过固定build身份关联，评测不改变当前发布。"""
        return {"items": await run_in_threadpool(workspace.evaluations, runtime.repo, actor, kb_id)}
