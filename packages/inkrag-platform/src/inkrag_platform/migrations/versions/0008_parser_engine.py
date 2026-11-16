"""在资料指针上冻结创建时的解析工具，供列表与详情展示真实引擎。"""

from alembic import op

revision = "0008_parser_engine"
down_revision = "0007_activation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """历史资料按最近一次解析任务回填，无记录的退回内置解析。"""
    op.execute(
        "ALTER TABLE ip_documents ADD COLUMN parser_engine VARCHAR(32) NOT NULL DEFAULT 'builtin'"
    )
    op.execute(
        """
        UPDATE ip_documents d
        SET parser_engine = COALESCE(
            (SELECT j.payload->>'parser_engine'
             FROM ip_jobs j
             WHERE j.document_id = d.id AND j.kind = 'parse' AND j.tenant_id = d.tenant_id
             ORDER BY j.created_at DESC LIMIT 1),
            'builtin'
        )
        """
    )


def downgrade() -> None:
    """撤回引擎列，列表退回只按扩展名展示支持方式。"""
    op.drop_column("ip_documents", "parser_engine")
