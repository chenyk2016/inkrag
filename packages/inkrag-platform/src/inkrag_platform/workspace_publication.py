"""发布前审阅：PG对冻结清单求差并分页，不将两份完整清单载入应用。"""

from typing import Literal

from pydantic import Field
from sqlalchemy import Text, func, select, text

from .database import BuildRow, JobRow, ReleaseRow
from .models import MAX_BUILD_DOCUMENTS, MAX_MANIFEST_BYTES, DomainError, Principal, StrictModel
from .repository import Repository
from .workspace_models import ServiceConfig


class PublicationChange(StrictModel):
    """removed来自当前线上清单，其余版本身份来自候选，不使用资料当前指针替代。"""

    document_id: str
    name: str | None
    result_id: str
    revision: int
    change: Literal["added", "changed", "removed", "unchanged"]


class PublicationSummary(StrictModel):
    """全量差异计数，与当页变化采用同一PG快照。"""

    added: int
    changed: int
    removed: int
    unchanged: int


class PublicationPreview(StrictModel):
    """确认界面必须提交本次看到的expected_release_id，防止覆盖期间其他人的发布。"""

    build_id: str
    kb_id: str
    expected_release_id: str | None
    summary: PublicationSummary
    service_config: ServiceConfig
    strategy_changed: bool
    publishable: bool
    stale_reasons: list[str]
    items: list[PublicationChange]
    total: int = Field(ge=0)
    limit: int
    offset: int


_DIFF_SQL = """
WITH candidate AS (
    SELECT value AS entry FROM ip_builds b
    CROSS JOIN LATERAL jsonb_array_elements(b.manifest)
    WHERE b.id=:build_id AND b.tenant_id=:tenant_id AND b.kb_id=:kb_id
), published AS (
    SELECT value AS entry FROM ip_builds b
    CROSS JOIN LATERAL jsonb_array_elements(b.manifest)
    WHERE b.id=:current_build_id AND b.tenant_id=:tenant_id AND b.kb_id=:kb_id
), differences AS MATERIALIZED (
    SELECT coalesce(c.entry->>'document_id',p.entry->>'document_id') AS document_id,
        coalesce(c.entry,p.entry) AS entry,
        CASE WHEN p.entry IS NULL THEN 'added' WHEN c.entry IS NULL THEN 'removed'
            WHEN c.entry=p.entry THEN 'unchanged' ELSE 'changed' END AS change
    FROM candidate c FULL OUTER JOIN published p
        ON c.entry->>'document_id'=p.entry->>'document_id'
), summary AS (
    SELECT count(*) FILTER(WHERE change='added') AS added,
        count(*) FILTER(WHERE change='changed') AS changed,
        count(*) FILTER(WHERE change='removed') AS removed,
        count(*) FILTER(WHERE change='unchanged') AS unchanged,
        count(*) AS total FROM differences
), page AS (
    SELECT x.document_id,d.name,x.entry->>'result_id' AS result_id,
        (x.entry->>'revision')::integer AS revision,x.change
    FROM differences x LEFT JOIN ip_documents d ON d.id=x.document_id
        AND d.tenant_id=:tenant_id AND d.kb_id=:kb_id
    ORDER BY CASE x.change WHEN 'added' THEN 0 WHEN 'changed' THEN 1
        WHEN 'removed' THEN 2 ELSE 3 END,x.document_id
    LIMIT :limit OFFSET :offset
) SELECT s.*,coalesce((SELECT jsonb_agg(to_jsonb(p) ORDER BY CASE p.change WHEN 'added' THEN 0 WHEN 'changed' THEN 1 WHEN 'removed' THEN 2 ELSE 3 END,p.document_id) FROM page p),'[]'::jsonb) AS items
FROM summary s
"""


def publication_preview(
    repo: Repository, principal: Principal, build_id: str, *, limit: int = 30, offset: int = 0
) -> PublicationPreview:
    """授权、当前发布、汇总和当页来自一次只读快照；只读取小型构建头和分页差异。"""
    if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
        raise DomainError("invalid_request", "发布预览分页无效")
    with repo.transaction() as session:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        header = session.execute(
            select(
                BuildRow.kb_id,
                BuildRow.service_config,
                BuildRow.model_revision,
                BuildRow.field_schema,
                BuildRow.kb_revision,
                BuildRow.authz_epoch,
                BuildRow.status,
                BuildRow.job_id,
                func.jsonb_array_length(BuildRow.manifest),
                func.octet_length(BuildRow.manifest.cast(Text)),
            ).where(BuildRow.id == build_id, BuildRow.tenant_id == principal.tenant_id)
        ).one_or_none()
        if header is None:
            raise DomainError("not_found", "候选构建不存在")
        (
            kb_id,
            config,
            model_revision,
            field_schema,
            kb_revision,
            authz_epoch,
            status,
            job_id,
            document_count,
            manifest_bytes,
        ) = header
        kb = repo.scope_in_transaction(session, principal, kb_id)
        job = session.get(JobRow, job_id)
        incremental = job is not None and job.payload.get("incremental") is True
        base_release_id = (
            job.payload.get("base_release_id") if job and incremental else kb.active_release_id
        )
        if document_count > MAX_BUILD_DOCUMENTS or manifest_bytes > MAX_MANIFEST_BYTES:
            raise DomainError("build_budget_exceeded", "发布预览清单超过安全预算")
        current_build_id: str | None = None
        current_config: ServiceConfig | None = None
        current_model_revision: str | None = None
        current_field_schema: dict[str, str] | None = None
        if kb.active_release_id is not None:
            current = session.execute(
                select(
                    BuildRow.id,
                    BuildRow.service_config,
                    BuildRow.model_revision,
                    BuildRow.field_schema,
                    func.jsonb_array_length(BuildRow.manifest),
                    func.octet_length(BuildRow.manifest.cast(Text)),
                )
                .join(ReleaseRow, ReleaseRow.build_id == BuildRow.id)
                .where(
                    ReleaseRow.id == kb.active_release_id,
                    ReleaseRow.tenant_id == principal.tenant_id,
                    ReleaseRow.kb_id == kb_id,
                    BuildRow.tenant_id == principal.tenant_id,
                    BuildRow.kb_id == kb_id,
                )
            ).one_or_none()
            if current is None:
                raise DomainError("database_unavailable", "当前发布引用不完整")
            (
                current_build_id,
                saved_config,
                current_model_revision,
                current_field_schema,
                current_count,
                current_bytes,
            ) = current
            if current_count > MAX_BUILD_DOCUMENTS or current_bytes > MAX_MANIFEST_BYTES:
                raise DomainError("build_budget_exceeded", "当前发布清单超过发布预览安全预算")
            current_config = ServiceConfig.model_validate(saved_config)
        frozen_config = ServiceConfig.model_validate(config)
        result = (
            session.execute(
                text(_DIFF_SQL),
                {
                    "tenant_id": principal.tenant_id,
                    "kb_id": kb_id,
                    "build_id": build_id,
                    "current_build_id": current_build_id,
                    "limit": limit,
                    "offset": offset,
                },
            )
            .mappings()
            .one()
        )
        summary = PublicationSummary.model_validate(
            {key: result[key] for key in ("added", "changed", "removed", "unchanged")}
        )
        stale_reasons: list[str] = []
        if status != "ready":
            stale_reasons.append("build_not_ready")
        if kb.revision != kb_revision:
            stale_reasons.append("knowledge_base_changed")
        if kb.authz_epoch != authz_epoch:
            stale_reasons.append("authorization_changed")
        if incremental and base_release_id != kb.active_release_id:
            stale_reasons.append("published_version_changed")
        return PublicationPreview.model_validate(
            {
                "build_id": build_id,
                "kb_id": kb_id,
                "expected_release_id": base_release_id,
                "summary": summary,
                "service_config": frozen_config,
                "strategy_changed": current_config != frozen_config
                or current_model_revision != model_revision
                or current_field_schema != field_schema,
                "publishable": not stale_reasons,
                "stale_reasons": stale_reasons,
                "items": result["items"],
                "total": result["total"],
                "limit": limit,
                "offset": offset,
            }
        )
