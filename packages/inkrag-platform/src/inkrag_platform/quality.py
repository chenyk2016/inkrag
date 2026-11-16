"""人工标注的不可变版本与来源校验，质量指标只解释人工标注范围。"""

from uuid import uuid4

from sqlalchemy import select

from .database import ResultRow, SampleRow
from .models import DomainError, EvaluationSample, ExpectedSource, Principal
from .operations import Runtime, document_view
from .repository import Repository
from .workspace_models import QualitySampleRequest


def save_quality_sample(
    repo: Repository, principal: Principal, kb_id: str, request: QualitySampleRequest
) -> EvaluationSample:
    """冻结人工标注版本；来源必须在同一可见库且解析结果身份真实存在。"""
    with repo.transaction() as session:
        repo.scope_in_transaction(session, principal, kb_id, write=True, lock=True)
        identities = set(request.expected_document_ids) | {
            item.document_id for item in request.expected_sources
        }
        source_documents = {
            identity: repo.document_in_transaction(session, principal, identity)
            for identity in identities
        }
        if any(doc.kb_id != kb_id for doc in source_documents.values()):
            raise DomainError("invalid_request", "人工金标必须属于同一知识库")
        sources: list[ExpectedSource] = []
        for item in request.expected_sources:
            result_id = item.result_id or source_documents[item.document_id].result_id
            if result_id is None or session.get(ResultRow, (item.document_id, result_id)) is None:
                raise DomainError("not_found", "金标引用的保存解析版本不存在")
            sources.append(item.model_copy(update={"result_id": result_id}))
        previous = session.get(SampleRow, request.supersedes_id) if request.supersedes_id else None
        if request.supersedes_id and (
            previous is None or previous.tenant_id != principal.tenant_id or previous.kb_id != kb_id
        ):
            raise DomainError("not_found", "替换的标注版本不存在")
        if (
            previous is not None
            and session.scalar(select(SampleRow.id).where(SampleRow.supersedes_id == previous.id))
            is not None
        ):
            raise DomainError("version_conflict", "标注已有新版本，请刷新后修改")
        row = SampleRow(
            id=uuid4().hex,
            tenant_id=principal.tenant_id,
            kb_id=kb_id,
            query=request.query,
            expected_document_ids=sorted(identities),
            expected_sources=[item.model_dump(mode="json") for item in sources],
            no_answer=request.no_answer,
            annotation_revision=previous.annotation_revision + 1 if previous else 1,
            supersedes_id=request.supersedes_id,
        )
        session.add(row)
        session.flush()
        repo.audit_in_transaction(session, principal, "quality_sample.create", row.id, kb_id=kb_id)
        return EvaluationSample(
            sample_id=row.id,
            kb_id=kb_id,
            query=row.query,
            expected_document_ids=row.expected_document_ids,
            expected_sources=sources,
            no_answer=row.no_answer,
            annotation_revision=row.annotation_revision,
            supersedes_id=row.supersedes_id,
            created_at=row.created_at,
        )


def create_quality_sample(
    runtime: Runtime, principal: Principal, kb_id: str, request: QualitySampleRequest
) -> EvaluationSample:
    """对细粒度金标验证原生元素/页码真实存在，再在同一可信KB保存标注。"""
    normalized: list[ExpectedSource] = []
    groups: dict[tuple[str, str | None], list[ExpectedSource]] = {}
    for source in request.expected_sources:
        groups.setdefault((source.document_id, source.result_id), []).append(source)
    byte_budget = 0
    for (document_id, result_id), sources in groups.items():
        view = document_view(runtime, principal, document_id, result_id)
        if view.document.kb_id != kb_id or view.result is None:
            raise DomainError("invalid_request", "证据来源必须是本库已保存的解析版本")
        byte_budget += len(view.result.model_dump_json().encode())
        if byte_budget > 64 * 1024 * 1024:
            raise DomainError("invalid_request", "单个问题的标注来源超过64MiB验证预算")
        refs = {
            item.self_ref
            for item in [
                *view.result.document.texts,
                *view.result.document.tables,
                *view.result.document.pictures,
            ]
        }
        for source in sources:
            if source.element_ref is not None and source.element_ref not in refs:
                raise DomainError("invalid_request", "标注的元素在此解析版本中不存在")
            if source.page is not None and source.page not in view.result.document.pages:
                raise DomainError("invalid_request", "标注的页码在此解析版本中不存在")
            normalized.append(source.model_copy(update={"result_id": view.result.result_id}))
    return save_quality_sample(
        runtime.repo, principal, kb_id, request.model_copy(update={"expected_sources": normalized})
    )
