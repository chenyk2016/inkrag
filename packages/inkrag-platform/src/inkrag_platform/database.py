"""PostgreSQL映射与连接生命周期；迁移只通过独立Alembic入口执行。"""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import JsonValue
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .models import DomainError


class Base(DeclarativeBase):
    """仅声明PG事实表；API启动不得自动create_all。"""


class TenantRow(Base):
    """来自可信身份发行方的租户登记，可禁用且不依赖客户端传入范围。"""

    __tablename__ = "ip_tenants"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeBaseRow(Base):
    """租户内KB及乐观版本、权限版本与发布指针。"""

    __tablename__ = "ip_knowledge_bases"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255))
    revision: Mapped[int] = mapped_column(default=1)
    authz_epoch: Mapped[int] = mapped_column(default=1)
    retrieval_epoch: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    active_release_id: Mapped[str | None] = mapped_column(String(32))
    auto_activate: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    fields: Mapped[list[dict[str, JsonValue]]] = mapped_column(JSONB, default=list)
    kind: Mapped[str] = mapped_column(String(16), default="knowledge")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentRow(Base):
    """当前资料指针；复合FK防止把资料挂入其他租户KB。"""

    __tablename__ = "ip_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "kb_id"], ["ip_knowledge_bases.tenant_id", "ip_knowledge_bases.id"]
        ),
        UniqueConstraint("tenant_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kb_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255))
    source_asset_id: Mapped[str] = mapped_column(String(64))
    result_id: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(default=0)
    indexed_result_id: Mapped[str | None] = mapped_column(String(64))
    indexed_revision: Mapped[int | None]
    selected_result_id: Mapped[str | None] = mapped_column(String(64))
    selected_revision: Mapped[int | None]
    selected_metadata: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    activation_mode: Mapped[str | None] = mapped_column(String(12))
    parser_engine: Mapped[str] = mapped_column(String(32), default="builtin")
    processing: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    attributes: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResultRow(Base):
    """版本元信息，不在业务目录重复存整份解析JSON。"""

    __tablename__ = "ip_result_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"], ["ip_documents.tenant_id", "ip_documents.id"]
        ),
        UniqueConstraint("tenant_id", "document_id", "revision"),
    )
    document_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int]
    previous_result_id: Mapped[str | None] = mapped_column(String(64))
    asset_ids: Mapped[list[str]] = mapped_column(JSONB)
    review_status: Mapped[str] = mapped_column(String(16), default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(256))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssetReferenceRow(Base):
    """HTTP资源访问必须同时通过此租户/资料所有权与当前授权。"""

    __tablename__ = "ip_asset_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"], ["ip_documents.tenant_id", "ip_documents.id"]
        ),
    )
    document_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)


class JobRow(Base):
    """持久任务与执行租约，状态不依赖Celery结果后端。"""

    __tablename__ = "ip_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "idempotency_key"),
        ForeignKeyConstraint(
            ["tenant_id", "kb_id"], ["ip_knowledge_bases.tenant_id", "ip_knowledge_bases.id"]
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(32), unique=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kb_id: Mapped[str] = mapped_column(String(32), index=True)
    document_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    result: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    expected_result_id: Mapped[str | None] = mapped_column(String(64))
    expected_document_revision: Mapped[int | None]
    request_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(256))
    attempt: Mapped[int] = mapped_column(default=0)
    lease_epoch: Mapped[int] = mapped_column(default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_diagnostic: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AttemptRow(Base):
    """每次领取保留审计，旧epoch不能复用新执行权。"""

    __tablename__ = "ip_job_attempts"
    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    epoch: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxRow(Base):
    """和任务同事务创建的投递事件，允许确认丢失后的重复投递。"""

    __tablename__ = "ip_outbox"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BuildRow(Base):
    """候选物理generation和冻结清单；不是搜索alias当前指针。"""

    __tablename__ = "ip_builds"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kb_id: Mapped[str] = mapped_column(String(32), index=True)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    index_name: Mapped[str] = mapped_column(String(255), unique=True)
    model_revision: Mapped[str] = mapped_column(String(255))
    kb_revision: Mapped[int]
    authz_epoch: Mapped[int]
    manifest: Mapped[list[dict[str, JsonValue]]] = mapped_column(JSONB)
    field_schema: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    service_config: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="building")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReleaseRow(Base):
    """不可变发布记录，active引用仅在PG事务中更新。"""

    __tablename__ = "ip_releases"
    __table_args__ = (UniqueConstraint("tenant_id", "kb_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kb_id: Mapped[str] = mapped_column(String(32), index=True)
    build_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int]
    index_name: Mapped[str] = mapped_column(String(255))
    model_revision: Mapped[str] = mapped_column(String(255))
    authz_epoch: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKeyRow(Base):
    """作用域绑定业务凭证，只保存256位随机原文的SHA256摘要。"""

    __tablename__ = "ip_api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kb_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditRow(Base):
    """租户范围操作事实，不记录原文/密钥。"""

    __tablename__ = "ip_audit"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kb_id: Mapped[str | None] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Database:
    """持有有界PG连接池，应用启动只校验schema，不替代迁移命令。"""

    def __init__(self, dsn: str) -> None:
        """拒绝SQLite与其它隐式后端，连接字符串不进入安全错误。"""
        if not dsn.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("生产仓库只支持PostgreSQL")
        if dsn.startswith("postgresql://"):
            dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
        self.engine = create_engine(
            dsn,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_timeout=10,
            connect_args={
                "connect_timeout": 5,
                "options": "-c statement_timeout=10000 -c lock_timeout=5000 "
                "-c idle_in_transaction_session_timeout=15000",
            },
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self) -> Generator[Session, None, None]:
        """短事务失败回滚并关闭；仅传播安全领域错误，不回显SQL或秘密。"""
        try:
            with self.sessions.begin() as session:
                yield session
        except SQLAlchemyError:
            raise DomainError("database_unavailable", "事务存储读取或提交失败") from None

    def check(self) -> None:
        """校验迁移版本和基本读取权限，不创建表或修复未知数据库。"""
        with self.transaction() as session:
            unsafe = session.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=current_user AND "
                    "(rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)) OR "
                    "EXISTS(SELECT 1 FROM pg_class WHERE oid='ip_knowledge_bases'::regclass "
                    "AND pg_get_userbyid(relowner)=current_user)"
                )
            )
            if unsafe:
                raise DomainError("unsafe_database_role", "运行时必须使用受限非owner数据库角色")
            expected = ScriptDirectory(str(Path(__file__).parent / "migrations")).get_heads()
            versions = list(session.scalars(text("SELECT version_num FROM ip_alembic_version")))
            # 校验随安装包发布的唯一迁移头，避免新增迁移后仍检查旧常量或漏掉多头。
            if len(expected) != 1 or versions != expected:
                raise DomainError("migration_required", "请先运行正式数据库迁移")

    def migrate(self) -> None:
        """显式运行版本化Alembic升级，调用方必须使用迁移角色凭据。"""
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    def close(self) -> None:
        """释放本实例连接池，不操作共享数据库或外部服务。"""
        self.engine.dispose()


class SampleRow(Base):
    """人工标注文档级相关性；样本永远带租户KB。"""

    __tablename__ = "ip_samples"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kb_id: Mapped[str] = mapped_column(String(32), index=True)
    query: Mapped[str] = mapped_column(String(4000))
    expected_document_ids: Mapped[list[str]] = mapped_column(JSONB)
    expected_sources: Mapped[list[dict[str, JsonValue]]] = mapped_column(JSONB, default=list)
    no_answer: Mapped[bool] = mapped_column(Boolean, default=False)
    annotation_revision: Mapped[int] = mapped_column(default=1)
    supersedes_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationRow(Base):
    """真实固定release评测的有限汇总，不保存问题或片段全文。"""

    __tablename__ = "ip_evaluations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kb_id: Mapped[str] = mapped_column(String(32), index=True)
    release_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    metrics: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceRow(Base):
    """知识库的服务草稿配置；候选构建冻结它后才影响发布。"""

    __tablename__ = "ip_services"
    kb_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int] = mapped_column(default=1)
    config: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QueryTraceRow(Base):
    """固定候选/发布的受控请求记录；不含密钥、原文或响应片段全文。"""

    __tablename__ = "ip_query_traces"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kb_id: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(256))
    target_id: Mapped[str] = mapped_column(String(32))
    target_kind: Mapped[str] = mapped_column(String(16))
    request: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    outcome: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CandidateEvaluationRow(Base):
    """候选评测逐题留存可复核来源，不要求先发布。"""

    __tablename__ = "ip_candidate_evaluations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kb_id: Mapped[str] = mapped_column(String(32), index=True)
    build_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    metrics: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    details: Mapped[list[dict[str, JsonValue]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
