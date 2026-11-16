"""生产事务事实仓库：租户边界、版本条件、任务租约及发布只有一份规则。"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from inkparse import ParseError
from inkparse.adapters import ParserEngine, check_engine_format
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import Text, func, or_, select, text, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, defer

from .database import (
    ApiKeyRow,
    AssetReferenceRow,
    AttemptRow,
    AuditRow,
    BuildRow,
    Database,
    DocumentRow,
    EvaluationRow,
    JobRow,
    KnowledgeBaseRow,
    OutboxRow,
    ReleaseRow,
    ResultRow,
    SampleRow,
    ServiceRow,
    TenantRow,
)
from .models import (
    MAX_BUILD_DOCUMENTS,
    MAX_MANIFEST_BYTES,
    ApiKeyInfo,
    AuditEvent,
    Build,
    BuildDocument,
    Document,
    DomainError,
    Evaluation,
    EvaluationSample,
    ExpectedSource,
    IssuedKey,
    Job,
    JobKind,
    KnowledgeBase,
    MetadataField,
    OutboxEvent,
    Principal,
    Release,
    ResultVersion,
    missing_required_fields,
    validate_metadata,
)
from .workspace_models import ServiceConfig, validate_service_filters


def _now(session: Session) -> datetime:
    """所有租约使用数据库时钟，避免跨节点主机时钟决定执行权。"""
    value = session.scalar(select(func.clock_timestamp()))
    return TypeAdapter(datetime).validate_python(value, strict=True)


def job_value(row: JobRow) -> Job:
    """将数据库状态重新验证为不可变领域结果，不暴露ORM连接。"""
    return Job.model_validate(
        {
            "job_id": row.id,
            "operation_id": row.operation_id,
            "tenant_id": row.tenant_id,
            "kb_id": row.kb_id,
            "document_id": row.document_id,
            "kind": row.kind,
            "status": row.status,
            "stage": row.stage,
            "payload": row.payload,
            "result": row.result,
            "expected_result_id": row.expected_result_id,
            "expected_document_revision": row.expected_document_revision,
            "attempt": row.attempt,
            "lease_epoch": row.lease_epoch,
            "lease_owner": row.lease_owner,
            "lease_until": row.lease_until,
            "error_code": row.error_code,
            "error_diagnostic": row.error_diagnostic,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def document_value(row: DocumentRow) -> Document:
    """序列化资料指针，全文和二进制不保存在本表。"""
    return Document(
        document_id=row.id,
        tenant_id=row.tenant_id,
        kb_id=row.kb_id,
        name=row.name,
        source_asset_id=row.source_asset_id,
        result_id=row.result_id,
        revision=row.revision,
        indexed_result_id=row.indexed_result_id,
        indexed_revision=row.indexed_revision,
        selected_result_id=row.selected_result_id,
        selected_revision=row.selected_revision,
        selected_metadata=row.selected_metadata,
        metadata=row.attributes,
        parser_engine=row.parser_engine,
        processing=row.processing,
        enabled=row.enabled,
        deleted=row.deleted,
        created_at=row.created_at,
    )


def kb_value(row: KnowledgeBaseRow) -> KnowledgeBase:
    """返回租户KB事实与当前发布指针。"""
    return KnowledgeBase(
        kb_id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        revision=row.revision,
        authz_epoch=row.authz_epoch,
        retrieval_epoch=row.retrieval_epoch,
        active_release_id=row.active_release_id,
        auto_activate=row.auto_activate,
        created_at=row.created_at,
        fields=[MetadataField.model_validate(value) for value in row.fields],
        kind=TypeAdapter(Literal["knowledge", "parse"]).validate_python(row.kind),
    )


def _version(row: ResultRow) -> ResultVersion:
    """返回解析结果的不可变版本元数据。"""
    return ResultVersion(
        document_id=row.document_id,
        result_id=row.result_id,
        revision=row.revision,
        previous_result_id=row.previous_result_id,
        asset_ids=row.asset_ids,
        review_status=TypeAdapter(Literal["pending", "approved"]).validate_python(
            row.review_status
        ),
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        created_at=row.created_at,
    )


def _build(row: BuildRow) -> Build:
    """把JSON清单还原成严格输入版本集合。"""
    return Build.model_validate(
        {
            "build_id": row.id,
            "tenant_id": row.tenant_id,
            "kb_id": row.kb_id,
            "job_id": row.job_id,
            "index_name": row.index_name,
            "model_revision": row.model_revision,
            "kb_revision": row.kb_revision,
            "authz_epoch": row.authz_epoch,
            "manifest": row.manifest,
            "document_count": len(row.manifest),
            "manifest_bytes": len(json.dumps(row.manifest, ensure_ascii=False).encode()),
            "manifest_included": True,
            "field_schema": row.field_schema,
            "service_config": row.service_config,
            "status": row.status,
            "created_at": row.created_at,
        }
    )


_BUILD_SUMMARY = (
    "SELECT id AS build_id,tenant_id,kb_id,job_id,index_name,model_revision,kb_revision,authz_epoch,"
    "field_schema,service_config,status,created_at,'[]'::jsonb AS manifest,false AS manifest_included,"
    "jsonb_array_length(manifest) AS document_count,octet_length(manifest::text) AS manifest_bytes "
    "FROM ip_builds WHERE tenant_id=:tenant_id "
)


def _release(row: ReleaseRow) -> Release:
    """发布引用以PG物理generation事实为准。"""
    return Release(
        release_id=row.id,
        kb_id=row.kb_id,
        build_id=row.build_id,
        version=row.version,
        index_name=row.index_name,
        model_revision=row.model_revision,
        authz_epoch=row.authz_epoch,
        created_at=row.created_at,
    )


def require_role(principal: Principal, *, write: bool = False, admin: bool = False) -> None:
    """角色来自可信身份适配；无角色或只读调用方不能修改业务事实。"""
    allowed = (
        {"admin"} if admin else {"admin", "editor"} if write else {"admin", "editor", "viewer"}
    )
    if not allowed.intersection(principal.roles):
        raise DomainError("forbidden", "当前身份无权执行此操作")


class Repository(Database):
    """PG业务仓库；用户方法强制租户/KB，worker方法只供受信执行面调用。"""

    def _scope(
        self,
        session: Session,
        principal: Principal,
        kb_id: str,
        *,
        write: bool = False,
        lock: bool = False,
    ) -> KnowledgeBaseRow:
        """在每条业务路径验证可信租户及KB范围，锁仅覆盖短事务。"""
        require_role(principal, write=write)
        if principal.kb_ids is not None and kb_id not in principal.kb_ids:
            raise DomainError("not_found", "知识库不存在")
        statement = (
            select(KnowledgeBaseRow)
            .join(TenantRow, TenantRow.id == KnowledgeBaseRow.tenant_id)
            .where(
                KnowledgeBaseRow.id == kb_id,
                KnowledgeBaseRow.tenant_id == principal.tenant_id,
                TenantRow.active.is_(True),
            )
        )
        # 业务只修改版本/配置，主键和租户键不可变；NO KEY UPDATE仍排斥业务写锁，
        # 同时允许审计外键的KEY SHARE，避免取消持有Job锁时反向等待KB。
        row = session.scalar(
            statement.with_for_update(
                of=KnowledgeBaseRow, key_share=True, read=False
            ).execution_options(populate_existing=True)
            if lock
            else statement
        )
        if row is None:
            raise DomainError("not_found", "知识库不存在")
        return row

    def _doc(
        self,
        session: Session,
        principal: Principal,
        document_id: str,
        *,
        write: bool = False,
        lock: bool = False,
    ) -> DocumentRow:
        """资料先按租户定位，再核对KB权限，不泄漏其他范围的存在性。"""
        statement = select(DocumentRow).where(
            DocumentRow.id == document_id,
            DocumentRow.tenant_id == principal.tenant_id,
            DocumentRow.deleted.is_(False),
        )
        row = session.scalar(statement)
        if row is None:
            raise DomainError("not_found", "资料不存在")
        self._scope(session, principal, row.kb_id, write=write, lock=lock)
        if lock:
            row = session.scalar(
                statement.with_for_update().execution_options(populate_existing=True)
            )
            if row is None:
                raise DomainError("not_found", "资料不存在")
        return row

    def _audit(
        self,
        session: Session,
        principal: Principal,
        action: str,
        target: str,
        *,
        kb_id: str | None = None,
    ) -> None:
        """审计与业务变更同事务，不记录正文或凭据。"""
        session.add(
            AuditRow(
                id=uuid4().hex,
                tenant_id=principal.tenant_id,
                subject=principal.subject,
                action=action,
                target_id=target,
                kb_id=kb_id,
            )
        )

    def _fail_builds(self, session: Session, job_id: str) -> None:
        """任务失权后候选不可发布；保留物理身份供审计与后续受控回收。"""
        session.execute(
            update(BuildRow)
            .where(BuildRow.job_id == job_id, BuildRow.status == "building")
            .values(status="failed")
        )

    def scope_in_transaction(
        self,
        session: Session,
        principal: Principal,
        kb_id: str,
        *,
        write: bool = False,
        lock: bool = False,
    ) -> KnowledgeBaseRow:
        """领域扩展在调用方事务中复用唯一范围规则，不另开检查事务。"""
        return self._scope(session, principal, kb_id, write=write, lock=lock)

    def document_in_transaction(
        self,
        session: Session,
        principal: Principal,
        document_id: str,
        *,
        write: bool = False,
        lock: bool = False,
    ) -> DocumentRow:
        """领域扩展锁定并校验资料，保证授权与写入使用同一快照。"""
        return self._doc(session, principal, document_id, write=write, lock=lock)

    def audit_in_transaction(
        self,
        session: Session,
        principal: Principal,
        action: str,
        target_id: str,
        *,
        kb_id: str | None = None,
    ) -> None:
        """领域扩展与业务写入原子追加审计，不允许先写后补。"""
        self._audit(session, principal, action, target_id, kb_id=kb_id)

    def leased_in_transaction(
        self,
        session: Session,
        job_id: str,
        owner: str,
        epoch: int,
    ) -> JobRow:
        """领域扩展产物提交必须在同一短事务重检租约，不能用先前读到的执行权。"""
        return self._leased(session, job_id, owner, epoch)

    def retrieval_projection(
        self,
        principal: Principal,
        kb_id: str,
        document_ids: list[str],
    ) -> tuple[int, int, list[str]]:
        """原子读取授权/检索代次及本次构建中删除或禁用的资料，旧索引也受当前开关约束。"""
        if len(document_ids) > MAX_BUILD_DOCUMENTS:
            raise DomainError("build_budget_exceeded", "本次查询的构建资料超过安全预算")
        with self.transaction() as session:
            kb = self._scope(session, principal, kb_id)
            if not document_ids:
                return kb.authz_epoch, kb.retrieval_epoch, []
            identities = list(
                session.scalars(
                    select(DocumentRow.id)
                    .where(
                        DocumentRow.tenant_id == principal.tenant_id,
                        DocumentRow.kb_id == kb_id,
                        DocumentRow.id.in_(document_ids),
                        (DocumentRow.deleted.is_(True) | DocumentRow.enabled.is_(False)),
                    )
                    .order_by(DocumentRow.id)
                )
            )
            return kb.authz_epoch, kb.retrieval_epoch, identities

    def deleted_document_ids(
        self, principal: Principal, kb_id: str, document_ids: list[str]
    ) -> tuple[int, list[str]]:
        """保留撤销投影调用契约，禁用资料同样不可进入检索结果。"""
        epoch, _, excluded = self.retrieval_projection(principal, kb_id, document_ids)
        return epoch, excluded

    def set_document_enabled(
        self, principal: Principal, document_id: str, enabled: bool
    ) -> Document:
        """原子切换检索资格；幂等重试不改代次，不改原件、解析版本或发布索引。

        锁定资料及知识库后写入状态和检索代次，查询前过滤并在返回前检查代次。
        该开关不是内容修订或权限撤销，不使既有发布、候选构建和解析任务失效。
        """
        with self.transaction() as session:
            doc = self._doc(session, principal, document_id, write=True, lock=True)
            kb = self._scope(session, principal, doc.kb_id, write=True, lock=True)
            if doc.enabled != enabled:
                doc.enabled = enabled
                kb.retrieval_epoch += 1
                self._audit(
                    session,
                    principal,
                    "document.enable" if enabled else "document.disable",
                    doc.id,
                    kb_id=doc.kb_id,
                )
                session.flush()
            return document_value(doc)

    def create_kb(self, principal: Principal, name: str, kind: str = "knowledge") -> KnowledgeBase:
        """创建租户内KB；绑定单KB的服务凭证不能扩展自己的范围。"""
        require_role(principal, write=True)
        if (
            principal.kb_ids is not None
            or not name.strip()
            or len(name) > 255
            or kind not in {"knowledge", "parse"}
        ):
            raise DomainError("invalid_request", "知识库名称或创建范围无效")
        with self.transaction() as session:
            session.execute(
                insert(TenantRow)
                .values(id=principal.tenant_id, active=True)
                .on_conflict_do_nothing(index_elements=[TenantRow.id])
            )
            tenant = session.get(TenantRow, principal.tenant_id)
            if tenant is None or not tenant.active:
                raise DomainError("forbidden", "租户已停用")
            row = KnowledgeBaseRow(
                id=uuid4().hex, tenant_id=principal.tenant_id, name=name.strip(), kind=kind
            )
            session.add(row)
            session.flush()
            self._audit(session, principal, "kb.create", row.id, kb_id=row.id)
            return kb_value(row)

    def list_kbs(self, principal: Principal) -> list[KnowledgeBase]:
        """只返回可信租户及凭证绑定的KB。"""
        require_role(principal)
        with self.transaction() as session:
            stmt = (
                select(KnowledgeBaseRow)
                .join(TenantRow, TenantRow.id == KnowledgeBaseRow.tenant_id)
                .where(
                    KnowledgeBaseRow.tenant_id == principal.tenant_id, TenantRow.active.is_(True)
                )
            )
            if principal.kb_ids is not None:
                stmt = stmt.where(KnowledgeBaseRow.id.in_(principal.kb_ids))
            return [
                kb_value(row)
                for row in session.scalars(
                    stmt.order_by(KnowledgeBaseRow.created_at.desc()).limit(100)
                )
            ]

    def get_kb(self, principal: Principal, kb_id: str) -> KnowledgeBase:
        """读取当前KB版本及发布身份。"""
        with self.transaction() as session:
            return kb_value(self._scope(session, principal, kb_id))

    def get_fields(self, principal: Principal, kb_id: str) -> list[MetadataField]:
        """字段定义属于KB事实，独立解析产品也可使用，不导入检索引擎。"""
        return self.get_kb(principal, kb_id).fields

    def set_auto_activate(self, principal: Principal, kb_id: str, enabled: bool) -> KnowledgeBase:
        """控制后续上传；关闭同时撤回尚未生效的自动授权。"""
        with self.transaction() as session:
            kb = self._scope(session, principal, kb_id, write=True, lock=True)
            if kb.kind != "knowledge":
                raise DomainError("invalid_request", "此集合不提供知识检索")
            if kb.auto_activate != enabled:
                kb.auto_activate = enabled
                kb.revision += 1
                if not enabled:
                    session.execute(
                        text(
                            "UPDATE ip_documents SET activation_mode=NULL WHERE tenant_id=:tenant AND kb_id=:kb AND activation_mode='automatic'"
                        ),
                        {"tenant": kb.tenant_id, "kb": kb.id},
                    )
                self._audit(session, principal, "knowledge.auto_activate", kb.id, kb_id=kb.id)
            return kb_value(kb)

    def enqueue_activation(self, session: Session, principal: Principal, kb_id: str) -> None:
        """与选用同事务写入任务和投递记录。"""
        waiting = session.scalar(
            select(JobRow.id)
            .where(
                JobRow.tenant_id == principal.tenant_id,
                JobRow.kb_id == kb_id,
                JobRow.kind == "build",
                JobRow.payload["activate"].as_boolean().is_(True),
                JobRow.status.in_(["queued", "retry_wait"]),
            )
            .limit(1)
        )
        if waiting:
            return
        key = uuid4().hex
        self._new_job(session, principal, "build", kb_id, None, {"activate": True}, key, None, key)

    def set_fields(
        self,
        principal: Principal,
        kb_id: str,
        expected_revision: int,
        fields: list[MetadataField],
    ) -> KnowledgeBase:
        """管理员CAS替换声明；现有资料不符合新规则时整体拒绝，不偷偷丢字段。"""
        require_role(principal, admin=True)
        if len(fields) > 64 or len(fields) != len({field.name for field in fields}):
            raise DomainError("invalid_metadata", "最多64个唯一字段")
        with self.transaction() as session:
            kb = self._scope(session, principal, kb_id, write=True, lock=True)
            if kb.revision != expected_revision:
                raise DomainError("version_conflict", "知识库已改变，请重新加载字段")
            missing_counts: dict[str, int] = {}
            for attributes, selected_metadata in session.execute(
                select(DocumentRow.attributes, DocumentRow.selected_metadata)
                .where(
                    DocumentRow.tenant_id == principal.tenant_id,
                    DocumentRow.kb_id == kb_id,
                    DocumentRow.deleted.is_(False),
                )
                .execution_options(yield_per=100)
            ):
                validate_metadata(attributes, fields, require_complete=False)
                for field in missing_required_fields(attributes, fields):
                    missing_counts[field.name] = missing_counts.get(field.name, 0) + 1
                if selected_metadata is not None:
                    # 已选快照可早于新必填规则，仍检查其中已有值的类型和声明。
                    validate_metadata(selected_metadata, fields, require_complete=False)
            if missing_counts:
                summary = "、".join(
                    f"「{field.label}」有 {missing_counts[field.name]} 份资料未填写"
                    for field in fields
                    if field.name in missing_counts
                )
                raise DomainError(
                    "required_fields_need_backfill",
                    summary + "。请先保存为选填，补齐后再设为必填。",
                    details={
                        "missing_required_fields": {
                            name: count for name, count in missing_counts.items()
                        }
                    },
                )
            service = session.get(ServiceRow, kb_id)
            if service is not None:
                validate_service_filters(ServiceConfig.model_validate(service.config), fields)
            kb.fields = [field.model_dump(mode="json") for field in fields]
            kb.revision += 1
            self._audit(session, principal, "fields.update", kb_id, kb_id=kb_id)
            session.flush()
            return kb_value(kb)

    def update_metadata(
        self,
        principal: Principal,
        document_id: str,
        expected_document_revision: int,
        metadata: dict[str, JsonValue],
    ) -> Document:
        """以资料聚合版本CAS修订属性，不修改不可变解析链或原件。"""
        with self.transaction() as session:
            current = self._doc(session, principal, document_id, write=True)
            kb = self._scope(session, principal, current.kb_id, write=True, lock=True)
            doc = self._doc(session, principal, document_id, write=True, lock=True)
            if doc.revision != expected_document_revision:
                raise DomainError("version_conflict", "资料版本已改变，请重新加载业务字段")
            parsing = session.scalar(
                select(JobRow.id)
                .where(
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.document_id == doc.id,
                    JobRow.kind == "parse",
                    JobRow.status.in_(
                        ["queued", "running", "retry_wait", "reconciling", "cancel_requested"]
                    ),
                )
                .limit(1)
            )
            if parsing:
                raise DomainError("not_ready", "资料正在解析，请在解析结束后编辑业务字段")
            fields = [MetadataField.model_validate(value) for value in kb.fields]
            validate_metadata(metadata, fields)
            if doc.attributes == metadata:
                return document_value(doc)
            doc.attributes = metadata
            doc.revision += 1
            kb.revision += 1
            self._audit(session, principal, "metadata.update", doc.id, kb_id=doc.kb_id)
            session.flush()
            return document_value(doc)

    def _new_document(
        self,
        session: Session,
        principal: Principal,
        kb_id: str,
        name: str,
        source_asset_id: str,
        metadata: dict[str, JsonValue] | None,
        parser_engine: ParserEngine = "builtin",
    ) -> DocumentRow:
        """调用方已保存原件后登记身份，不能把客户端路径当资源位置。"""
        kb = self._scope(session, principal, kb_id, write=True, lock=True)
        if not name.strip() or len(name) > 255 or any(c in name for c in ("/", "\\", "\x00")):
            raise DomainError("invalid_request", "资料名称无效")
        TypeAdapter(str).validate_python(source_asset_id, strict=True)
        validate_metadata(
            metadata or {}, [MetadataField.model_validate(value) for value in kb.fields]
        )
        row = DocumentRow(
            id=uuid4().hex,
            tenant_id=principal.tenant_id,
            kb_id=kb_id,
            name=name,
            source_asset_id=source_asset_id,
            parser_engine=parser_engine,
            attributes=metadata or {},
        )
        session.add(row)
        session.flush()
        session.add(
            AssetReferenceRow(
                document_id=row.id, tenant_id=principal.tenant_id, asset_id=source_asset_id
            )
        )
        kb.revision += 1
        self._audit(session, principal, "document.create", row.id, kb_id=kb_id)
        return row

    def create_document(
        self,
        principal: Principal,
        kb_id: str,
        name: str,
        source_asset_id: str,
        metadata: dict[str, JsonValue] | None = None,
    ) -> Document:
        """登记资料；需要原子创建解析任务时使用submit_parse。"""
        with self.transaction() as session:
            return document_value(
                self._new_document(session, principal, kb_id, name, source_asset_id, metadata)
            )

    def get_document(self, principal: Principal, document_id: str) -> Document:
        """读取未删除的当前资料指针。"""
        with self.transaction() as session:
            return document_value(self._doc(session, principal, document_id))

    def list_documents(
        self, principal: Principal, kb_id: str, limit: int = 100, offset: int = 0
    ) -> list[Document]:
        """使用真实有界SQL分页，租户/KB在limit之前约束。"""
        if not 1 <= limit <= 100 or offset < 0:
            raise DomainError("invalid_request", "分页无效")
        with self.transaction() as session:
            self._scope(session, principal, kb_id)
            return [
                document_value(r)
                for r in session.scalars(
                    select(DocumentRow)
                    .where(
                        DocumentRow.tenant_id == principal.tenant_id,
                        DocumentRow.kb_id == kb_id,
                        DocumentRow.deleted.is_(False),
                    )
                    .order_by(DocumentRow.created_at.desc(), DocumentRow.id)
                    .limit(limit)
                    .offset(offset)
                )
            ]

    def get_version(
        self, principal: Principal, document_id: str, result_id: str | None = None
    ) -> ResultVersion:
        """历史读取必须属于此资料且当前仍有权限。"""
        with self.transaction() as session:
            doc = self._doc(session, principal, document_id)
            row = session.scalar(
                select(ResultRow).where(
                    ResultRow.tenant_id == principal.tenant_id,
                    ResultRow.document_id == document_id,
                    ResultRow.result_id == (result_id or doc.result_id),
                )
            )
            if row is None:
                raise DomainError("not_found", "解析版本尚未就绪或不存在")
            return _version(row)

    def versions(self, principal: Principal, document_id: str) -> list[ResultVersion]:
        """分页上限内读取历史，不允许按摘要跨资料访问。"""
        with self.transaction() as session:
            self._doc(session, principal, document_id)
            return [
                _version(r)
                for r in session.scalars(
                    select(ResultRow)
                    .where(
                        ResultRow.tenant_id == principal.tenant_id,
                        ResultRow.document_id == document_id,
                    )
                    .order_by(ResultRow.revision.desc())
                    .limit(100)
                )
            ]

    def _save_version(
        self,
        session: Session,
        doc: DocumentRow,
        result_id: str,
        revision: int,
        previous_result_id: str | None,
        asset_ids: list[str],
    ) -> None:
        """资料已行锁定，检查版本链后同事务推进指针与历史。"""
        if revision < 1 or previous_result_id != doc.result_id:
            raise DomainError("version_conflict", "解析版本链与当前资料不一致")
        known = session.get(ResultRow, (doc.id, result_id))
        if known is None:
            latest = (
                session.scalar(
                    select(func.max(ResultRow.revision)).where(
                        ResultRow.document_id == doc.id, ResultRow.tenant_id == doc.tenant_id
                    )
                )
                or 0
            )
            session.add(
                ResultRow(
                    document_id=doc.id,
                    tenant_id=doc.tenant_id,
                    result_id=result_id,
                    revision=latest + 1,
                    previous_result_id=previous_result_id,
                    asset_ids=sorted(set([doc.source_asset_id, *asset_ids])),
                )
            )
        elif known.tenant_id != doc.tenant_id:
            raise DomainError("not_found", "结果不属于当前资料")
        existing = set(
            session.scalars(
                select(AssetReferenceRow.asset_id).where(
                    AssetReferenceRow.document_id == doc.id,
                    AssetReferenceRow.tenant_id == doc.tenant_id,
                )
            )
        )
        for identity in {doc.source_asset_id, *asset_ids} - existing:
            session.add(
                AssetReferenceRow(document_id=doc.id, tenant_id=doc.tenant_id, asset_id=identity)
            )
        doc.result_id, doc.revision = result_id, doc.revision + 1

    def save_revision(
        self,
        principal: Principal,
        document_id: str,
        expected_result_id: str,
        result_id: str,
        revision: int,
        previous_result_id: str | None,
        asset_ids: list[str],
        *,
        activate: bool = False,
        expected_document_revision: int | None = None,
    ) -> Document:
        """在PG行锁和版本条件下保存修订，资产写失败不得先调用此提交。"""
        with self.transaction() as session:
            before = self._doc(session, principal, document_id)
            kb = self._scope(session, principal, before.kb_id, write=True, lock=True)
            doc = self._doc(session, principal, document_id, write=True, lock=True)
            if doc.result_id != expected_result_id or (
                activate and doc.revision != expected_document_revision
            ):
                raise DomainError("version_conflict", "资料已有新版本，请重新加载")
            if activate and kb.kind != "knowledge":
                raise DomainError("invalid_request", "独立解析集合仅可保存版本")
            self._save_version(session, doc, result_id, revision, previous_result_id, asset_ids)
            if activate:
                doc.selected_result_id = result_id
                doc.selected_revision = doc.revision
                doc.selected_metadata = dict(doc.attributes)
                doc.activation_mode = "manual"
                self.enqueue_activation(session, principal, kb.id)
            kb.revision += 1
            self._audit(session, principal, "document.revise", doc.id, kb_id=doc.kb_id)
            session.flush()
            return document_value(doc)

    def delete_document(
        self, principal: Principal, document_id: str, expected_revision: int
    ) -> Document:
        """先撤销可见性和授权epoch；索引清理未追平时检索必须fail-closed。"""
        with self.transaction() as session:
            doc = self._doc(session, principal, document_id, write=True, lock=True)
            kb = self._scope(session, principal, doc.kb_id, write=True, lock=True)
            if doc.revision != expected_revision:
                raise DomainError("version_conflict", "资料版本已变化")
            doc.deleted = True
            doc.revision += 1
            kb.revision += 1
            kb.authz_epoch += 1
            for job in session.scalars(
                select(JobRow).where(
                    JobRow.document_id == doc.id,
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.status.in_(["queued", "running", "retry_wait"]),
                )
            ):
                job.status = "cancelled"
                job.lease_epoch += 1
                job.lease_owner = None
                job.lease_until = None
            self._audit(session, principal, "document.delete", doc.id, kb_id=doc.kb_id)
            session.flush()
            return document_value(doc)

    def has_asset(self, principal: Principal, asset_id: str) -> bool:
        """摘要不是凭证，资源必须至少被当前可见资料引用。"""
        require_role(principal)
        with self.transaction() as session:
            stmt = (
                select(AssetReferenceRow.document_id)
                .join(DocumentRow, DocumentRow.id == AssetReferenceRow.document_id)
                .join(TenantRow, TenantRow.id == DocumentRow.tenant_id)
                .where(
                    AssetReferenceRow.tenant_id == principal.tenant_id,
                    AssetReferenceRow.asset_id == asset_id,
                    DocumentRow.tenant_id == principal.tenant_id,
                    DocumentRow.deleted.is_(False),
                    TenantRow.active.is_(True),
                )
            )
            if principal.kb_ids is not None:
                stmt = stmt.where(DocumentRow.kb_id.in_(principal.kb_ids))
            return session.scalar(stmt.limit(1)) is not None

    def _new_job(
        self,
        session: Session,
        principal: Principal,
        kind: JobKind,
        kb_id: str,
        document: DocumentRow | None,
        payload: dict[str, JsonValue],
        idempotency_key: str,
        expected_result_id: str | None,
        request_hash: str,
    ) -> JobRow:
        """仅在已授权短事务中写job与outbox；唯一约束防并发重复。"""
        row = JobRow(
            id=uuid4().hex,
            operation_id=uuid4().hex,
            tenant_id=principal.tenant_id,
            kb_id=kb_id,
            document_id=document.id if document else None,
            kind=kind,
            payload=payload,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            expected_result_id=expected_result_id,
            expected_document_revision=document.revision if document else None,
        )
        session.add(row)
        session.flush()
        session.add(OutboxRow(id=uuid4().hex, job_id=row.id))
        self._audit(session, principal, "job.accept", row.id, kb_id=kb_id)
        return row

    def _previous_job(
        self,
        session: Session,
        principal: Principal,
        kind: JobKind,
        idempotency_key: str,
        digest: str,
    ) -> JobRow | None:
        """KB行锁串行同范围接收，唯一键绑定请求摘要，不能异请求复用。"""
        if not idempotency_key or len(idempotency_key) > 256:
            raise DomainError("invalid_request", "需要有效Idempotency-Key")
        lock_key = int.from_bytes(
            hashlib.sha256(
                (principal.tenant_id + ":" + kind + ":" + idempotency_key).encode()
            ).digest()[:8],
            "big",
            signed=True,
        )
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        previous = session.scalar(
            select(JobRow).where(
                JobRow.tenant_id == principal.tenant_id,
                JobRow.kind == kind,
                JobRow.idempotency_key == idempotency_key,
            )
        )
        if previous is not None and previous.request_hash != digest:
            raise DomainError("idempotency_conflict", "此幂等键已用于其他请求")
        return previous

    def submit_parse(
        self,
        principal: Principal,
        kb_id: str,
        name: str,
        source_asset_id: str,
        idempotency_key: str,
        metadata: dict[str, JsonValue] | None = None,
        parser_engine: ParserEngine = "builtin",
        ocr_enabled: bool | None = None,
    ) -> Job:
        """同事务创建资料、解析任务和投递意图，API成功后进程退出也不会丢任务。"""
        if parser_engine == "builtin" and ocr_enabled is True:
            raise DomainError("unsupported_option", "内置解析工具不支持 OCR")
        identity: list[JsonValue] = [kb_id, name, source_asset_id, metadata]
        if parser_engine != "builtin":
            identity.append(parser_engine)
        if ocr_enabled is not None:
            identity.append({"ocr_enabled": ocr_enabled})
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self.transaction() as session:
            self._scope(session, principal, kb_id, write=True, lock=True)
            previous = self._previous_job(session, principal, "parse", idempotency_key, digest)
            if previous is not None:
                return job_value(previous)
            doc = self._new_document(
                session, principal, kb_id, name, source_asset_id, metadata, parser_engine
            )
            return job_value(
                self._new_job(
                    session,
                    principal,
                    "parse",
                    kb_id,
                    doc,
                    {"parser_engine": parser_engine, "ocr_enabled": ocr_enabled},
                    idempotency_key,
                    None,
                    digest,
                )
            )

    def confirm_import(
        self,
        principal: Principal,
        kb_id: str,
        files: list[tuple[str, str, str, str, dict[str, JsonValue], dict[str, JsonValue], int]],
    ) -> list[Job]:
        """原子登记整批资料和任务。同一暂存文件只可确认一次，重试返回原任务。"""
        accepted: list[Job] = []
        with self.transaction() as session:
            self._scope(session, principal, kb_id, write=True, lock=True)
            for stage_id, filename, asset_id, result_id, metadata, rules, expires_at in files:
                key = "import:" + stage_id
                identity = [kb_id, filename, asset_id, result_id, metadata, rules]
                digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
                previous = self._previous_job(session, principal, "parse", key, digest)
                if previous is not None:
                    accepted.append(job_value(previous))
                    continue
                if expires_at <= _now(session).timestamp():
                    raise DomainError("invalid_request", "预览已过期，请重新添加文件")
                engine = TypeAdapter(ParserEngine).validate_python(rules["engine"])
                document = self._new_document(
                    session, principal, kb_id, filename, asset_id, metadata, engine
                )
                document.processing = TypeAdapter(dict[str, JsonValue]).validate_python(
                    rules.get("processing") or {}, strict=True
                )
                accepted.append(
                    job_value(
                        self._new_job(
                            session,
                            principal,
                            "parse",
                            kb_id,
                            document,
                            {
                                "prepared_result_id": result_id,
                                "parser_engine": rules["engine"],
                                "ocr_enabled": rules.get("ocr_enabled"),
                                "import_rules": rules,
                            },
                            key,
                            None,
                            digest,
                        )
                    )
                )
        return accepted

    def accept_job(
        self,
        principal: Principal,
        kind: JobKind,
        kb_id: str,
        document_id: str | None,
        payload: dict[str, JsonValue],
        idempotency_key: str,
        expected_result_id: str | None = None,
        expected_document_revision: int | None = None,
    ) -> Job:
        """冻结输入身份与版本；入库选用不改写人工审核，后台只接受已持久job_id。"""
        digest = hashlib.sha256(
            json.dumps(
                [kb_id, document_id, payload, expected_result_id, expected_document_revision],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.transaction() as session:
            kb = self._scope(session, principal, kb_id, write=True, lock=True)
            previous = self._previous_job(session, principal, kind, idempotency_key, digest)
            if previous is not None:
                return job_value(previous)
            doc = (
                self._doc(session, principal, document_id, write=True, lock=True)
                if document_id
                else None
            )
            if doc is not None and (
                doc.kb_id != kb_id
                or doc.result_id != expected_result_id
                or (
                    expected_document_revision is not None
                    and doc.revision != expected_document_revision
                )
            ):
                raise DomainError("version_conflict", "提交的资料版本已变化")
            if kind in {"build", "ingest"} and kb.kind != "knowledge":
                raise DomainError(
                    "invalid_request", "独立解析集合不能构建检索服务，请先选用到知识库"
                )
            if kind == "build" and payload.get("incremental") is True:
                payload = {
                    **payload,
                    "base_release_id": kb.active_release_id,
                    "base_kb_revision": kb.revision,
                }
            if kind == "ingest" and doc is not None:
                if doc.result_id is None:
                    raise DomainError("not_ready", "资料尚未完成解析")
                doc.selected_result_id = doc.result_id
                doc.selected_revision = doc.revision
                doc.selected_metadata = dict(doc.attributes)
                doc.activation_mode = None
                kb.revision += 1
            return job_value(
                self._new_job(
                    session,
                    principal,
                    kind,
                    kb_id,
                    doc,
                    payload,
                    idempotency_key,
                    expected_result_id,
                    digest,
                )
            )

    def submit_reparse(
        self,
        principal: Principal,
        document_id: str,
        expected_document_revision: int,
        idempotency_key: str,
        source_asset_id: str | None = None,
        filename: str | None = None,
    ) -> Job:
        """冻结重解析输入；新原件成功解析前不覆盖已用资料和历史资产。"""
        digest = hashlib.sha256(
            json.dumps(
                [document_id, expected_document_revision, source_asset_id, filename], sort_keys=True
            ).encode()
        ).hexdigest()
        with self.transaction() as session:
            peek = self._doc(session, principal, document_id, write=True)
            self._scope(session, principal, peek.kb_id, write=True, lock=True)
            previous = self._previous_job(session, principal, "parse", idempotency_key, digest)
            if previous is not None:
                return job_value(previous)
            doc = self._doc(session, principal, document_id, write=True, lock=True)
            if doc.revision != expected_document_revision:
                raise DomainError("version_conflict", "资料已变化，请刷新后重新解析")
            if source_asset_id is not None and (
                len(source_asset_id) != 64
                or any(char not in "0123456789abcdef" for char in source_asset_id)
            ):
                raise DomainError("invalid_request", "原件资产身份无效")
            if filename is not None and (
                not filename.strip()
                or len(filename) > 255
                or any(char in filename for char in ("/", "\\", "\x00"))
            ):
                raise DomainError("invalid_request", "原件名称无效")
            latest_parse = session.scalar(
                select(JobRow)
                .where(
                    JobRow.document_id == document_id,
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.kind == "parse",
                )
                .order_by(JobRow.created_at.desc(), JobRow.id.desc())
                .limit(1)
            )
            engine = (
                latest_parse.payload.get("parser_engine", "builtin")
                if latest_parse is not None
                else "builtin"
            )
            if engine not in {"builtin", "docling"}:
                raise DomainError("invalid_job", "原解析工具标识无效，不能自动切换")
            try:
                check_engine_format(
                    TypeAdapter(ParserEngine).validate_python(engine), filename or doc.name
                )
            except ParseError as exc:
                raise DomainError(exc.code, str(exc)) from None
            from .parser_rules import validate_parser_rules

            persisted_rules = latest_parse.payload.get("import_rules") if latest_parse else None
            if isinstance(persisted_rules, dict):
                validate_parser_rules(
                    TypeAdapter(ParserEngine).validate_python(engine),
                    filename or doc.name,
                    persisted_rules,
                )
            return job_value(
                self._new_job(
                    session,
                    principal,
                    "parse",
                    doc.kb_id,
                    doc,
                    {
                        "reparse": True,
                        "replacement": source_asset_id is not None,
                        "parser_engine": engine,
                        "ocr_enabled": latest_parse.payload.get("ocr_enabled")
                        if latest_parse
                        else None,
                        "import_rules": latest_parse.payload.get("import_rules")
                        if latest_parse
                        else None,
                        "source_asset_id": source_asset_id or doc.source_asset_id,
                        "filename": filename or doc.name,
                    },
                    idempotency_key,
                    doc.result_id,
                    digest,
                )
            )

    def get_job(self, principal: Principal, job_id: str) -> Job:
        """客户端任务读取受同租户及KB权限控制。"""
        with self.transaction() as session:
            row = session.scalar(
                select(JobRow).where(JobRow.id == job_id, JobRow.tenant_id == principal.tenant_id)
            )
            if row is None or (
                row.kind == "import_preview" and row.payload.get("owner") != principal.subject
            ):
                raise DomainError("not_found", "任务不存在")
            self._scope(session, principal, row.kb_id)
            return job_value(row)

    def list_jobs(self, principal: Principal, kb_id: str, limit: int = 100) -> list[Job]:
        """返回有界任务列表，状态以PG事实为准。"""
        if not 1 <= limit <= 100:
            raise DomainError("invalid_request", "分页无效")
        with self.transaction() as session:
            self._scope(session, principal, kb_id)
            return [
                job_value(r)
                for r in session.scalars(
                    select(JobRow)
                    .where(
                        JobRow.tenant_id == principal.tenant_id,
                        JobRow.kb_id == kb_id,
                        JobRow.kind != "import_preview",
                    )
                    .order_by(JobRow.created_at.desc())
                    .limit(limit)
                )
            ]

    def cancel_job(self, principal: Principal, job_id: str) -> Job:
        """持久化取消并立即失效旧epoch，不能只依靠Celery进程终止。"""
        with self.transaction() as session:
            row = session.scalar(
                select(JobRow)
                .where(JobRow.id == job_id, JobRow.tenant_id == principal.tenant_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None or (
                row.kind == "import_preview" and row.payload.get("owner") != principal.subject
            ):
                raise DomainError("not_found", "任务不存在")
            self._scope(session, principal, row.kb_id, write=True)
            if row.status not in ("succeeded", "failed", "cancelled"):
                self._fail_builds(session, row.id)
                row.status = "cancelled"
                row.lease_epoch += 1
                row.lease_owner = None
                row.lease_until = None
                row.updated_at = _now(session)
            self._audit(session, principal, "job.cancel", row.id, kb_id=row.kb_id)
            return job_value(row)

    def retry_job(self, principal: Principal, job_id: str) -> Job:
        """只显式重试确定失败任务，输入变化要求新意图；保留历史epoch。"""
        with self.transaction() as session:
            row = session.scalar(
                select(JobRow)
                .where(JobRow.id == job_id, JobRow.tenant_id == principal.tenant_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None or (
                row.kind == "import_preview" and row.payload.get("owner") != principal.subject
            ):
                raise DomainError("not_found", "任务不存在")
            self._scope(session, principal, row.kb_id, write=True)
            if row.status != "failed":
                raise DomainError("version_conflict", "只有确定失败任务可以重试")
            if row.error_diagnostic and row.error_diagnostic.get("retryable") is False:
                raise DomainError(
                    "invalid_request", "此输入不能直接重试，请替换原件或创建新的重新解析意图"
                )
            if row.document_id:
                doc = self._doc(session, principal, row.document_id)
                if doc.revision != row.expected_document_revision:
                    raise DomainError("stale_job", "任务输入已过期，请创建新意图")
            row.status = "queued"
            row.attempt = 0
            row.error_code = None
            row.error_diagnostic = None
            row.updated_at = _now(session)
            session.add(OutboxRow(id=uuid4().hex, job_id=row.id))
            self._audit(session, principal, "job.retry", row.id, kb_id=row.kb_id)
            return job_value(row)

    def worker_job(self, job_id: str) -> Job:
        """受信执行面根据消息ID读取PG保存的范围，绝不信任消息自带tenant。"""
        with self.transaction() as session:
            row = session.get(JobRow, job_id)
            if row is None:
                raise DomainError("not_found", "任务不存在")
            return job_value(row)

    def claim_job(self, job_id: str, owner: str, lease_seconds: int = 60) -> Job | None:
        """短事务领取或重领过期租约；成功重投/取消直接忽略。"""
        if not 5 <= lease_seconds <= 3600 or not owner:
            raise ValueError("执行租约无效")
        with self.transaction() as session:
            row = session.scalar(
                select(JobRow)
                .where(JobRow.id == job_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if row is None:
                return None
            now = _now(session)
            eligible = row.status in ("queued", "retry_wait") or (
                row.status == "running" and row.lease_until is not None and row.lease_until <= now
            )
            if not eligible or (row.next_retry_at is not None and row.next_retry_at > now):
                return None
            if row.attempt >= 3:
                row.status = "failed"
                row.stage = "failed"
                row.error_code = "attempts_exhausted"
                row.lease_owner = None
                row.lease_until = None
                self._fail_builds(session, row.id)
                return None
            if row.status == "running":
                self._fail_builds(session, row.id)
            row.status = "running"
            row.stage = "claimed"
            row.attempt += 1
            row.lease_epoch += 1
            row.lease_owner = owner
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            row.error_code = None
            session.add(AttemptRow(job_id=row.id, epoch=row.lease_epoch, owner=owner))
            return job_value(row)

    def _leased(self, session: Session, job_id: str, owner: str, epoch: int) -> JobRow:
        """所有产物提交使用同一数据库执行权条件，旧worker不能发布。"""
        row = session.scalar(
            select(JobRow)
            .where(JobRow.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or row.status != "running"
            or row.lease_owner != owner
            or row.lease_epoch != epoch
            or row.lease_until is None
            or row.lease_until <= _now(session)
        ):
            raise DomainError("lease_lost", "执行权已过期、被取消或被其他Worker接管")
        return row

    def heartbeat(
        self, job_id: str, owner: str, epoch: int, stage: str, lease_seconds: int = 60
    ) -> None:
        """只允许当前仍有效的执行权续约，不允许过期任务自我复活。"""
        with self.transaction() as session:
            row = self._leased(session, job_id, owner, epoch)
            now = _now(session)
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.stage = stage[:32]
            row.updated_at = now

    def complete_parse(
        self,
        job_id: str,
        owner: str,
        epoch: int,
        result_id: str,
        revision: int,
        previous_result_id: str | None,
        asset_ids: list[str],
        *,
        prepare_update: bool = False,
    ) -> Document:
        """解析版本、资料指针与任务成功同事务提交，外部资产必须先完整就绪。"""
        with self.transaction() as session:
            peek = session.get(JobRow, job_id)
            if peek is None:
                raise DomainError("not_found", "任务不存在")
            self._scope(
                session,
                Principal(tenant_id=peek.tenant_id, subject="worker", roles=["editor"]),
                peek.kb_id,
                write=True,
                lock=True,
            )
            job = self._leased(session, job_id, owner, epoch)
            if job.document_id is None:
                raise DomainError("invalid_job", "解析任务没有资料")
            principal = Principal(tenant_id=job.tenant_id, subject="worker", roles=["editor"])
            doc = self._doc(session, principal, job.document_id, write=True, lock=True)
            kb = self._scope(session, principal, job.kb_id, write=True, lock=True)
            if doc.revision != job.expected_document_revision:
                raise DomainError("stale_job", "资料在执行期间已改变")
            if job.payload.get("reparse") is True:
                source = job.payload.get("source_asset_id")
                filename = job.payload.get("filename")
                if isinstance(source, str):
                    doc.source_asset_id = source
                if isinstance(filename, str):
                    doc.name = filename
                previous_result_id = doc.result_id
            self._save_version(session, doc, result_id, revision, previous_result_id, asset_ids)
            if kb.kind == "knowledge" and kb.auto_activate and prepare_update:
                doc.selected_result_id = result_id
                doc.selected_revision = doc.revision
                doc.selected_metadata = dict(doc.attributes)
                doc.activation_mode = "automatic"
                self.enqueue_activation(session, principal, kb.id)
            kb.revision += 1
            job.result = {"document_id": doc.id, "result_id": result_id}
            job.status = "succeeded"
            job.stage = "complete"
            job.lease_owner = None
            job.lease_until = None
            job.updated_at = _now(session)
            session.flush()
            return document_value(doc)

    def finish_job(self, job_id: str, owner: str, epoch: int, result: dict[str, JsonValue]) -> Job:
        """一般阶段结果成功提交；解析和构建请用对应原子完成方法。"""
        with self.transaction() as session:
            row = self._leased(session, job_id, owner, epoch)
            row.result = result
            row.status = "succeeded"
            row.stage = "complete"
            row.lease_owner = None
            row.lease_until = None
            row.updated_at = _now(session)
            return job_value(row)

    def fail_job(
        self,
        job_id: str,
        owner: str,
        epoch: int,
        code: str,
        retry: bool = False,
        diagnostic: dict[str, JsonValue] | None = None,
    ) -> Job:
        """确定失败才调用；外部提交未知由reconciling处理，不伪造成功。"""
        with self.transaction() as session:
            row = self._leased(session, job_id, owner, epoch)
            now = _now(session)
            self._fail_builds(session, row.id)
            row.status = "retry_wait" if retry and row.attempt < 3 else "failed"
            row.error_code = code[:128]
            row.error_diagnostic = {
                key: value[:2000] if isinstance(value, str) else value
                for key, value in (diagnostic or {}).items()
                if key in {"message", "hint", "suggestion", "location", "retryable"}
                and isinstance(value, (str, bool))
            } or None
            row.stage = "failed"
            row.lease_owner = None
            row.lease_until = None
            row.updated_at = now
            if row.status == "retry_wait":
                row.next_retry_at = now + timedelta(seconds=min(60, 2**row.attempt))
                session.add(OutboxRow(id=uuid4().hex, job_id=row.id))
            return job_value(row)

    def claim_outbox(
        self, limit: int = 20, lease_seconds: int = 30, *, job_id: str | None = None
    ) -> list[OutboxEvent]:
        """多个Relay使用PG SKIP LOCKED竞争事件，网络投递不持有事务。"""
        with self.transaction() as session:
            now = _now(session)
            rows = list(
                session.scalars(
                    select(OutboxRow)
                    .join(JobRow, JobRow.id == OutboxRow.job_id)
                    .where(
                        or_(
                            OutboxRow.status == "pending",
                            (OutboxRow.status == "sending") & (OutboxRow.lease_until <= now),
                        ),
                        or_(JobRow.next_retry_at.is_(None), JobRow.next_retry_at <= now),
                        OutboxRow.job_id == job_id if job_id is not None else true(),
                    )
                    .order_by(OutboxRow.created_at)
                    .limit(max(1, min(limit, 100)))
                    .with_for_update(skip_locked=True, of=OutboxRow)
                    .execution_options(populate_existing=True)
                )
            )
            for row in rows:
                row.status = "sending"
                row.lease_until = now + timedelta(seconds=lease_seconds)
                row.attempts += 1
            return [OutboxEvent(event_id=r.id, job_id=r.job_id, attempts=r.attempts) for r in rows]

    def mark_outbox(self, event_id: str, attempts: int, delivered: bool) -> None:
        """只有已验证实际路由和publisher confirm才能标成功，失败可重投。"""
        with self.transaction() as session:
            row = session.scalar(
                select(OutboxRow)
                .where(OutboxRow.id == event_id, OutboxRow.attempts == attempts)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is not None and row.status == "sending":
                row.status = "sent" if delivered else "pending"
                row.lease_until = None

    def recover_jobs(self, limit: int = 100, *, job_id: str | None = None) -> int:
        """回收过期执行和逾期未领取的投递，重试任务须等退避和领取宽限期结束。"""
        with self.transaction() as session:
            now = _now(session)
            delivery_deadline = now - timedelta(seconds=60)
            pending = (
                select(OutboxRow.id)
                .where(OutboxRow.job_id == JobRow.id, OutboxRow.status.in_(["pending", "sending"]))
                .correlate(JobRow)
                .exists()
            )
            rows = list(
                session.scalars(
                    select(JobRow)
                    .where(
                        or_(
                            (JobRow.status == "running") & (JobRow.lease_until <= now),
                            JobRow.status.in_(["queued", "retry_wait"])
                            & (JobRow.updated_at < delivery_deadline)
                            & or_(
                                JobRow.next_retry_at.is_(None),
                                JobRow.next_retry_at < delivery_deadline,
                            )
                            & ~pending,
                        ),
                        JobRow.id == job_id if job_id is not None else true(),
                    )
                    .limit(max(1, min(limit, 1000)))
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            )
            for row in rows:
                self._fail_builds(session, row.id)
                row.status = "queued"
                row.lease_epoch += 1
                row.lease_owner = None
                row.lease_until = None
                row.updated_at = now
                existing = session.scalar(
                    select(OutboxRow.id)
                    .where(OutboxRow.job_id == row.id, OutboxRow.status.in_(["pending", "sending"]))
                    .limit(1)
                )
                if existing is None:
                    session.add(OutboxRow(id=uuid4().hex, job_id=row.id))
            return len(rows)

    def start_build(
        self,
        job_id: str,
        owner: str,
        epoch: int,
        index_name: str,
        model_revision: str,
        build_id: str | None = None,
        *,
        max_documents: int = MAX_BUILD_DOCUMENTS,
        max_manifest_bytes: int = MAX_MANIFEST_BYTES,
    ) -> Build:
        """冻结已解析资料清单及KB版本，每个尝试使用独立物理索引。"""
        if not 1 <= max_documents <= MAX_BUILD_DOCUMENTS or not (
            1024 <= max_manifest_bytes <= MAX_MANIFEST_BYTES
        ):
            raise ValueError("构建清单预算超出安全配置范围")
        with self.transaction() as session:
            peek = session.get(JobRow, job_id)
            if peek is None:
                raise DomainError("not_found", "任务不存在")
            self._scope(
                session,
                Principal(tenant_id=peek.tenant_id, subject="worker", roles=["editor"]),
                peek.kb_id,
                write=True,
                lock=True,
            )
            job = self._leased(session, job_id, owner, epoch)
            principal = Principal(tenant_id=job.tenant_id, subject="worker", roles=["editor"])
            kb = self._scope(session, principal, job.kb_id, write=True, lock=True)
            baseline = None
            activation = job.payload.get("activate") is True
            if activation or job.payload.get("incremental") is True:
                if not activation and (kb.active_release_id, kb.revision) != (
                    job.payload.get("base_release_id"),
                    job.payload.get("base_kb_revision"),
                ):
                    raise DomainError("version_conflict", "更新范围已变化，请重新准备")
                if activation:
                    job.payload = {**job.payload, "base_release_id": kb.active_release_id}
                if kb.active_release_id is not None:
                    active = session.get(ReleaseRow, kb.active_release_id)
                    baseline = session.get(BuildRow, active.build_id) if active else None
                    if baseline is None:
                        raise DomainError("database_unavailable", "当前发布引用不完整")
            if job.document_id is not None:
                trigger = self._doc(session, principal, job.document_id, write=True, lock=True)
                if (trigger.revision, trigger.result_id) != (
                    job.expected_document_revision,
                    job.expected_result_id,
                ):
                    raise DomainError("stale_job", "构建所依据的资料版本已改变")
            sizes = TypeAdapter(tuple[int, int, int]).validate_python(
                tuple(
                    session.execute(
                        text(
                            "SELECT count(*)::INTEGER,coalesce(sum(pg_column_size(attributes)),0)::BIGINT,"
                            "coalesce(sum(octet_length(jsonb_build_object('document_id',id,"
                            "'result_id',result_id,'revision',revision,'metadata',attributes)::text)),0)"
                            "::BIGINT+greatest(count(*)-1,0)*2+2 FROM ("
                            "SELECT id,selected_result_id AS result_id,selected_revision AS revision,selected_metadata AS attributes FROM ip_documents "
                            "WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND NOT deleted "
                            "AND selected_result_id IS NOT NULL "
                            "AND (NOT :activation OR activation_mode IS NOT NULL) LIMIT :sample_limit) sample"
                        ),
                        {
                            "tenant_id": job.tenant_id,
                            "kb_id": job.kb_id,
                            "sample_limit": max_documents + 1,
                            "activation": activation,
                        },
                    ).one()
                ),
                strict=True,
            )
            if sizes[0] > max_documents or max(sizes[1], sizes[2]) > max_manifest_bytes:
                raise DomainError(
                    "build_budget_exceeded",
                    "知识库超过构建资料数或清单预算，请拆分知识库或评估扩容",
                )
            rows = list(
                session.scalars(
                    select(DocumentRow)
                    .where(
                        DocumentRow.tenant_id == job.tenant_id,
                        DocumentRow.kb_id == job.kb_id,
                        DocumentRow.deleted.is_(False),
                        DocumentRow.selected_result_id.is_not(None),
                        DocumentRow.activation_mode.is_not(None) if activation else true(),
                    )
                    .order_by(DocumentRow.id)
                )
            )
            manifest = [
                BuildDocument(
                    document_id=r.id,
                    result_id=r.selected_result_id,
                    revision=r.selected_revision or 0,
                    metadata=r.selected_metadata or {},
                    processing=r.processing,
                )
                for r in rows
                if r.selected_result_id is not None
            ]
            encoded = [entry.model_dump(mode="json") for entry in manifest]
            if baseline is not None:
                selected_ids = {entry.document_id for entry in manifest}
                retained = [
                    item for item in baseline.manifest if item["document_id"] not in selected_ids
                ]
                active_ids = set(
                    session.scalars(
                        select(DocumentRow.id).where(
                            DocumentRow.tenant_id == job.tenant_id,
                            DocumentRow.kb_id == job.kb_id,
                            DocumentRow.deleted.is_(False),
                            DocumentRow.id.in_([item["document_id"] for item in retained]),
                        )
                    )
                )
                encoded.extend(item for item in retained if item["document_id"] in active_ids)
            if (
                len(encoded) > max_documents
                or len(json.dumps(encoded, ensure_ascii=False).encode()) > max_manifest_bytes
            ):
                raise DomainError(
                    "build_budget_exceeded", "构建清单超过预算，请拆分知识库或评估扩容"
                )
            service = session.get(ServiceRow, kb.id)
            current_schema = {
                field.name: "text" if field.type == "enum" else field.type
                for field in (MetadataField.model_validate(value) for value in kb.fields)
            }
            schema = (
                {**baseline.field_schema, **current_schema}
                if activation and baseline is not None
                else baseline.field_schema
                if baseline is not None
                else current_schema
            )
            row = BuildRow(
                id=build_id or uuid4().hex,
                tenant_id=job.tenant_id,
                kb_id=job.kb_id,
                job_id=job.id,
                index_name=index_name,
                model_revision=model_revision,
                kb_revision=kb.revision,
                authz_epoch=kb.authz_epoch,
                manifest=encoded,
                service_config=baseline.service_config
                if baseline is not None
                else service.config
                if service
                else {},
                field_schema=schema,
            )
            session.add(row)
            session.flush()
            self._check_manifest_budget(session, row.id, max_manifest_bytes=max_manifest_bytes)
            return _build(row)

    def _check_manifest_budget(
        self, session: Session, build_id: str, *, max_manifest_bytes: int = MAX_MANIFEST_BYTES
    ) -> None:
        """以读侧使用的 PostgreSQL JSONB 表示核对预算，不把存量大清单载入内存。"""
        document_count, manifest_bytes = session.execute(
            select(
                func.jsonb_array_length(BuildRow.manifest),
                func.octet_length(BuildRow.manifest.cast(Text)),
            ).where(BuildRow.id == build_id)
        ).one()
        if document_count > MAX_BUILD_DOCUMENTS or manifest_bytes > max_manifest_bytes:
            raise DomainError("build_budget_exceeded", "构建清单超过安全预算，请拆分知识库")

    def complete_build(self, job_id: str, owner: str, epoch: int, build_id: str) -> Build:
        """搜索后端已write-block/校验后提交ready及成功任务，不提前发布。"""
        with self.transaction() as session:
            peek = session.get(JobRow, job_id)
            if peek is None:
                raise DomainError("not_found", "任务不存在")
            self._scope(
                session,
                Principal(tenant_id=peek.tenant_id, subject="worker", roles=["editor"]),
                peek.kb_id,
                write=True,
                lock=True,
            )
            job = self._leased(session, job_id, owner, epoch)
            row = session.scalar(
                select(BuildRow)
                .where(
                    BuildRow.id == build_id,
                    BuildRow.job_id == job_id,
                    BuildRow.tenant_id == job.tenant_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None or row.status != "building":
                raise DomainError("not_found", "构建不存在")
            kb = session.scalar(
                select(KnowledgeBaseRow)
                .where(
                    KnowledgeBaseRow.id == job.kb_id, KnowledgeBaseRow.tenant_id == job.tenant_id
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            activation = job.payload.get("activate") is True
            if (
                kb is None
                or kb.revision != row.kb_revision
                or kb.authz_epoch != row.authz_epoch
                or (activation and kb.active_release_id != job.payload.get("base_release_id"))
            ):
                raise DomainError(
                    "activation_stale" if activation else "stale_job", "构建期间资料或权限已改变"
                )
            row.status = "ready"
            result: dict[str, JsonValue] = {"build_id": row.id, "index_name": row.index_name}
            if activation:
                pending = list(
                    session.scalars(
                        select(DocumentRow).where(
                            DocumentRow.tenant_id == kb.tenant_id,
                            DocumentRow.kb_id == kb.id,
                            DocumentRow.deleted.is_(False),
                            DocumentRow.activation_mode.is_not(None),
                        )
                    )
                )
                if pending:
                    worker = Principal(
                        tenant_id=kb.tenant_id, subject="activation-worker", roles=["editor"]
                    )
                    release = self._publish(session, worker, kb, row, kb.active_release_id)
                    result["release_id"] = release.release_id
                    for document in pending:
                        document.activation_mode = None
            job.result = result
            job.status = "succeeded"
            job.stage = "complete"
            job.lease_owner = None
            job.lease_until = None
            job.updated_at = _now(session)
            return _build(row)

    def list_builds(self, principal: Principal, kb_id: str) -> list[Build]:
        """有界摘要列表不把一百份完整清单传进应用内存；详情需单独授权读取。"""
        with self.transaction() as session:
            self._scope(session, principal, kb_id)
            return [
                Build.model_validate(dict(row))
                for row in session.execute(
                    text(_BUILD_SUMMARY + "AND kb_id=:kb_id ORDER BY created_at DESC LIMIT 100"),
                    {"tenant_id": principal.tenant_id, "kb_id": kb_id},
                ).mappings()
            ]

    def get_build(
        self,
        principal: Principal,
        build_id: str,
        *,
        max_documents: int = MAX_BUILD_DOCUMENTS,
        max_manifest_bytes: int = MAX_MANIFEST_BYTES,
    ) -> Build:
        """先授权并校验存量清单预算，再读取单份完整清单，不能靠旧记录绕过保护。"""
        if not 1 <= max_documents <= MAX_BUILD_DOCUMENTS or not (
            1024 <= max_manifest_bytes <= MAX_MANIFEST_BYTES
        ):
            raise ValueError("构建清单预算超出安全配置范围")
        with self.transaction() as session:
            summary = (
                session.execute(
                    text(_BUILD_SUMMARY + "AND id=:build_id"),
                    {"tenant_id": principal.tenant_id, "build_id": build_id},
                )
                .mappings()
                .one_or_none()
            )
            if summary is None:
                raise DomainError("not_found", "构建不存在")
            value = Build.model_validate(dict(summary))
            self._scope(session, principal, value.kb_id)
            if value.document_count > max_documents or value.manifest_bytes > max_manifest_bytes:
                raise DomainError(
                    "build_budget_exceeded", "存量构建超过清单预算，请拆分知识库或评估扩容"
                )
            row = session.scalar(
                select(BuildRow).where(
                    BuildRow.id == build_id, BuildRow.tenant_id == principal.tenant_id
                )
            )
            if row is None:
                raise DomainError("not_found", "构建不存在")
            return _build(row)

    def _publish(
        self,
        session: Session,
        principal: Principal,
        kb: KnowledgeBaseRow,
        build: BuildRow,
        expected_release_id: str | None,
    ) -> Release:
        """PG是唯一发布切换点；旧授权epoch的generation不能借回滚复活。"""
        if kb.active_release_id != expected_release_id:
            raise DomainError("version_conflict", "发布已变化，请刷新后重试")
        if build.status != "ready" or build.authz_epoch != kb.authz_epoch:
            raise DomainError("authorization_pending", "构建未就绪或权限投影已过期")
        self._check_manifest_budget(session, build.id)
        if kb.active_release_id is not None:
            active = session.get(ReleaseRow, kb.active_release_id)
            if active is not None and active.build_id == build.id:
                return _release(active)
        latest = session.scalar(
            select(func.max(ReleaseRow.version)).where(
                ReleaseRow.tenant_id == principal.tenant_id, ReleaseRow.kb_id == kb.id
            )
        )
        row = ReleaseRow(
            id=uuid4().hex,
            tenant_id=principal.tenant_id,
            kb_id=kb.id,
            build_id=build.id,
            version=(latest or 0) + 1,
            index_name=build.index_name,
            model_revision=build.model_revision,
            authz_epoch=build.authz_epoch,
        )
        session.add(row)
        session.flush()
        kb.active_release_id = row.id
        parameters = {"build_id": build.id, "tenant_id": principal.tenant_id, "kb_id": kb.id}
        # 直接在PG展开不可变清单，只更新差异，避免逐文档ORM往返或把完整目录拉进内存。
        session.execute(
            text(
                "UPDATE ip_documents d SET indexed_result_id=m.result_id,indexed_revision=m.revision "
                "FROM ip_builds b, jsonb_to_recordset(b.manifest) "
                "AS m(document_id TEXT,result_id TEXT,revision INTEGER) "
                "WHERE b.id=:build_id AND b.tenant_id=:tenant_id AND b.kb_id=:kb_id "
                "AND d.tenant_id=:tenant_id AND d.kb_id=:kb_id AND NOT d.deleted "
                "AND d.id=m.document_id AND (d.indexed_result_id IS DISTINCT FROM m.result_id "
                "OR d.indexed_revision IS DISTINCT FROM m.revision)"
            ),
            parameters,
        )
        session.execute(
            text(
                "UPDATE ip_documents d SET indexed_result_id=NULL,indexed_revision=NULL "
                "WHERE d.tenant_id=:tenant_id AND d.kb_id=:kb_id AND NOT d.deleted "
                "AND (d.indexed_result_id IS NOT NULL OR d.indexed_revision IS NOT NULL) "
                "AND NOT EXISTS(SELECT 1 FROM ip_builds b, jsonb_to_recordset(b.manifest) "
                "AS m(document_id TEXT) WHERE b.id=:build_id AND b.tenant_id=:tenant_id "
                "AND b.kb_id=:kb_id AND m.document_id=d.id)"
            ),
            parameters,
        )
        self._audit(session, principal, "release.publish", row.id, kb_id=kb.id)
        return _release(row)

    def publish_build(
        self, principal: Principal, build_id: str, expected_release_id: str | None = None
    ) -> Release:
        """只有当前资料版本对应的ready构建可以首次发布，CAS避免覆盖他人发布。"""
        require_role(principal, write=True)
        with self.transaction() as session:
            build = session.scalar(
                select(BuildRow)
                .options(defer(BuildRow.manifest))
                .where(BuildRow.id == build_id, BuildRow.tenant_id == principal.tenant_id)
            )
            if build is None:
                raise DomainError("not_found", "构建不存在")
            kb = self._scope(session, principal, build.kb_id, write=True, lock=True)
            source_job = session.get(JobRow, build.job_id)
            if source_job and source_job.payload.get("incremental") is True:
                if expected_release_id != source_job.payload.get("base_release_id"):
                    raise DomainError("version_conflict", "线上发布已变化，请重新准备更新")
                active = (
                    session.get(ReleaseRow, kb.active_release_id) if kb.active_release_id else None
                )
                if active is not None and active.build_id == build.id:
                    return _release(active)
            if kb.revision != build.kb_revision:
                raise DomainError("version_conflict", "资料已变化，请重新构建")
            return self._publish(session, principal, kb, build, expected_release_id)

    def list_releases(self, principal: Principal, kb_id: str) -> list[Release]:
        """历史记录不授权读取对应的过期权限内容。"""
        with self.transaction() as session:
            self._scope(session, principal, kb_id)
            return [
                _release(r)
                for r in session.scalars(
                    select(ReleaseRow)
                    .where(ReleaseRow.tenant_id == principal.tenant_id, ReleaseRow.kb_id == kb_id)
                    .order_by(ReleaseRow.version.desc())
                    .limit(100)
                )
            ]

    def get_active_release(
        self,
        principal: Principal,
        kb_id: str,
        *,
        allow_deleted_projection: bool = False,
    ) -> Release:
        """每次查询固定当前发布并核对撤销epoch，不从OpenSearch别名推测。"""
        with self.transaction() as session:
            kb = self._scope(session, principal, kb_id)
            if kb.active_release_id is None:
                raise DomainError("not_published", "知识库尚未发布")
            row = session.scalar(
                select(ReleaseRow).where(
                    ReleaseRow.id == kb.active_release_id,
                    ReleaseRow.tenant_id == principal.tenant_id,
                    ReleaseRow.kb_id == kb_id,
                )
            )
            if row is None:
                raise DomainError("database_unavailable", "发布引用不完整")
            if row.authz_epoch != kb.authz_epoch and not allow_deleted_projection:
                raise DomainError("authorization_pending", "权限撤销正在同步，暂时停止此知识库查询")
            return _release(row)

    def rollback_release(
        self, principal: Principal, release_id: str, expected_release_id: str | None
    ) -> Release:
        """追加发布指向旧内容，但当前授权epoch和CAS仍必须通过。"""
        with self.transaction() as session:
            previous = session.scalar(
                select(ReleaseRow).where(
                    ReleaseRow.id == release_id, ReleaseRow.tenant_id == principal.tenant_id
                )
            )
            if previous is None:
                raise DomainError("not_found", "发布不存在")
            kb = self._scope(session, principal, previous.kb_id, write=True, lock=True)
            build = session.get(BuildRow, previous.build_id, options=[defer(BuildRow.manifest)])
            if build is None:
                raise DomainError("not_found", "构建不存在")
            return self._publish(session, principal, kb, build, expected_release_id)

    def create_api_key(self, principal: Principal, kb_id: str, name: str) -> IssuedKey:
        """只有管理员签发只读KB凭证，原文只返回这一次。"""
        require_role(principal, admin=True)
        token = "ik_" + secrets.token_urlsafe(32)
        with self.transaction() as session:
            self._scope(session, principal, kb_id)
            row = ApiKeyRow(
                id=uuid4().hex,
                tenant_id=principal.tenant_id,
                kb_id=kb_id,
                name=name[:255],
                digest=hashlib.sha256(token.encode()).hexdigest(),
            )
            session.add(row)
            session.flush()
            self._audit(session, principal, "key.create", row.id, kb_id=kb_id)
            return IssuedKey(key_id=row.id, token=token, name=row.name, kb_id=kb_id)

    def list_api_keys(self, principal: Principal, kb_id: str) -> list[ApiKeyInfo]:
        """列举凭证元信息，不返回原文或摘要。"""
        require_role(principal, admin=True)
        with self.transaction() as session:
            self._scope(session, principal, kb_id)
            return [
                ApiKeyInfo(
                    key_id=r.id,
                    name=r.name,
                    kb_id=r.kb_id,
                    revoked=r.revoked,
                    created_at=r.created_at,
                )
                for r in session.scalars(
                    select(ApiKeyRow)
                    .where(ApiKeyRow.tenant_id == principal.tenant_id, ApiKeyRow.kb_id == kb_id)
                    .order_by(ApiKeyRow.created_at.desc())
                    .limit(100)
                )
            ]

    def revoke_api_key(self, principal: Principal, key_id: str) -> None:
        """数据库撤销下次认证立即生效，不依靠进程内缓存。"""
        require_role(principal, admin=True)
        with self.transaction() as session:
            row = session.scalar(
                select(ApiKeyRow)
                .where(ApiKeyRow.id == key_id, ApiKeyRow.tenant_id == principal.tenant_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                raise DomainError("not_found", "凭证不存在")
            self._scope(session, principal, row.kb_id)
            row.revoked = True
            self._audit(session, principal, "key.revoke", key_id, kb_id=row.kb_id)

    def authenticate_api_key(self, token: str) -> Principal | None:
        """服务凭证只获得绑定租户KB的只读身份，不接受客户端自己声明范围。"""
        if not token.startswith("ik_") or len(token) > 256:
            return None
        with self.transaction() as session:
            row = session.scalar(
                select(ApiKeyRow)
                .join(TenantRow, TenantRow.id == ApiKeyRow.tenant_id)
                .where(
                    ApiKeyRow.digest == hashlib.sha256(token.encode()).hexdigest(),
                    ApiKeyRow.revoked.is_(False),
                    TenantRow.active.is_(True),
                )
            )
            return (
                None
                if row is None
                else Principal(
                    tenant_id=row.tenant_id,
                    subject="api-key:" + row.id,
                    roles=["viewer"],
                    kb_ids=[row.kb_id],
                )
            )

    def list_audit(
        self,
        principal: Principal,
        kb_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """管理员审计按已存归属筛选；未知历史事件只留全租户视图，不冒充本库。"""
        require_role(principal, admin=True)
        if not 1 <= limit <= 100:
            raise DomainError("invalid_request", "审计分页无效")
        with self.transaction() as session:
            stmt = (
                select(AuditRow)
                .join(TenantRow, TenantRow.id == AuditRow.tenant_id)
                .where(
                    AuditRow.tenant_id == principal.tenant_id,
                    TenantRow.active.is_(True),
                )
            )
            if kb_id is not None:
                self._scope(session, principal, kb_id)
                stmt = stmt.where(AuditRow.kb_id == kb_id)
            elif principal.kb_ids is not None:
                stmt = stmt.where(AuditRow.kb_id.in_(principal.kb_ids))
            return [
                AuditEvent(
                    event_id=row.id,
                    kb_id=row.kb_id,
                    subject=row.subject,
                    action=row.action,
                    target_id=row.target_id,
                    created_at=row.created_at,
                )
                for row in session.scalars(
                    stmt.order_by(AuditRow.created_at.desc(), AuditRow.id).limit(limit)
                )
            ]

    def create_sample(
        self, principal: Principal, kb_id: str, query: str, expected_document_ids: list[str]
    ) -> EvaluationSample:
        """保存人工文档级标注；相关资料必须属于同一可见KB。"""
        if not query.strip() or len(query) > 4000 or not 1 <= len(expected_document_ids) <= 100:
            raise DomainError("invalid_request", "评测样本需要问题和1到100份相关资料")
        with self.transaction() as session:
            self._scope(session, principal, kb_id, write=True)
            for document_id in expected_document_ids:
                if self._doc(session, principal, document_id).kb_id != kb_id:
                    raise DomainError("invalid_request", "相关资料不属于此知识库")
            row = SampleRow(
                id=uuid4().hex,
                tenant_id=principal.tenant_id,
                kb_id=kb_id,
                query=query,
                expected_document_ids=sorted(set(expected_document_ids)),
            )
            session.add(row)
            session.flush()
            self._audit(session, principal, "sample.create", row.id, kb_id=row.kb_id)
            return EvaluationSample(
                sample_id=row.id,
                kb_id=kb_id,
                query=row.query,
                expected_document_ids=row.expected_document_ids,
                expected_sources=[
                    ExpectedSource.model_validate(value) for value in row.expected_sources
                ],
                no_answer=row.no_answer,
                annotation_revision=row.annotation_revision,
                supersedes_id=row.supersedes_id,
                created_at=row.created_at,
            )

    def list_samples(self, principal: Principal, kb_id: str) -> list[EvaluationSample]:
        """读取人工标注集，不能把合成命中当作质量标注。"""
        with self.transaction() as session:
            self._scope(session, principal, kb_id)
            return [
                EvaluationSample(
                    sample_id=r.id,
                    kb_id=kb_id,
                    query=r.query,
                    expected_document_ids=r.expected_document_ids,
                    expected_sources=[
                        ExpectedSource.model_validate(value) for value in r.expected_sources
                    ],
                    no_answer=r.no_answer,
                    annotation_revision=r.annotation_revision,
                    supersedes_id=r.supersedes_id,
                    created_at=r.created_at,
                )
                for r in session.scalars(
                    select(SampleRow)
                    .where(SampleRow.tenant_id == principal.tenant_id, SampleRow.kb_id == kb_id)
                    .order_by(SampleRow.created_at)
                    .limit(1000)
                )
            ]

    def record_evaluation(
        self,
        principal: Principal,
        kb_id: str,
        release_id: str,
        name: str,
        metrics: dict[str, JsonValue],
        expected_retrieval_epoch: int | None = None,
    ) -> Evaluation:
        """持久保存真实执行器计算的固定发布汇总，不由本仓库编造指标。"""
        with self.transaction() as session:
            kb = self._scope(session, principal, kb_id, write=True, lock=True)
            if (
                expected_retrieval_epoch is not None
                and kb.retrieval_epoch != expected_retrieval_epoch
            ):
                raise DomainError("version_conflict", "评测期间资料启用状态已变化")
            release = session.scalar(
                select(ReleaseRow).where(
                    ReleaseRow.id == release_id,
                    ReleaseRow.tenant_id == principal.tenant_id,
                    ReleaseRow.kb_id == kb_id,
                )
            )
            if release is None or release.authz_epoch != kb.authz_epoch:
                raise DomainError("version_conflict", "评测发布不存在或权限已变化")
            row = EvaluationRow(
                id=uuid4().hex,
                tenant_id=principal.tenant_id,
                kb_id=kb_id,
                release_id=release_id,
                name=name[:255],
                metrics=metrics,
            )
            session.add(row)
            session.flush()
            self._audit(session, principal, "evaluation.complete", row.id, kb_id=row.kb_id)
            return Evaluation(
                evaluation_id=row.id,
                kb_id=kb_id,
                release_id=release_id,
                name=row.name,
                metrics=row.metrics,
                created_at=row.created_at,
            )

    def record_job_evaluation(
        self,
        job_id: str,
        owner: str,
        epoch: int,
        release_id: str,
        name: str,
        metrics: dict[str, JsonValue],
        expected_retrieval_epoch: int | None = None,
    ) -> Evaluation:
        """租约、当前发布、评测保存与任务成功在同一PG事务中提交。"""
        with self.transaction() as session:
            peek = session.get(JobRow, job_id)
            if peek is None:
                raise DomainError("not_found", "任务不存在")
            principal = Principal(tenant_id=peek.tenant_id, subject="worker", roles=["editor"])
            kb = self._scope(session, principal, peek.kb_id, write=True, lock=True)
            job = self._leased(session, job_id, owner, epoch)
            if (
                expected_retrieval_epoch is not None
                and kb.retrieval_epoch != expected_retrieval_epoch
            ):
                raise DomainError("version_conflict", "评测期间资料启用状态已变化")
            release = session.scalar(
                select(ReleaseRow).where(
                    ReleaseRow.id == release_id,
                    ReleaseRow.tenant_id == job.tenant_id,
                    ReleaseRow.kb_id == job.kb_id,
                )
            )
            if (
                job.kind != "evaluate"
                or release is None
                or release.authz_epoch != kb.authz_epoch
                or kb.active_release_id != release_id
            ):
                raise DomainError("version_conflict", "评测发布或权限已改变")
            row = EvaluationRow(
                id=uuid4().hex,
                tenant_id=job.tenant_id,
                kb_id=job.kb_id,
                release_id=release_id,
                name=name[:255],
                metrics=metrics,
            )
            session.add(row)
            session.flush()
            job.result = {"evaluation_id": row.id, "release_id": release_id}
            job.status = "succeeded"
            job.stage = "complete"
            job.lease_owner = None
            job.lease_until = None
            job.updated_at = _now(session)
            self._audit(session, principal, "evaluation.complete", row.id, kb_id=row.kb_id)
            return Evaluation(
                evaluation_id=row.id,
                kb_id=job.kb_id,
                release_id=release_id,
                name=row.name,
                metrics=row.metrics,
                created_at=row.created_at,
            )

    def list_evaluations(self, principal: Principal, kb_id: str) -> list[Evaluation]:
        """读取有界真实评测历史，不回传原始秘密。"""
        with self.transaction() as session:
            self._scope(session, principal, kb_id)
            return [
                Evaluation(
                    evaluation_id=r.id,
                    kb_id=kb_id,
                    release_id=r.release_id,
                    name=r.name,
                    metrics=r.metrics,
                    created_at=r.created_at,
                )
                for r in session.scalars(
                    select(EvaluationRow)
                    .where(
                        EvaluationRow.tenant_id == principal.tenant_id, EvaluationRow.kb_id == kb_id
                    )
                    .order_by(EvaluationRow.created_at.desc())
                    .limit(100)
                )
            ]
