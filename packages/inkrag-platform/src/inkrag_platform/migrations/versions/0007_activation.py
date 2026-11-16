"""保存知识库自动生效设置与资料的显式生效意图。"""

from alembic import op

revision = "0007_activation"
down_revision = "0006_audit_scope"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """默认开启新导入自动生效，已有资料不补发。"""
    op.execute(
        "ALTER TABLE ip_knowledge_bases ADD COLUMN auto_activate BOOLEAN NOT NULL DEFAULT true"
    )
    op.execute("ALTER TABLE ip_documents ADD COLUMN activation_mode VARCHAR(12)")


def downgrade() -> None:
    """撤回设置与待执行意图，保留已生效的历史版本。"""
    op.drop_column("ip_documents", "activation_mode")
    op.drop_column("ip_knowledge_bases", "auto_activate")
