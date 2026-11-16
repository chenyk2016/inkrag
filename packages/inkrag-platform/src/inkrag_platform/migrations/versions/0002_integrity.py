"""把范围引用、租户停用与高频领取索引固定为数据库约束。"""

from alembic import op

revision = "0002_integrity"
down_revision = "0001_production"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """仅升级本平台表，不接触历史SQLite或其它应用schema。"""
    op.execute(
        "CREATE TABLE ip_tenants (id VARCHAR(128) PRIMARY KEY, active BOOLEAN NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    op.execute(
        "INSERT INTO ip_tenants(id,active) SELECT DISTINCT tenant_id,true FROM ip_knowledge_bases"
    )
    statements = (
        "ALTER TABLE ip_knowledge_bases ADD CONSTRAINT fk_kb_tenant FOREIGN KEY (tenant_id) REFERENCES ip_tenants(id)",
        "ALTER TABLE ip_documents ADD CONSTRAINT uq_documents_scope UNIQUE(tenant_id,kb_id,id)",
        "ALTER TABLE ip_jobs ADD CONSTRAINT uq_jobs_scope UNIQUE(tenant_id,kb_id,id)",
        "ALTER TABLE ip_jobs ADD CONSTRAINT fk_job_document_scope FOREIGN KEY(tenant_id,kb_id,document_id) REFERENCES ip_documents(tenant_id,kb_id,id)",
        "ALTER TABLE ip_builds ADD CONSTRAINT uq_builds_scope UNIQUE(tenant_id,kb_id,id)",
        "ALTER TABLE ip_builds ADD CONSTRAINT fk_build_job_scope FOREIGN KEY(tenant_id,kb_id,job_id) REFERENCES ip_jobs(tenant_id,kb_id,id)",
        "ALTER TABLE ip_releases ADD CONSTRAINT uq_releases_scope UNIQUE(tenant_id,kb_id,id)",
        "ALTER TABLE ip_releases ADD CONSTRAINT fk_release_build_scope FOREIGN KEY(tenant_id,kb_id,build_id) REFERENCES ip_builds(tenant_id,kb_id,id)",
        "ALTER TABLE ip_knowledge_bases ADD CONSTRAINT fk_active_release_scope FOREIGN KEY(tenant_id,id,active_release_id) REFERENCES ip_releases(tenant_id,kb_id,id) DEFERRABLE INITIALLY DEFERRED",
        "ALTER TABLE ip_api_keys ADD CONSTRAINT fk_key_kb_scope FOREIGN KEY(tenant_id,kb_id) REFERENCES ip_knowledge_bases(tenant_id,id)",
        "ALTER TABLE ip_samples ADD CONSTRAINT fk_sample_kb_scope FOREIGN KEY(tenant_id,kb_id) REFERENCES ip_knowledge_bases(tenant_id,id)",
        "ALTER TABLE ip_evaluations ADD CONSTRAINT fk_evaluation_release_scope FOREIGN KEY(tenant_id,kb_id,release_id) REFERENCES ip_releases(tenant_id,kb_id,id)",
        "ALTER TABLE ip_audit ADD CONSTRAINT fk_audit_tenant FOREIGN KEY(tenant_id) REFERENCES ip_tenants(id)",
        "ALTER TABLE ip_outbox ADD CONSTRAINT fk_outbox_job FOREIGN KEY(job_id) REFERENCES ip_jobs(id) ON DELETE CASCADE",
        "ALTER TABLE ip_job_attempts ADD CONSTRAINT fk_attempt_job FOREIGN KEY(job_id) REFERENCES ip_jobs(id) ON DELETE CASCADE",
        "ALTER TABLE ip_result_versions ADD CONSTRAINT uq_result_scope UNIQUE(tenant_id,document_id,result_id)",
        "ALTER TABLE ip_documents ADD CONSTRAINT fk_document_result FOREIGN KEY(tenant_id,id,result_id) REFERENCES ip_result_versions(tenant_id,document_id,result_id) DEFERRABLE INITIALLY DEFERRED",
        "ALTER TABLE ip_documents ADD CONSTRAINT fk_document_indexed_result FOREIGN KEY(tenant_id,id,indexed_result_id) REFERENCES ip_result_versions(tenant_id,document_id,result_id) DEFERRABLE INITIALLY DEFERRED",
        "ALTER TABLE ip_documents ADD CONSTRAINT ck_document_revision CHECK(revision>=0)",
        "ALTER TABLE ip_jobs ADD CONSTRAINT ck_job_attempt CHECK(attempt>=0 AND lease_epoch>=0)",
        "CREATE INDEX ix_outbox_pending ON ip_outbox(created_at) WHERE status='pending'",
        "CREATE INDEX ix_outbox_expired ON ip_outbox(lease_until) WHERE status='sending'",
        "CREATE INDEX ix_jobs_expired ON ip_jobs(lease_until) WHERE status='running'",
        "CREATE INDEX ix_jobs_retry ON ip_jobs(next_retry_at) WHERE status='retry_wait'",
        "CREATE INDEX ix_documents_listing ON ip_documents(tenant_id,kb_id,created_at DESC,id) WHERE NOT deleted",
        "CREATE INDEX ix_jobs_listing ON ip_jobs(tenant_id,kb_id,created_at DESC,id)",
        "CREATE INDEX ix_audit_listing ON ip_audit(tenant_id,created_at DESC,id)",
    )
    for statement in statements:
        op.execute(statement)
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT FROM pg_roles WHERE rolname='inkrag_app') THEN "
        "GRANT SELECT,INSERT,UPDATE,DELETE ON ip_tenants TO inkrag_app; END IF; END $$"
    )


def downgrade() -> None:
    """移除本次约束与索引，保留既有业务资料；只在显式迁移命令使用。"""
    constraints = (
        ("ip_documents", "fk_document_indexed_result"),
        ("ip_documents", "fk_document_result"),
        ("ip_result_versions", "uq_result_scope"),
        ("ip_job_attempts", "fk_attempt_job"),
        ("ip_outbox", "fk_outbox_job"),
        ("ip_audit", "fk_audit_tenant"),
        ("ip_evaluations", "fk_evaluation_release_scope"),
        ("ip_samples", "fk_sample_kb_scope"),
        ("ip_api_keys", "fk_key_kb_scope"),
        ("ip_knowledge_bases", "fk_active_release_scope"),
        ("ip_releases", "fk_release_build_scope"),
        ("ip_releases", "uq_releases_scope"),
        ("ip_builds", "fk_build_job_scope"),
        ("ip_builds", "uq_builds_scope"),
        ("ip_jobs", "fk_job_document_scope"),
        ("ip_jobs", "uq_jobs_scope"),
        ("ip_documents", "uq_documents_scope"),
        ("ip_knowledge_bases", "fk_kb_tenant"),
        ("ip_documents", "ck_document_revision"),
        ("ip_jobs", "ck_job_attempt"),
    )
    for table, constraint in constraints:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
    for name in (
        "ix_outbox_pending",
        "ix_outbox_expired",
        "ix_jobs_expired",
        "ix_jobs_retry",
        "ix_documents_listing",
        "ix_jobs_listing",
        "ix_audit_listing",
    ):
        op.drop_index(name)
    op.drop_table("ip_tenants")
