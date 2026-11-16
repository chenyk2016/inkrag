"""持久保存导入时冻结的检索处理配置，旧资料空对象沿用核心默认。"""

from alembic import op

revision = "0010_document_processing"
down_revision = "0009_document_enabled"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """新增资料处理配置；构建清单原有JSON自然保存冻结配置。"""
    op.execute("ALTER TABLE ip_documents ADD COLUMN processing JSONB NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    """撤销处理配置列，部署旧代码前需评估已定制资料的处理行为。"""
    op.drop_column("ip_documents", "processing")
