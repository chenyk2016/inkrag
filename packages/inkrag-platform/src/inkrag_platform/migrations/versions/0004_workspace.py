"""独立解析集合、明确选用、服务草稿及固定候选可观测性。"""

from alembic import op

revision = "0004_workspace"
down_revision = "0003_metadata"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """兼容保留已发布选用，不把历史未审核资料自动加入下一次构建。"""
    op.execute(
        "ALTER TABLE ip_knowledge_bases ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'knowledge' CHECK (kind IN ('knowledge','parse'))"
    )
    op.execute(
        "ALTER TABLE ip_documents ADD COLUMN selected_result_id VARCHAR(64), ADD COLUMN selected_revision INTEGER, ADD COLUMN selected_metadata JSONB"
    )
    op.execute(
        "UPDATE ip_documents d SET selected_result_id=item->>'result_id',selected_revision=(item->>'revision')::integer,selected_metadata=item->'metadata' FROM ip_knowledge_bases k JOIN ip_releases r ON r.id=k.active_release_id AND r.tenant_id=k.tenant_id AND r.kb_id=k.id JOIN ip_builds b ON b.id=r.build_id AND b.tenant_id=r.tenant_id CROSS JOIN LATERAL jsonb_array_elements(b.manifest) item WHERE d.tenant_id=k.tenant_id AND d.kb_id=k.id AND d.id=item->>'document_id' AND d.indexed_result_id=item->>'result_id' AND NOT d.deleted"
    )
    op.execute(
        "ALTER TABLE ip_documents ADD CONSTRAINT ip_document_selection_complete CHECK ((selected_result_id IS NULL AND selected_revision IS NULL AND selected_metadata IS NULL) OR (selected_result_id IS NOT NULL AND selected_revision IS NOT NULL AND selected_metadata IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE ip_result_versions ADD COLUMN review_status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','approved')), ADD COLUMN reviewed_by VARCHAR(256), ADD COLUMN reviewed_at TIMESTAMPTZ"
    )
    op.execute(
        "UPDATE ip_result_versions r SET review_status='approved' FROM ip_documents d WHERE r.document_id=d.id AND r.result_id=d.selected_result_id"
    )
    op.execute("ALTER TABLE ip_jobs ADD COLUMN error_diagnostic JSONB")
    op.execute("ALTER TABLE ip_builds ADD COLUMN service_config JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute(
        "CREATE TABLE ip_services (kb_id VARCHAR(32) PRIMARY KEY,tenant_id VARCHAR(128) NOT NULL,revision INTEGER NOT NULL DEFAULT 1,config JSONB NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),FOREIGN KEY(tenant_id,kb_id) REFERENCES ip_knowledge_bases(tenant_id,id))"
    )
    op.execute(
        "CREATE TABLE ip_query_traces (id VARCHAR(32) PRIMARY KEY,tenant_id VARCHAR(128) NOT NULL,kb_id VARCHAR(32) NOT NULL,subject VARCHAR(256) NOT NULL,target_id VARCHAR(32) NOT NULL,target_kind VARCHAR(16) NOT NULL,request JSONB NOT NULL,outcome JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),FOREIGN KEY(tenant_id,kb_id) REFERENCES ip_knowledge_bases(tenant_id,id))"
    )
    op.execute(
        "CREATE TABLE ip_candidate_evaluations (id VARCHAR(32) PRIMARY KEY,tenant_id VARCHAR(128) NOT NULL,kb_id VARCHAR(32) NOT NULL,build_id VARCHAR(32) NOT NULL,name VARCHAR(255) NOT NULL,metrics JSONB NOT NULL,details JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),FOREIGN KEY(tenant_id,kb_id) REFERENCES ip_knowledge_bases(tenant_id,id))"
    )
    op.execute(
        "CREATE INDEX ip_workspace_kbs ON ip_knowledge_bases (tenant_id,kind,created_at DESC,id)"
    )
    op.execute(
        "CREATE INDEX ip_workspace_documents ON ip_documents (tenant_id,kb_id,created_at DESC,id) WHERE NOT deleted"
    )
    op.execute("CREATE INDEX ip_workspace_jobs ON ip_jobs (tenant_id,created_at DESC,id)")
    op.execute(
        "CREATE INDEX ip_workspace_job_filter ON ip_jobs (tenant_id,kb_id,status,kind,created_at DESC,id)"
    )
    op.execute(
        "CREATE INDEX ip_query_trace_list ON ip_query_traces (tenant_id,kb_id,created_at DESC,id)"
    )
    op.execute(
        "CREATE INDEX ip_candidate_evaluation_list ON ip_candidate_evaluations (tenant_id,kb_id,created_at DESC,id)"
    )
    op.execute(
        "ALTER TABLE ip_documents ADD CONSTRAINT fk_document_selected_result FOREIGN KEY(tenant_id,id,selected_result_id) REFERENCES ip_result_versions(tenant_id,document_id,result_id) DEFERRABLE INITIALLY DEFERRED"
    )
    op.execute(
        "ALTER TABLE ip_candidate_evaluations ADD CONSTRAINT fk_candidate_evaluation_build FOREIGN KEY(tenant_id,kb_id,build_id) REFERENCES ip_builds(tenant_id,kb_id,id)"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT FROM pg_roles WHERE rolname='inkrag_app') THEN GRANT SELECT,INSERT,UPDATE,DELETE ON ip_services,ip_query_traces,ip_candidate_evaluations TO inkrag_app; END IF; END $$"
    )


def downgrade() -> None:
    """仅撤销本次业务列及新事实表，不删除原件、解析版本或旧发布。"""
    for index in (
        "ip_workspace_kbs",
        "ip_workspace_documents",
        "ip_workspace_jobs",
        "ip_workspace_job_filter",
    ):
        op.execute("DROP INDEX " + index)
    for table in ("ip_candidate_evaluations", "ip_query_traces", "ip_services"):
        op.drop_table(table)
    op.drop_column("ip_builds", "service_config")
    op.drop_column("ip_jobs", "error_diagnostic")
    for column in ("review_status", "reviewed_by", "reviewed_at"):
        op.drop_column("ip_result_versions", column)
    op.execute("ALTER TABLE ip_documents DROP CONSTRAINT fk_document_selected_result")
    op.execute("ALTER TABLE ip_documents DROP CONSTRAINT ip_document_selection_complete")
    for column in ("selected_result_id", "selected_revision", "selected_metadata"):
        op.drop_column("ip_documents", column)
    op.drop_column("ip_knowledge_bases", "kind")
