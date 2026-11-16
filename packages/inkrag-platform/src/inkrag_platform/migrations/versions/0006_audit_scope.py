"""审计持久化可信知识库归属，无法唯一解释的旧记录保留全租户范围。"""

from alembic import op

revision = "0006_audit_scope"
down_revision = "0005_quality_samples"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """以历史实体身份做无歧义回填，不按当前页面或最近知识库猜测归属。"""
    op.execute("ALTER TABLE ip_audit ADD COLUMN kb_id VARCHAR(32)")
    op.execute("""WITH targets AS (
        SELECT tenant_id,id AS target_id,id AS kb_id FROM ip_knowledge_bases
        UNION ALL SELECT tenant_id,id,kb_id FROM ip_documents
        UNION ALL SELECT tenant_id,id,kb_id FROM ip_jobs
        UNION ALL SELECT tenant_id,id,kb_id FROM ip_releases
        UNION ALL SELECT tenant_id,id,kb_id FROM ip_api_keys
        UNION ALL SELECT tenant_id,id,kb_id FROM ip_samples
        UNION ALL SELECT tenant_id,id,kb_id FROM ip_evaluations
        UNION ALL SELECT tenant_id,id,kb_id FROM ip_candidate_evaluations
        UNION ALL SELECT r.tenant_id,r.result_id,d.kb_id FROM ip_result_versions r
            JOIN ip_documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
    ), unique_targets AS (
        SELECT tenant_id,target_id,min(kb_id) AS kb_id FROM targets
        GROUP BY tenant_id,target_id HAVING count(DISTINCT kb_id)=1
    ) UPDATE ip_audit a SET kb_id=t.kb_id FROM unique_targets t
        WHERE a.tenant_id=t.tenant_id AND a.target_id=t.target_id""")
    op.execute(
        "ALTER TABLE ip_audit ADD CONSTRAINT fk_audit_kb_scope FOREIGN KEY(tenant_id,kb_id) REFERENCES ip_knowledge_bases(tenant_id,id)"
    )
    op.execute("CREATE INDEX ix_audit_kb_scope ON ip_audit(tenant_id,kb_id,created_at DESC,id)")


def downgrade() -> None:
    """撤回归属索引与列，保留全部历史审计事件。"""
    op.execute("DROP INDEX ix_audit_kb_scope")
    op.execute("ALTER TABLE ip_audit DROP CONSTRAINT fk_audit_kb_scope")
    op.drop_column("ip_audit", "kb_id")
