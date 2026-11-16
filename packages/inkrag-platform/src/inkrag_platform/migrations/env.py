"""迁移通过调用方显式借入的管理员连接执行，不自动读取环境或运行时凭据。"""

from alembic import context
from sqlalchemy.engine import Connection

connection = context.config.attributes.get("connection")
if not isinstance(connection, Connection):
    raise RuntimeError("迁移必须显式提供数据库连接")
context.configure(
    connection=connection,
    target_metadata=None,
    version_table="ip_alembic_version",
    transactional_ddl=True,
)
with context.begin_transaction():
    context.run_migrations()
