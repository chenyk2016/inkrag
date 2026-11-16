"""人工标注绑定保存结果的细粒度来源，无答案与旧文档级样本兼容。"""

from alembic import op

revision = "0005_quality_samples"
down_revision = "0004_workspace"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """追加标注语义及不可变替代链，不重写已有问题或金标。"""
    op.execute(
        "ALTER TABLE ip_samples ADD COLUMN expected_sources JSONB NOT NULL DEFAULT '[]'::jsonb, ADD COLUMN no_answer BOOLEAN NOT NULL DEFAULT false, ADD COLUMN annotation_revision INTEGER NOT NULL DEFAULT 1 CHECK (annotation_revision>0), ADD COLUMN supersedes_id VARCHAR(32)"
    )
    op.execute("ALTER TABLE ip_samples ADD CONSTRAINT uq_sample_scope UNIQUE(tenant_id,kb_id,id)")
    op.execute(
        "ALTER TABLE ip_samples ADD CONSTRAINT fk_superseded_sample FOREIGN KEY(tenant_id,kb_id,supersedes_id) REFERENCES ip_samples(tenant_id,kb_id,id)"
    )
    op.execute(
        "ALTER TABLE ip_samples ADD CONSTRAINT ck_no_answer_labels CHECK (NOT no_answer OR (jsonb_array_length(expected_document_ids)=0 AND jsonb_array_length(expected_sources)=0))"
    )

    op.execute(
        "CREATE UNIQUE INDEX ip_sample_successor ON ip_samples(supersedes_id) WHERE supersedes_id IS NOT NULL"
    )


def downgrade() -> None:
    """仅撤销新标注字段，不清理历史问题和评测结果。"""
    op.execute(
        "ALTER TABLE ip_samples DROP CONSTRAINT ck_no_answer_labels, DROP CONSTRAINT fk_superseded_sample, DROP CONSTRAINT uq_sample_scope"
    )
    for column in ("supersedes_id", "annotation_revision", "no_answer", "expected_sources"):
        op.drop_column("ip_samples", column)
