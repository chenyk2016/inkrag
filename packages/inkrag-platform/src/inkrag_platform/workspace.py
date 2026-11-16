"""工作台短事务与领域控制；复用仓库授权，不在HTTP内拼接范围判断。"""

import json
import re
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue
from sqlalchemy import and_, case, func, literal, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from .database import (
    AssetReferenceRow,
    AuditRow,
    CandidateEvaluationRow,
    DocumentRow,
    JobRow,
    KnowledgeBaseRow,
    QueryTraceRow,
    ResultRow,
    ServiceRow,
    TenantRow,
)
from .models import (
    Document,
    DomainError,
    MetadataField,
    Principal,
    validate_metadata,
)
from .repository import Repository, document_value, job_value, kb_value, require_role
from .workspace_models import (
    AdoptionRequest,
    CandidateEvaluation,
    CollectionSummary,
    DocumentListItem,
    DocumentListStatus,
    DocumentPage,
    Overview,
    Page,
    QueryTrace,
    SelectionRequest,
    ServiceConfig,
    ServiceDraft,
    ServiceUpdate,
    WorkspaceJob,
    validate_service_filters,
)


def _visible(session: Session, principal: Principal) -> Select[tuple[str]]:
    """所有全局聚合先验证可信身份，再从PG取可见KB身份。"""
    require_role(principal)
    stmt = (
        select(KnowledgeBaseRow.id)
        .join(TenantRow, TenantRow.id == KnowledgeBaseRow.tenant_id)
        .where(KnowledgeBaseRow.tenant_id == principal.tenant_id, TenantRow.active.is_(True))
    )
    if principal.kb_ids is not None:
        stmt = stmt.where(KnowledgeBaseRow.id.in_(principal.kb_ids))
    return stmt


def _page(limit: int, offset: int) -> None:
    """拒绝无界/负分页；稳定排序含唯一身份解决相同时间。"""
    if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
        raise DomainError("invalid_request", "分页范围无效")


def _snapshot(session: Session) -> None:
    """分页与计数共享只读快照，不因并发写入产生同页互相矛盾。"""
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))


def overview(repo: Repository, principal: Principal) -> Overview:
    """用全量可见集合聚合，不受列表默认上限影响。"""
    with repo.transaction() as session:
        _snapshot(session)
        ids = _visible(session, principal)
        bases = select(KnowledgeBaseRow).where(KnowledgeBaseRow.id.in_(ids))
        kinds = {
            kind: count
            for kind, count in session.execute(
                select(KnowledgeBaseRow.kind, func.count())
                .where(KnowledgeBaseRow.id.in_(ids))
                .group_by(KnowledgeBaseRow.kind)
            ).tuples()
        }
        documents = (
            select(DocumentRow)
            .where(
                DocumentRow.tenant_id == principal.tenant_id,
                DocumentRow.kb_id.in_(ids),
                DocumentRow.deleted.is_(False),
            )
            .subquery()
        )
        tasks = {
            state: count
            for state, count in session.execute(
                select(JobRow.status, func.count())
                .where(
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.kb_id.in_(ids),
                    JobRow.kind != "import_preview",
                )
                .group_by(JobRow.status)
            ).tuples()
        }
        return Overview(
            knowledge_base_count=kinds.get("knowledge", 0),
            parse_collection_count=kinds.get("parse", 0),
            document_count=session.scalar(select(func.count()).select_from(documents)) or 0,
            selected_count=session.scalar(
                select(func.count())
                .select_from(documents)
                .where(documents.c.selected_result_id.is_not(None))
            )
            or 0,
            task_count=sum(tasks.values()),
            running_count=tasks.get("running", 0),
            queued_count=tasks.get("queued", 0) + tasks.get("retry_wait", 0),
            failed_count=tasks.get("failed", 0) + tasks.get("reconciling", 0),
            published_count=session.scalar(
                select(func.count()).select_from(
                    bases.where(KnowledgeBaseRow.active_release_id.is_not(None)).subquery()
                )
            )
            or 0,
        )


def collections(
    repo: Repository,
    principal: Principal,
    *,
    kind: Literal["knowledge", "parse", "all"] = "all",
    search: str = "",
    limit: int = 30,
    offset: int = 0,
) -> Page[CollectionSummary]:
    """服务端筛选集合并报告全部匹配数；计数不会退化为当前页长度。"""
    _page(limit, offset)
    with repo.transaction() as session:
        _snapshot(session)
        stmt = select(KnowledgeBaseRow).where(KnowledgeBaseRow.id.in_(_visible(session, principal)))
        if kind != "all":
            stmt = stmt.where(KnowledgeBaseRow.kind == kind)
        if search:
            stmt = stmt.where(KnowledgeBaseRow.name.icontains(search, autoescape=True))
        total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        page = (
            stmt.with_only_columns(KnowledgeBaseRow.id)
            .order_by(KnowledgeBaseRow.created_at.desc(), KnowledgeBaseRow.id)
            .limit(limit)
            .offset(offset)
            .cte("visible_page")
        )
        docs = (
            select(
                DocumentRow.kb_id,
                func.count().label("documents"),
                func.count().filter(DocumentRow.selected_result_id.is_not(None)).label("selected"),
            )
            .where(
                DocumentRow.tenant_id == principal.tenant_id,
                DocumentRow.kb_id.in_(select(page.c.id)),
                DocumentRow.deleted.is_(False),
            )
            .group_by(DocumentRow.kb_id)
            .subquery()
        )
        tasks = (
            select(
                JobRow.kb_id,
                func.count().label("jobs"),
                func.count()
                .filter(JobRow.status.in_(["failed", "reconciling"]))
                .label("unresolved"),
            )
            .where(
                JobRow.tenant_id == principal.tenant_id,
                JobRow.kb_id.in_(select(page.c.id)),
                JobRow.kind != "import_preview",
            )
            .group_by(JobRow.kb_id)
            .subquery()
        )
        summaries = (
            select(
                KnowledgeBaseRow,
                func.coalesce(docs.c.documents, 0),
                func.coalesce(docs.c.selected, 0),
                func.coalesce(tasks.c.jobs, 0),
                func.coalesce(tasks.c.unresolved, 0),
            )
            .join(page, page.c.id == KnowledgeBaseRow.id)
            .outerjoin(docs, docs.c.kb_id == KnowledgeBaseRow.id)
            .outerjoin(tasks, tasks.c.kb_id == KnowledgeBaseRow.id)
            .order_by(KnowledgeBaseRow.created_at.desc(), KnowledgeBaseRow.id)
        )
        items = [
            CollectionSummary(
                knowledge_base=kb_value(row),
                document_count=document_count,
                selected_count=selected_count,
                job_count=job_count,
                unresolved_count=unresolved_count,
            )
            for row, document_count, selected_count, job_count, unresolved_count in session.execute(
                summaries
            )
        ]
        return Page(items=items, total=total, limit=limit, offset=offset)


def jobs(
    repo: Repository,
    principal: Principal,
    *,
    kb_id: str | None = None,
    document_id: str | None = None,
    collection_kind: Literal["knowledge", "parse", "all"] = "all",
    status: str | None = None,
    kind: str | None = None,
    search: str = "",
    limit: int = 30,
    offset: int = 0,
) -> Page[WorkspaceJob]:
    """全局/单集合任务采用同一授权快照，允许筛选全部历史而非前百项。"""
    _page(limit, offset)
    if document_id is not None and re.fullmatch(r"[0-9a-f]{32}", document_id) is None:
        raise DomainError("invalid_request", "资料身份格式无效")
    if status and status not in {
        "queued",
        "running",
        "retry_wait",
        "cancel_requested",
        "cancelled",
        "succeeded",
        "failed",
        "reconciling",
    }:
        raise DomainError("invalid_request", "任务状态无效")
    if kind and kind not in {"parse", "ingest", "build", "evaluate", "delete"}:
        raise DomainError("invalid_request", "任务类型无效")
    with repo.transaction() as session:
        _snapshot(session)
        ids = _visible(session, principal)
        if kb_id is not None:
            repo.scope_in_transaction(session, principal, kb_id)
            ids = select(KnowledgeBaseRow.id).where(KnowledgeBaseRow.id == kb_id)
        stmt = (
            select(JobRow, KnowledgeBaseRow.name, KnowledgeBaseRow.kind, DocumentRow.name)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == JobRow.kb_id)
            .outerjoin(
                DocumentRow,
                (DocumentRow.id == JobRow.document_id)
                & (DocumentRow.tenant_id == JobRow.tenant_id),
            )
            .where(
                JobRow.tenant_id == principal.tenant_id,
                JobRow.kb_id.in_(ids),
                JobRow.kind != "import_preview",
            )
        )
        if document_id is not None:
            stmt = stmt.where(JobRow.document_id == document_id)
        if collection_kind != "all":
            stmt = stmt.where(KnowledgeBaseRow.kind == collection_kind)
        if status:
            stmt = stmt.where(JobRow.status == status)
        if kind:
            stmt = stmt.where(JobRow.kind == kind)
        if search:
            stmt = stmt.where(
                or_(
                    JobRow.id.icontains(search, autoescape=True),
                    KnowledgeBaseRow.name.icontains(search, autoescape=True),
                    DocumentRow.name.icontains(search, autoescape=True),
                )
            )
        total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        items = [
            WorkspaceJob.model_validate(
                {
                    "job": job_value(row),
                    "kb_name": name,
                    "collection_kind": collection,
                    "document_name": document,
                }
            )
            for row, name, collection, document in session.execute(
                stmt.order_by(JobRow.created_at.desc(), JobRow.id).limit(limit).offset(offset)
            )
        ]
        return Page(items=items, total=total, limit=limit, offset=offset)


def documents(
    repo: Repository,
    principal: Principal,
    kb_id: str,
    *,
    search: str = "",
    selected: bool | None = None,
    state: DocumentListStatus | None = None,
    limit: int = 30,
    offset: int = 0,
) -> DocumentPage:
    """全库筛选及计数在分页前执行，线上状态独立于当前资料版本。"""
    _page(limit, offset)
    with repo.transaction() as session:
        _snapshot(session)
        kb = repo.scope_in_transaction(session, principal, kb_id)
        latest_job = (
            select(JobRow.status, JobRow.updated_at)
            .where(
                JobRow.document_id == DocumentRow.id,
                JobRow.tenant_id == principal.tenant_id,
                JobRow.kb_id == kb_id,
                JobRow.kind == "parse",
            )
            .order_by(JobRow.created_at.desc(), JobRow.id.desc())
            .limit(1)
            .correlate(DocumentRow)
            .lateral()
        )
        result_current = and_(
            DocumentRow.result_id.is_not(None),
            DocumentRow.result_id == DocumentRow.selected_result_id,
            DocumentRow.selected_metadata == DocumentRow.attributes,
        )
        activation_status = (
            select(JobRow.status)
            .where(
                JobRow.tenant_id == principal.tenant_id,
                JobRow.kb_id == kb_id,
                JobRow.kind == "build",
                JobRow.payload["activate"].as_boolean().is_(True),
            )
            .order_by(JobRow.created_at.desc(), JobRow.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        status = case(
            (
                latest_job.c.status.in_(
                    ["queued", "running", "retry_wait", "reconciling", "cancel_requested"]
                ),
                "processing",
            ),
            (latest_job.c.status.in_(["failed", "cancelled"]), "failed"),
            (DocumentRow.result_id.is_(None), "failed"),
            (literal(kb.kind == "parse"), "saved"),
            (
                and_(
                    DocumentRow.activation_mode.is_not(None),
                    activation_status.in_(["failed", "cancelled"]),
                ),
                "failed",
            ),
            (DocumentRow.activation_mode.is_not(None), "activating"),
            (
                and_(
                    result_current,
                    DocumentRow.indexed_result_id == DocumentRow.result_id,
                    DocumentRow.indexed_revision == DocumentRow.selected_revision,
                ),
                "live",
            ),
            (result_current, "ready"),
            (
                and_(
                    DocumentRow.selected_result_id.is_(None),
                    DocumentRow.indexed_result_id == DocumentRow.result_id,
                ),
                "live",
            ),
            (ResultRow.review_status == "approved", "saved"),
            else_="review",
        ).label("list_status")
        stmt = (
            select(DocumentRow, status)
            .outerjoin(latest_job, text("true"))
            .outerjoin(
                ResultRow,
                and_(
                    ResultRow.document_id == DocumentRow.id,
                    ResultRow.result_id == DocumentRow.result_id,
                    ResultRow.tenant_id == principal.tenant_id,
                ),
            )
            .where(
                DocumentRow.tenant_id == principal.tenant_id,
                DocumentRow.kb_id == kb_id,
                DocumentRow.deleted.is_(False),
            )
        )
        if search:
            stmt = stmt.where(DocumentRow.name.icontains(search, autoescape=True))
        if selected is not None:
            stmt = stmt.where(
                DocumentRow.selected_result_id.is_not(None)
                if selected
                else DocumentRow.selected_result_id.is_(None)
            )
        grouped = stmt.with_only_columns(status).subquery()
        counts = dict(
            session.execute(
                select(grouped.c.list_status, func.count()).group_by(grouped.c.list_status)
            )
            .tuples()
            .all()
        )
        total = counts.get(state, 0) if state is not None else sum(counts.values())
        if state is not None:
            stmt = stmt.where(status == state)
        activity = func.greatest(
            DocumentRow.created_at,
            ResultRow.created_at,
            ResultRow.reviewed_at,
            latest_job.c.updated_at,
            select(func.max(AuditRow.created_at))
            .where(
                AuditRow.tenant_id == principal.tenant_id,
                AuditRow.kb_id == kb_id,
                AuditRow.target_id == DocumentRow.id,
            )
            .correlate(DocumentRow)
            .scalar_subquery(),
        )
        return DocumentPage(
            items=[
                DocumentListItem.model_validate(
                    {
                        **document_value(row).model_dump(),
                        "list_status": item_status,
                        "activity_at": date,
                    }
                )
                for row, item_status, date in session.execute(
                    stmt.add_columns(activity)
                    .order_by(DocumentRow.created_at.desc(), DocumentRow.id)
                    .limit(limit)
                    .offset(offset)
                )
            ],
            total=total,
            counts=counts,
            limit=limit,
            offset=offset,
        )


def select_result(
    repo: Repository, principal: Principal, document_id: str, request: SelectionRequest
) -> Document:
    """选用仅保存结果身份与属性快照，不改写人工审核事实；相同选用保持幂等。"""
    with repo.transaction() as session:
        document = repo.document_in_transaction(session, principal, document_id)
        kb = repo.scope_in_transaction(session, principal, document.kb_id, write=True, lock=True)
        doc = repo.document_in_transaction(session, principal, document_id, write=True, lock=True)
        if kb.kind != "knowledge":
            raise DomainError("invalid_request", "请将独立解析资料选用到知识库")
        if doc.revision != request.expected_document_revision:
            raise DomainError("version_conflict", "资料已变化，请刷新后选择")
        if (request.confirm_content or request.activate) and (
            request.result_id is None or request.result_id != doc.result_id
        ):
            raise DomainError("version_conflict", "请核对当前解析版本后确认")
        if request.result_id is not None:
            result = session.get(ResultRow, (doc.id, request.result_id))
            if result is None or result.tenant_id != principal.tenant_id:
                raise DomainError("not_found", "选用的解析结果不属于此资料")
            if request.confirm_content and result.review_status != "approved":
                result.review_status = "approved"
                result.reviewed_by = principal.subject
                result.reviewed_at = datetime.now().astimezone()
                repo.audit_in_transaction(
                    session, principal, "result.review:approved", result.result_id, kb_id=doc.kb_id
                )
            elif (
                doc.selected_result_id == request.result_id
                and doc.selected_metadata == doc.attributes
            ):
                if not request.activate:
                    return document_value(doc)
                if doc.activation_mode == "manual":
                    repo.enqueue_activation(session, principal, kb.id)
                    return document_value(doc)
        if request.result_id is None and doc.selected_result_id is None:
            return document_value(doc)
        doc.revision += 1
        doc.selected_result_id = request.result_id
        doc.selected_revision = doc.revision if request.result_id else None
        doc.selected_metadata = dict(doc.attributes) if request.result_id else None
        doc.activation_mode = "manual" if request.activate else None
        kb.revision += 1
        if request.activate:
            repo.enqueue_activation(session, principal, kb.id)
        repo.audit_in_transaction(session, principal, "document.select", doc.id, kb_id=doc.kb_id)
        return document_value(doc)


def adopt(
    repo: Repository,
    principal: Principal,
    kb_id: str,
    request: AdoptionRequest,
    *,
    source_asset_id: str,
) -> Document:
    """双向授权后复用资产；目标审核从待审核开始，不复制或改写来源审核事实。"""
    with repo.transaction() as session:
        # 固定KB锁顺序，避免两个知识库交叉采用死锁。
        source = repo.document_in_transaction(session, principal, request.source_document_id)
        for identity in sorted({kb_id, source.kb_id}):
            repo.scope_in_transaction(
                session, principal, identity, write=identity == kb_id, lock=True
            )
        kb = repo.scope_in_transaction(session, principal, kb_id, write=True)
        source = repo.document_in_transaction(
            session, principal, request.source_document_id, lock=True
        )
        if kb.kind != "knowledge":
            raise DomainError("invalid_request", "已存结果只能采用到知识库")
        result = session.get(ResultRow, (source.id, request.result_id))
        if result is None or result.tenant_id != principal.tenant_id:
            raise DomainError("not_found", "解析版本不存在")
        if source_asset_id not in result.asset_ids:
            raise DomainError("invalid_artifact", "原件不属于所选解析结果")
        validate_metadata(
            request.metadata, [MetadataField.model_validate(value) for value in kb.fields]
        )
        # 相同版本重复采用返回既有资料，避免按钮重试产生重复知识。
        previous = session.scalar(
            select(DocumentRow)
            .where(
                DocumentRow.tenant_id == principal.tenant_id,
                DocumentRow.kb_id == kb_id,
                DocumentRow.result_id == request.result_id,
                DocumentRow.deleted.is_(False),
            )
            .order_by(DocumentRow.id)
            .limit(1)
        )
        if previous is not None:
            if previous.attributes != request.metadata:
                raise DomainError("version_conflict", "此解析版本已采用，请修改现有资料属性")
            return document_value(previous)
        row = DocumentRow(
            id=uuid4().hex,
            tenant_id=principal.tenant_id,
            kb_id=kb_id,
            name=source.name,
            processing=dict(source.processing),
            source_asset_id=source_asset_id,
            result_id=request.result_id,
            revision=1,
            selected_result_id=request.result_id,
            selected_revision=1,
            selected_metadata=dict(request.metadata),
            attributes=request.metadata,
        )
        session.add(row)
        session.flush()
        session.add(
            ResultRow(
                document_id=row.id,
                tenant_id=principal.tenant_id,
                result_id=request.result_id,
                revision=1,
                previous_result_id=None,
                asset_ids=result.asset_ids,
                review_status="pending",
                reviewed_by=None,
                reviewed_at=None,
            )
        )
        for asset_id in result.asset_ids:
            session.add(
                AssetReferenceRow(
                    document_id=row.id, tenant_id=principal.tenant_id, asset_id=asset_id
                )
            )
        kb.revision += 1
        repo.audit_in_transaction(
            session, principal, "document.adopt:" + source.id, row.id, kb_id=kb_id
        )
        return document_value(row)


def get_service(repo: Repository, principal: Principal, kb_id: str) -> ServiceDraft:
    """未保存时返回明确revision0默认草稿，不伪造已持久配置。"""
    with repo.transaction() as session:
        repo.scope_in_transaction(session, principal, kb_id)
        row = session.get(ServiceRow, kb_id)
        return ServiceDraft(
            kb_id=kb_id,
            revision=row.revision if row else 0,
            config=ServiceConfig.model_validate(row.config) if row else ServiceConfig(),
            updated_at=row.updated_at if row else None,
        )


def update_service(
    repo: Repository, principal: Principal, kb_id: str, request: ServiceUpdate
) -> ServiceDraft:
    """草稿变动推进KB版本；旧候选必须重建才允许发布新的配置。"""
    with repo.transaction() as session:
        kb = repo.scope_in_transaction(session, principal, kb_id, write=True, lock=True)
        if kb.kind != "knowledge":
            raise DomainError("invalid_request", "独立解析集合没有检索服务")
        validate_service_filters(
            request.config, [MetadataField.model_validate(item) for item in kb.fields]
        )
        row = session.get(ServiceRow, kb_id)
        if (row.revision if row else 0) != request.expected_revision:
            raise DomainError("version_conflict", "服务草稿已变化")
        if row is not None and row.config == request.config.model_dump(mode="json"):
            return ServiceDraft(
                kb_id=kb_id, revision=row.revision, config=request.config, updated_at=row.updated_at
            )
        if row is None:
            row = ServiceRow(
                kb_id=kb_id,
                tenant_id=principal.tenant_id,
                revision=1,
                config=request.config.model_dump(mode="json"),
            )
            session.add(row)
        else:
            row.revision += 1
            row.config = request.config.model_dump(mode="json")
            row.updated_at = (
                session.scalar(select(func.clock_timestamp())) or datetime.now().astimezone()
            )
        kb.revision += 1
        repo.audit_in_transaction(session, principal, "service.update", kb_id, kb_id=kb_id)
        session.flush()
        return ServiceDraft(
            kb_id=kb_id, revision=row.revision, config=request.config, updated_at=row.updated_at
        )


def _trace(row: QueryTraceRow) -> QueryTrace:
    """序列化不含片段全文的受控请求事实。"""
    return QueryTrace.model_validate(
        {
            "trace_id": row.id,
            "kb_id": row.kb_id,
            "subject": row.subject,
            "target_id": row.target_id,
            "target_kind": row.target_kind,
            "request": row.request,
            "outcome": row.outcome,
            "created_at": row.created_at,
        }
    )


def record_trace(
    repo: Repository,
    principal: Principal,
    kb_id: str,
    target_id: str,
    target_kind: str,
    request: dict[str, JsonValue],
    outcome: dict[str, JsonValue],
) -> QueryTrace:
    """先提交可查记录再返回trace身份，数据库不可用不能伪造trace。"""
    if len(json.dumps([request, outcome]).encode()) > 64 * 1024:
        raise DomainError("invalid_request", "请求记录超过预算")
    with repo.transaction() as session:
        repo.scope_in_transaction(session, principal, kb_id)
        row = QueryTraceRow(
            id=uuid4().hex,
            tenant_id=principal.tenant_id,
            kb_id=kb_id,
            subject=principal.subject,
            target_id=target_id,
            target_kind=target_kind,
            request=request,
            outcome=outcome,
        )
        session.add(row)
        session.flush()
        return _trace(row)


def traces(
    repo: Repository, principal: Principal, kb_id: str, *, limit: int = 30, offset: int = 0
) -> Page[QueryTrace]:
    """管理员可查KB请求；其他主体只能查自己，服务密钥不能读别人的问题。"""
    _page(limit, offset)
    with repo.transaction() as session:
        _snapshot(session)
        repo.scope_in_transaction(session, principal, kb_id)
        stmt = select(QueryTraceRow).where(
            QueryTraceRow.tenant_id == principal.tenant_id, QueryTraceRow.kb_id == kb_id
        )
        if "admin" not in principal.roles:
            stmt = stmt.where(QueryTraceRow.subject == principal.subject)
        total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        return Page(
            items=[
                _trace(row)
                for row in session.scalars(
                    stmt.order_by(QueryTraceRow.created_at.desc(), QueryTraceRow.id)
                    .limit(limit)
                    .offset(offset)
                )
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


def get_trace(repo: Repository, principal: Principal, trace_id: str) -> QueryTrace:
    """单记录读取保留租户、KB和调用者三重范围。"""
    with repo.transaction() as session:
        row = session.get(QueryTraceRow, trace_id)
        if (
            row is None
            or row.tenant_id != principal.tenant_id
            or ("admin" not in principal.roles and row.subject != principal.subject)
        ):
            raise DomainError("not_found", "请求记录不存在")
        repo.scope_in_transaction(session, principal, row.kb_id)
        return _trace(row)


def evaluation_value(row: CandidateEvaluationRow) -> CandidateEvaluation:
    """评测包含逐题可核查指标和实际来源，不只返回宏平均。"""
    return CandidateEvaluation(
        evaluation_id=row.id,
        kb_id=row.kb_id,
        build_id=row.build_id,
        name=row.name,
        metrics=row.metrics,
        details=row.details,
        created_at=row.created_at,
    )


def evaluations(repo: Repository, principal: Principal, kb_id: str) -> list[CandidateEvaluation]:
    """返回最近一百个固定候选评测，不修改发布指针。"""
    with repo.transaction() as session:
        repo.scope_in_transaction(session, principal, kb_id)
        return [
            evaluation_value(row)
            for row in session.scalars(
                select(CandidateEvaluationRow)
                .where(
                    CandidateEvaluationRow.tenant_id == principal.tenant_id,
                    CandidateEvaluationRow.kb_id == kb_id,
                )
                .order_by(CandidateEvaluationRow.created_at.desc())
                .limit(100)
            )
        ]


def review_result(
    repo: Repository,
    principal: Principal,
    document_id: str,
    result_id: str,
    expected_document_revision: int,
    status: Literal["pending", "approved"],
) -> Document:
    """审核按结果身份保存；撤回审核同时撤回下一候选的选用，不覆盖已发布快照。"""
    with repo.transaction() as session:
        doc = repo.document_in_transaction(session, principal, document_id, write=True, lock=True)
        kb = repo.scope_in_transaction(session, principal, doc.kb_id, write=True, lock=True)
        if doc.revision != expected_document_revision:
            raise DomainError("version_conflict", "资料已变化，请刷新后审核")
        version = session.get(ResultRow, (doc.id, result_id))
        if version is None or version.tenant_id != principal.tenant_id:
            raise DomainError("not_found", "解析结果不存在")
        if version.review_status == status:
            return document_value(doc)
        version.review_status = status
        version.reviewed_by = principal.subject
        version.reviewed_at = datetime.now().astimezone()
        if status == "pending" and doc.selected_result_id == result_id:
            doc.selected_result_id, doc.selected_revision, doc.selected_metadata = None, None, None
            doc.activation_mode = None
        doc.revision += 1
        kb.revision += 1
        repo.audit_in_transaction(
            session, principal, "result.review:" + status, result_id, kb_id=doc.kb_id
        )
        return document_value(doc)
