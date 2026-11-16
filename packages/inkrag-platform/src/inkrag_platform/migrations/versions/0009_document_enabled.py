"""增加资料检索开关与独立代次，不改变已有发布与解析版本。"""

from alembic import op

revision = "0009_document_enabled"
down_revision = "0008_parser_engine"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """历史资料默认启用，检索代次独立于权限与内容版本。"""
    op.execute("ALTER TABLE ip_documents ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute(
        "ALTER TABLE ip_knowledge_bases ADD COLUMN retrieval_epoch INTEGER NOT NULL DEFAULT 1"
    )


def downgrade() -> None:
    """撤回开关列，需部署旧代码时全部资料恢复其旧检索语义。"""
    op.drop_column("ip_documents", "enabled")
    op.drop_column("ip_knowledge_bases", "retrieval_epoch")
