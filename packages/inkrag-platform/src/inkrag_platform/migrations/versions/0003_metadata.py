"""元数据声明与资料聚合版本和解析版本解耦，构建冻结实际字段类型。"""

from alembic import op

revision = "0003_metadata"
down_revision = "0002_integrity"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """以兼容默认值增加声明和同步版本，不改任何既有解析JSON。"""
    op.execute(
        "ALTER TABLE ip_knowledge_bases ADD COLUMN fields JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute("ALTER TABLE ip_documents ADD COLUMN indexed_revision INTEGER")
    op.execute(
        "UPDATE ip_documents SET indexed_revision=revision WHERE indexed_result_id=result_id"
    )
    op.execute("ALTER TABLE ip_builds ADD COLUMN field_schema JSONB NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    """只删除本次新增列，不移除资产或解析版本。"""
    op.drop_column("ip_builds", "field_schema")
    op.drop_column("ip_documents", "indexed_revision")
    op.drop_column("ip_knowledge_bases", "fields")
