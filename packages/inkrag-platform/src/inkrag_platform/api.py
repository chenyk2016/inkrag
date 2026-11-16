"""生产平台HTTP边界：可信身份、有界输入、持久任务与相同核心规则。"""

import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal
from urllib.parse import quote, unquote_to_bytes, urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from inkparse import ParseError
from inkparse.adapters import BUILTIN_FORMATS, DOCLING_FORMATS, ParserEngine
from inkparse.adapters.docling.models import models_ready
from inkstore import AssetId, StorageError
from pydantic import Field, JsonValue, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.gzip import GZipMiddleware

from .answer_routes import install_answer_routes
from .http_limits import MAX_UPLOAD, BodyBudget, CapacityGate, failure
from .imports import build_import_router
from .models import (
    Document,
    DomainError,
    Job,
    KnowledgeBase,
    MetadataField,
    Principal,
)
from .operations import (
    DocumentView,
    RevisionRequest,
    Runtime,
    StrictModel,
    check_parser_input,
    document_view,
    export_document,
    save_revision,
    submit_upload,
)
from .parse_routes import build_parse_router
from .vision_routes import build_vision_router
from .workspace_http import install_workspace_routes

LOGGER = logging.getLogger("inkrag.platform.http")


class NameRequest(StrictModel):
    """创建知识库的明确名称，不接受客户端作用域覆盖。"""

    name: str = Field(min_length=1, max_length=128)


class CollectionRequest(NameRequest):
    """集合类型与名称共同确定独立解析或知识产品入口。"""

    kind: Literal["knowledge", "parse"] = "knowledge"


class EnabledRequest(StrictModel):
    """资料检索开关；关闭保留原件和已有版本。"""

    enabled: bool


class IngestRequest(StrictModel):
    """提交已保存版本，响应只确认任务已持久接受。"""

    expected_result_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_document_revision: int = Field(ge=0)


class PublishRequest(StrictModel):
    """发布或回滚必须指出操作时看到的当前发布身份。"""

    expected_release_id: str | None = None


class RetrieveRequest(StrictModel):
    """合法过滤由搜索适配再次检查，绝不接受原始DSL或索引名。"""

    kb_id: str
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=100)
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"
    filters: list[dict[str, JsonValue]] = Field(
        default_factory=list[dict[str, JsonValue]], max_length=20
    )


class FieldsRequest(StrictModel):
    """管理员以KB聚合版本CAS声明字段，变更后仍需重建发布。"""

    expected_revision: int = Field(ge=0)
    fields: list[MetadataField] = Field(max_length=64)


class MetadataRequest(StrictModel):
    """资料聚合版本条件更新业务字段，格式和标量由仓库验证。"""

    expected_document_revision: int = Field(ge=0)
    metadata: dict[str, JsonValue]


class SampleRequest(StrictModel):
    """人工标注相关资料，用于可重复的文档级检索评测。"""

    query: str = Field(min_length=1, max_length=4000)
    expected_document_ids: list[str] = Field(min_length=1, max_length=100)


class EvaluationRequest(StrictModel):
    """提交已发布版本上的评测任务，不在HTTP进程计算指标。"""

    release_id: str
    name: str = Field(min_length=1, max_length=128)
    top_k: int = Field(default=5, ge=1, le=100)
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"


def create_app(runtime: Runtime, *, web_dir: str | Path | None = None) -> FastAPI:
    """仅装配已初始化的依赖；不启动Docker、迁移、模型或后台线程。"""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        """依赖生命周期归装配入口所有，应用不关闭借入连接。"""
        yield

    app = FastAPI(
        title="InkRAG 文档与知识平台",
        version="2.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyBudget)
    app.add_middleware(CapacityGate)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

    @app.middleware("http")
    async def response_policy(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """请求ID及浏览器策略统一实施，令牌不出现在URL或日志。"""
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        issuer = runtime.oidc.get("issuer", "")
        parts = urlsplit(issuer)
        origin = (
            f"{parts.scheme}://{parts.netloc}"
            if parts.scheme in {"http", "https"} and parts.netloc
            else ""
        )
        frame_ancestors = "'self'" if request.url.path == "/oidc/silent-callback" else "'none'"
        response.headers.update(
            {
                "X-Request-ID": request.state.request_id,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "worker-src 'self' blob:; img-src 'self' blob: data:; "
                f"frame-src 'self' blob: {origin}; object-src 'none'; base-uri 'none'; "
                f"frame-ancestors {frame_ancestors}; connect-src 'self' {origin}",
            }
        )
        if request.url.path.startswith("/assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    bearer = HTTPBearer(auto_error=False)

    async def principal(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        """成熟Bearer依赖生成真实OpenAPI安全约定，身份仍由可信验证器判定。"""
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                401, "需要组织登录或有效的调用方凭证", headers={"WWW-Authenticate": "Bearer"}
            )
        return await run_in_threadpool(runtime.auth.verify, credentials.credentials)

    app.include_router(build_parse_router(runtime, principal))
    app.include_router(build_import_router(runtime, principal))
    install_workspace_routes(app, runtime, principal)
    install_answer_routes(app, runtime, principal, runtime.local_model)
    app.include_router(build_vision_router(runtime, principal, runtime.vision_model))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """协议拒绝统一结构，不回显输入字段内容。"""
        response = failure(
            "unauthorized" if exc.status_code == 401 else "invalid_request",
            str(exc.detail),
            exc.status_code,
            request.state.request_id,
        )
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """业务类别映射HTTP，冲突与依赖故障不被混为成功。"""
        status = (
            401
            if exc.code in {"unauthorized", "invalid_token"}
            else 403
            if exc.code == "forbidden"
            else 404
            if exc.code == "not_found"
            else 413
            if exc.code == "vision_image_too_large"
            else 429
            if exc.code in {"local_model_busy", "vision_model_busy"}
            else 502
            if exc.code in {"local_model_invalid", "vision_model_invalid"}
            else 409
            if exc.code
            in {
                "version_conflict",
                "idempotency_conflict",
                "not_ready",
                "stale_job",
                "lease_lost",
                "cancelled",
                "no_release",
                "not_published",
                "authorization_pending",
                "vision_excluded",
            }
            else 503
            if exc.code
            in {
                "database_unavailable",
                "storage_unavailable",
                "search_unavailable",
                "invalid_embedding_response",
                "embedding_unavailable",
                "answer_unavailable",
                "vision_unavailable",
                "vision_model_timeout",
                "vision_model_unavailable",
                "local_model_timeout",
                "local_model_unavailable",
            }
            else 422
        )
        response = failure(
            exc.code, str(exc), status, request.state.request_id, details=exc.details
        )
        if exc.code in {"local_model_busy", "vision_model_busy"}:
            response.headers["Retry-After"] = "2"
        return response

    if runtime.search is not None:
        from inkrag.models import Error as SearchError

        @app.exception_handler(SearchError)
        async def search_error(request: Request, exc: SearchError) -> JSONResponse:
            """仅检索宿主导入RAG错误；输入、索引契约与依赖故障分别返回。"""
            if exc.code in {"invalid_filter", "invalid_fields", "invalid_query"}:
                return failure(exc.code, str(exc), 422, request.state.request_id)
            if exc.code in {
                "index_contract_conflict",
                "field_schema_conflict",
                "index_not_frozen",
                "index_copy_incomplete",
                "index_frozen",
                "build_conflict",
                "invalid_scope",
            }:
                return failure(
                    exc.code,
                    "知识库索引契约已变化，请重新构建并发布",
                    409,
                    request.state.request_id,
                )
            return failure(
                exc.code, "检索依赖未能完整返回结果，请稍后重试", 503, request.state.request_id
            )

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(request: Request, _exc: httpx.HTTPError) -> JSONResponse:
        """推理提供方网络/状态异常不泄露地址或正文，也不伪装空命中。"""
        return failure(
            "embedding_unavailable", "检索依赖暂时不可用，请稍后重试", 503, request.state.request_id
        )

    @app.exception_handler(ParseError)
    async def parse_error(request: Request, exc: ParseError) -> JSONResponse:
        """输入不能被解析时明确拒绝，不返回缺失内容成功。"""
        return failure(exc.code, str(exc), 422, request.state.request_id)

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError) -> JSONResponse:
        """外部存储失败不回退本地，不泄露客户端配置。"""
        return failure(exc.code, str(exc), 503, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def request_error(request: Request, _exc: RequestValidationError) -> JSONResponse:
        """避免Pydantic默认错误回显敏感请求输入。"""
        return failure("invalid_request", "请求字段、类型或预算无效", 422, request.state.request_id)

    @app.exception_handler(ValidationError)
    async def validation_error(request: Request, _exc: ValidationError) -> JSONResponse:
        """损坏记录不能被当成完整资料返回。"""
        return failure("invalid_data", "数据校验失败", 422, request.state.request_id)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """以关联ID记录异常类型，响应不包含正文或秘密。"""
        LOGGER.error("request_failed id=%s type=%s", request.state.request_id, type(exc).__name__)
        return failure("internal_error", "请求失败，请凭关联ID核查", 500, request.state.request_id)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        """仅检查进程存活，不伪装依赖健康。"""
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> Response:
        """装配层检查真实依赖；就绪不代表任务/业务容量已经验收。"""
        try:
            await run_in_threadpool(runtime.check_ready or runtime.repo.check)
        except Exception:
            return failure("dependencies_unavailable", "服务依赖尚未就绪", 503, uuid4().hex)
        return JSONResponse({"status": "ready"})

    @app.get("/api/v2/session/config")
    async def session_config() -> dict[str, JsonValue]:
        """只公开OIDC客户端配置，不包含任何客户端秘密。"""
        return {
            "issuer": runtime.oidc.get("issuer", ""),
            "client_id": runtime.oidc.get("client_id", "inkrag-web"),
            "mode": runtime.mode,
        }

    @app.get("/api/v2/me")
    async def me(p: Annotated[Principal, Depends(principal)]) -> Principal:
        """返回可信身份与角色，供界面解释权限。"""
        return p

    @app.get("/api/v2/capabilities")
    async def capabilities(p: Annotated[Principal, Depends(principal)]) -> dict[str, JsonValue]:
        """能力从真实装配产生，不假装parse-only已有模型或索引。"""
        from .processing import processing_capabilities

        model = runtime.local_model.model_id if runtime.local_model else None
        vision_model = runtime.vision_model
        return {
            "answer": {
                "available": model is not None and runtime.search is not None,
                "model": model,
            },
            "vision": {
                "available": vision_model is not None,
                "model": vision_model.model_id if vision_model else None,
                "provider": runtime.vision_provider,
            },
            "processing": processing_capabilities(runtime),
            "formats": list(BUILTIN_FORMATS),
            "parse_engines": [
                {
                    "id": "builtin",
                    "ocr_formats": [],
                    "ocr_default": False,
                    "name": "内置解析",
                    "formats": list(BUILTIN_FORMATS),
                    "available": True,
                    "reason": None,
                    "pending_formats": [],
                },
                {
                    "id": "docling",
                    "ocr_formats": ["pdf", "image"],
                    "ocr_default": True,
                    "name": "Docling",
                    "formats": list(DOCLING_FORMATS),
                    "available": True,
                    "reason": "PDF及图片需要预置离线模型"
                    if not models_ready(runtime.parse_models_path)
                    else None,
                    "pending_formats": ["pdf", "image"]
                    if not models_ready(runtime.parse_models_path)
                    else [],
                },
            ],
            "max_upload_bytes": MAX_UPLOAD,
            "storage": "S3/MinIO",
            "mode": runtime.mode,
            "tenant_id": p.tenant_id,
            "roles": [role for role in p.roles],
            "limitations": [
                "Docling 支持 PDF 与图片 OCR，可按文件开启或关闭；实际可用性取决于离线模型配置。内置解析不执行 OCR。",
                "OCR 提取图片文字；图片语义理解与生成完整回答尚未接入。旧 DOC/PPT 请先转换为 DOCX/PPTX。",
                "解析与入库异步执行；自动生效完成或手动发布后，资料才参与线上检索。",
            ],
        }

    @app.get("/api/v2/status")
    async def service_status(_p: Annotated[Principal, Depends(principal)]) -> dict[str, JsonValue]:
        """受权显示真实依赖探测和固定模型身份，永不返回连接秘密。"""
        healthy = True
        try:
            await run_in_threadpool(runtime.check_ready or runtime.repo.check)
        except Exception:
            healthy = False
        return {"ready": healthy, "mode": runtime.mode, "model_identity": runtime.model_identity}

    @app.get("/api/v2/knowledge-bases/{kb_id}/fields")
    async def fields(kb_id: str, p: Annotated[Principal, Depends(principal)]) -> KnowledgeBase:
        """返回工作态字段定义及KB版本；该版本不替代在线发布schema。"""
        return await run_in_threadpool(runtime.repo.get_kb, p, kb_id)

    @app.put("/api/v2/knowledge-bases/{kb_id}/fields")
    async def set_fields(
        kb_id: str, request: FieldsRequest, p: Annotated[Principal, Depends(principal)]
    ) -> KnowledgeBase:
        """字段变更由仓库完整检查当前资料与CAS，只有管理员可写。"""
        return await run_in_threadpool(
            runtime.repo.set_fields, p, kb_id, request.expected_revision, request.fields
        )

    @app.get("/api/v2/knowledge-bases/{kb_id}/search-fields")
    async def search_fields(
        kb_id: str, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """读取已发布实际索引的受控schema，避免编辑态字段误导在线查询。"""
        if runtime.search is None:
            raise HTTPException(422, "当前宿主未启用检索")
        release = await run_in_threadpool(runtime.repo.get_active_release, p, kb_id)
        schema = await run_in_threadpool(
            runtime.search.fields_json, p.tenant_id, kb_id, release.index_name
        )
        return {
            "fields": schema,
            "release_id": release.release_id,
            "model_revision": release.model_revision,
        }

    @app.get("/api/v2/openapi.json")
    async def openapi(_p: Annotated[Principal, Depends(principal)]) -> JSONResponse:
        """按实际请求响应生成受保护协议。"""
        return JSONResponse(app.openapi())

    @app.get("/api/v2/knowledge-bases")
    async def knowledge_bases(
        p: Annotated[Principal, Depends(principal)],
    ) -> dict[str, list[KnowledgeBase]]:
        """只返回当前主体有权访问的知识库。"""
        return {"items": await run_in_threadpool(runtime.repo.list_kbs, p)}

    @app.post("/api/v2/knowledge-bases", status_code=201)
    async def create_knowledge_base(
        request: CollectionRequest, p: Annotated[Principal, Depends(principal)]
    ) -> KnowledgeBase:
        """创建受租户/角色限制的知识库。"""
        return await run_in_threadpool(runtime.repo.create_kb, p, request.name, request.kind)

    @app.get("/api/v2/knowledge-bases/{kb_id}/documents")
    async def documents(
        kb_id: str,
        p: Annotated[Principal, Depends(principal)],
        limit: int = Query(default=100, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, list[Document]]:
        """在数据库范围过滤后分页，不先加载全库。"""
        return {
            "items": await run_in_threadpool(runtime.repo.list_documents, p, kb_id, limit, offset)
        }

    @app.post(
        "/api/v2/knowledge-bases/{kb_id}/documents",
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
    async def upload(
        kb_id: str,
        request: Request,
        p: Annotated[Principal, Depends(principal)],
        filename: str = Query(min_length=1, max_length=255),
        parser_engine: Annotated[ParserEngine, Query()] = "builtin",
        ocr_enabled: Annotated[bool | None, Query()] = None,
        idempotency_key: Annotated[str, Header(min_length=8, max_length=128)] = "",
        x_ink_metadata: Annotated[str, Header(max_length=16384)] = "%7B%7D",
    ) -> Job:
        """流式写临时盘并受控保存原件，只接受持久解析任务，不在API解析。"""
        if not idempotency_key:
            raise HTTPException(422, "上传需提供Idempotency-Key")
        if not re.fullmatch(r"[^/\\\x00-\x1f]+", filename) or filename in {".", ".."}:
            raise HTTPException(422, "文件名无效")
        try:
            await run_in_threadpool(
                check_parser_input, runtime, parser_engine, filename, ocr_enabled
            )
        except ParseError as exc:
            raise HTTPException(415, str(exc)) from None
        await run_in_threadpool(runtime.repo.get_kb, p, kb_id)
        metadata = TypeAdapter(dict[str, JsonValue]).validate_json(unquote_to_bytes(x_ink_metadata))
        if not {"admin", "editor"}.intersection(p.roles):
            raise DomainError("forbidden", "当前账号没有修改权限")
        with NamedTemporaryFile(
            prefix="inkrag-upload-", suffix=Path(filename).suffix.lower()
        ) as stream:
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise HTTPException(413, "文件超过20MiB")
                await run_in_threadpool(stream.write, chunk)
            await run_in_threadpool(stream.flush)
            return await run_in_threadpool(
                submit_upload,
                runtime,
                p,
                kb_id,
                Path(stream.name),
                filename,
                idempotency_key,
                metadata,
                parser_engine,
                ocr_enabled,
            )

    @app.get("/api/v2/documents/{document_id}/info")
    async def document_info(
        document_id: str, p: Annotated[Principal, Depends(principal)]
    ) -> Document:
        """按稳定身份读取小型目录事实，未解析深链不依赖列表首屏或载入全文。"""
        return await run_in_threadpool(runtime.repo.get_document, p, document_id)

    @app.get("/api/v2/documents/{document_id}")
    async def document(
        document_id: str, p: Annotated[Principal, Depends(principal)], result_id: str | None = None
    ) -> DocumentView:
        """读取当前或历史版本，解析尚未完成时result为空。"""
        return await run_in_threadpool(document_view, runtime, p, document_id, result_id)

    @app.patch("/api/v2/documents/{document_id}")
    async def revise(
        document_id: str, request: RevisionRequest, p: Annotated[Principal, Depends(principal)]
    ) -> DocumentView:
        """保存显式修订，PG版本冲突不覆盖他人结果。"""
        return await run_in_threadpool(save_revision, runtime, p, document_id, request)

    @app.patch("/api/v2/documents/{document_id}/enabled")
    async def document_enabled(
        document_id: str, request: EnabledRequest, p: Annotated[Principal, Depends(principal)]
    ) -> Document:
        """编辑者切换检索资格，确认成功后的新查询立即使用此状态。"""
        return await run_in_threadpool(
            runtime.repo.set_document_enabled, p, document_id, request.enabled
        )

    @app.patch("/api/v2/documents/{document_id}/metadata")
    async def metadata(
        document_id: str, request: MetadataRequest, p: Annotated[Principal, Depends(principal)]
    ) -> Document:
        """资料字段修改以聚合revision CAS保护，旧构建不能越过KB版本发布。"""
        return await run_in_threadpool(
            runtime.repo.update_metadata,
            p,
            document_id,
            request.expected_document_revision,
            request.metadata,
        )

    @app.get("/api/v2/documents/{document_id}/versions")
    async def versions(
        document_id: str, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """列举受控资料历史，只暴露版本元信息。"""
        rows = await run_in_threadpool(runtime.repo.versions, p, document_id)
        return {
            "items": [TypeAdapter(JsonValue).validate_json(row.model_dump_json()) for row in rows]
        }

    @app.post("/api/v2/documents/{document_id}/ingest", status_code=202)
    async def ingest(
        document_id: str,
        request: IngestRequest,
        p: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[str, Header(min_length=8, max_length=128)] = "",
    ) -> Job:
        """入库只提交持久意图，明确版本检查后由worker执行。"""
        if runtime.search is None:
            raise HTTPException(422, "当前宿主未启用RAG")
        if not idempotency_key:
            raise HTTPException(422, "入库需提供Idempotency-Key")
        current = await run_in_threadpool(runtime.repo.get_document, p, document_id)
        return await run_in_threadpool(
            runtime.repo.accept_job,
            p,
            "ingest",
            current.kb_id,
            document_id,
            {"result_id": request.expected_result_id},
            idempotency_key,
            request.expected_result_id,
            request.expected_document_revision,
        )

    @app.delete("/api/v2/documents/{document_id}")
    async def delete(
        document_id: str,
        p: Annotated[Principal, Depends(principal)],
        expected_revision: int = Query(ge=0),
    ) -> Document:
        """先撤销资料可见性并fence后台任务，物理清理由保留策略决定。"""
        return await run_in_threadpool(
            runtime.repo.delete_document, p, document_id, expected_revision
        )

    @app.get("/api/v2/jobs")
    async def jobs(kb_id: str, p: Annotated[Principal, Depends(principal)]) -> dict[str, list[Job]]:
        """从持久任务事实查询，不依赖某个API实例的内存。"""
        return {"items": await run_in_threadpool(runtime.repo.list_jobs, p, kb_id)}

    @app.get("/api/v2/jobs/{job_id}")
    async def job(job_id: str, p: Annotated[Principal, Depends(principal)]) -> Job:
        """读取单个真实任务阶段和错误。"""
        return await run_in_threadpool(runtime.repo.get_job, p, job_id)

    @app.post("/api/v2/jobs/{job_id}/cancel")
    async def cancel(job_id: str, p: Annotated[Principal, Depends(principal)]) -> Job:
        """持久记录取消意图，不以杀进程替代版本约束。"""
        return await run_in_threadpool(runtime.repo.cancel_job, p, job_id)

    @app.post("/api/v2/jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: str, p: Annotated[Principal, Depends(principal)]) -> Job:
        """仅显式重试合法失败任务，沿用持久输入与恢复语义。"""
        return await run_in_threadpool(runtime.repo.retry_job, p, job_id)

    @app.post("/api/v2/knowledge-bases/{kb_id}/builds", status_code=202)
    async def rebuild(
        kb_id: str,
        p: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[str, Header(min_length=8, max_length=128)] = "",
        incremental: bool = False,
        full_rebuild: bool = False,
    ) -> Job:
        """显式构建可见清单，恢复时可跳过旧物理索引从原文重建。"""
        if runtime.search is None or not idempotency_key:
            raise HTTPException(422, "重建需要检索能力和Idempotency-Key")
        if incremental and full_rebuild:
            raise DomainError("invalid_request", "从原文完整重建时不可同时请求增量更新")
        payload: dict[str, JsonValue] = {}
        if incremental:
            payload["incremental"] = True
        if full_rebuild:
            payload["full_rebuild"] = True
        return await run_in_threadpool(
            runtime.repo.accept_job,
            p,
            "build",
            kb_id,
            None,
            payload,
            idempotency_key,
        )

    @app.get("/api/v2/knowledge-bases/{kb_id}/builds")
    async def builds(
        kb_id: str, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """只列出该知识库可见构建，ready不表示已发布。"""
        rows = await run_in_threadpool(runtime.repo.list_builds, p, kb_id)
        return {
            "items": [TypeAdapter(JsonValue).validate_json(row.model_dump_json()) for row in rows]
        }

    @app.get("/api/v2/knowledge-bases/{kb_id}/releases")
    async def releases(
        kb_id: str, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """发布历史是不可变事实，查询不会自动采用最新工作build。"""
        rows = await run_in_threadpool(runtime.repo.list_releases, p, kb_id)
        return {
            "items": [TypeAdapter(JsonValue).validate_json(row.model_dump_json()) for row in rows]
        }

    @app.post("/api/v2/builds/{build_id}/publish")
    async def publish(
        build_id: str, request: PublishRequest, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """对已完成的真实generation做PG条件发布。"""
        row = await run_in_threadpool(
            runtime.repo.publish_build, p, build_id, request.expected_release_id
        )
        return {"release": TypeAdapter(JsonValue).validate_json(row.model_dump_json())}

    @app.post("/api/v2/releases/{release_id}/rollback")
    async def rollback(
        release_id: str, request: PublishRequest, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """回滚追加发布，不绕过当前授权和删除epoch。"""
        row = await run_in_threadpool(
            runtime.repo.rollback_release, p, release_id, request.expected_release_id
        )
        return {"release": TypeAdapter(JsonValue).validate_json(row.model_dump_json())}

    @app.post("/api/v2/retrieve")
    async def retrieve(
        request: RetrieveRequest, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """兼容查询入口与正式服务共用冻结策略，不能绕过固定范围和调用权限。"""
        from .workspace_models import TrialRequest
        from .workspace_query import query_target

        trial = TrialRequest.model_validate(
            request.model_dump(exclude={"kb_id"}, exclude_unset=True)
        )
        result = await run_in_threadpool(
            query_target, runtime, p, request.kb_id, trial, candidate=False
        )
        return result

    @app.get("/api/v2/knowledge-bases/{kb_id}/callers")
    async def callers(
        kb_id: str, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """列出调用方元信息，永不返回已签发密钥原文。"""
        rows = await run_in_threadpool(runtime.repo.list_api_keys, p, kb_id)
        return {
            "items": [TypeAdapter(JsonValue).validate_json(row.model_dump_json()) for row in rows]
        }

    @app.post("/api/v2/knowledge-bases/{kb_id}/callers", status_code=201)
    async def issue_caller(
        kb_id: str, request: NameRequest, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """调用方密钥只在签发时显示一次，其范围由服务端固定。"""
        row = await run_in_threadpool(runtime.repo.create_api_key, p, kb_id, request.name)
        return {"key": TypeAdapter(JsonValue).validate_json(row.model_dump_json())}

    @app.delete("/api/v2/callers/{key_id}", status_code=204)
    async def revoke_caller(key_id: str, p: Annotated[Principal, Depends(principal)]) -> Response:
        """撤销指定凭据，后续请求必须立即拒绝。"""
        await run_in_threadpool(runtime.repo.revoke_api_key, p, key_id)
        return Response(status_code=204)

    @app.get("/api/v2/audit")
    async def audit(
        p: Annotated[Principal, Depends(principal)], kb_id: str | None = None
    ) -> dict[str, JsonValue]:
        """读取受控审计事实，不回显秘密或完整文档正文。"""
        rows = await run_in_threadpool(runtime.repo.list_audit, p, kb_id)
        return {
            "items": [TypeAdapter(JsonValue).validate_json(row.model_dump_json()) for row in rows]
        }

    @app.get("/api/v2/knowledge-bases/{kb_id}/samples")
    async def samples(
        kb_id: str, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """读取真实人工标注，不自动生成相关性判断。"""
        rows = await run_in_threadpool(runtime.repo.list_samples, p, kb_id)
        return {
            "items": [TypeAdapter(JsonValue).validate_json(row.model_dump_json()) for row in rows]
        }

    @app.post("/api/v2/knowledge-bases/{kb_id}/samples", status_code=201)
    async def add_sample(
        kb_id: str, request: SampleRequest, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """只允许本库可见资料作为相关性标注。"""
        row = await run_in_threadpool(
            runtime.repo.create_sample, p, kb_id, request.query, request.expected_document_ids
        )
        return {"sample": TypeAdapter(JsonValue).validate_json(row.model_dump_json())}

    @app.get("/api/v2/knowledge-bases/{kb_id}/evaluations")
    async def evaluations(
        kb_id: str, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, JsonValue]:
        """只展示实际完成并保存的固定发布评测。"""
        rows = await run_in_threadpool(runtime.repo.list_evaluations, p, kb_id)
        return {
            "items": [TypeAdapter(JsonValue).validate_json(row.model_dump_json()) for row in rows]
        }

    @app.post("/api/v2/knowledge-bases/{kb_id}/evaluations", status_code=202)
    async def evaluate(
        kb_id: str,
        request: EvaluationRequest,
        p: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[str, Header(min_length=8, max_length=128)] = "",
    ) -> Job:
        """冻结人工样本身份和已发布输入，后台计算而非接收客户端指标。"""
        if runtime.search is None or not idempotency_key:
            raise HTTPException(422, "评测需要检索能力和Idempotency-Key")
        release = await run_in_threadpool(runtime.repo.get_active_release, p, kb_id)
        if release.release_id != request.release_id:
            raise DomainError("version_conflict", "发布版本已变化")
        rows = await run_in_threadpool(runtime.repo.list_samples, p, kb_id)
        document_samples = [row for row in rows if not row.no_answer and not row.expected_sources]
        if not document_samples or len(document_samples) > 100:
            raise DomainError("invalid_evaluation", "线上评测需要1到100个文档级标注")
        return await run_in_threadpool(
            runtime.repo.accept_job,
            p,
            "evaluate",
            kb_id,
            None,
            {
                "release_id": request.release_id,
                "name": request.name,
                "top_k": request.top_k,
                "mode": request.mode,
                "sample_ids": [row.sample_id for row in document_samples],
            },
            idempotency_key,
        )

    @app.get("/api/v2/assets/{asset_id}")
    async def asset(asset_id: str, p: Annotated[Principal, Depends(principal)]) -> Response:
        """先核对KB可见资源，再从当前租户的存储范围读取。"""
        TypeAdapter(AssetId).validate_python(asset_id, strict=True)
        if not await run_in_threadpool(runtime.repo.has_asset, p, asset_id):
            raise DomainError("not_found", "资源不存在或不可访问")
        data = await run_in_threadpool(runtime.assets_for(p).read, asset_id)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment"},
        )

    @app.get("/api/v2/documents/{document_id}/export")
    async def export(
        document_id: str,
        p: Annotated[Principal, Depends(principal)],
        format: Literal["json", "markdown"] = "json",
        result_id: str | None = None,
    ) -> Response:
        """导出选定不可变结果，文件名不接受控制字符。"""
        data = await run_in_threadpool(export_document, runtime, p, document_id, format, result_id)
        return Response(
            data,
            media_type="application/json" if format == "json" else "text/markdown",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document_id)}.{'json' if format == 'json' else 'md'}"
            },
        )

    if web_dir is not None:
        root = Path(web_dir)

        @app.get("/", include_in_schema=False)
        @app.get("/oidc/callback", include_in_schema=False)
        @app.get("/oidc/silent-callback", include_in_schema=False)
        @app.get("/knowledge", include_in_schema=False)
        @app.get("/knowledge/{frontend_path:path}", include_in_schema=False)
        @app.get("/parsing", include_in_schema=False)
        @app.get("/tasks", include_in_schema=False)
        @app.get("/settings", include_in_schema=False)
        @app.get("/logout", include_in_schema=False)
        async def frontend(frontend_path: str = "") -> FileResponse:
            """仅正式前端路由回退入口，API未知路径仍返回404，不被SPA遮蔽。"""
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-store"})

        app.mount("/", StaticFiles(directory=root, html=True), name="web")
    return app
