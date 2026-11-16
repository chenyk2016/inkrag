"""用 SQLAlchemy 保存本地元数据，事务只管理可见性和执行状态。"""

from pathlib import Path

from sqlalchemy import URL, create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .models import Error, Job


class Base(DeclarativeBase):
    """声明新版元数据表，不读取或迁移旧 schema。"""


class SpaceRow(Base):
    """默认知识库的版本、模型契约与已提交快照引用。"""

    __tablename__ = "knowledge_spaces"
    id: Mapped[str] = mapped_column(primary_key=True)
    format_version: Mapped[int] = mapped_column(default=1)
    revision: Mapped[int] = mapped_column(default=0)
    model_name: Mapped[str]
    model_revision: Mapped[str]
    dimensions: Mapped[int | None]
    snapshot_id: Mapped[str | None]


class ResourceRow(Base):
    """当前资料版本和原件引用，不重复保存片段正文。"""

    __tablename__ = "resources"
    document_id: Mapped[str] = mapped_column(primary_key=True)
    resource_id: Mapped[str]
    revision: Mapped[int]
    object_key: Mapped[str]
    node_count: Mapped[int]


class JobRow(Base):
    """同步执行任务的阶段与安全失败原因，成功与资料提交同事务。"""

    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(primary_key=True)
    resource_id: Mapped[str]
    target_revision: Mapped[int]
    document_id: Mapped[str]
    resource_revision: Mapped[int]
    source_name: Mapped[str]
    format_name: Mapped[str]
    processing_json: Mapped[str]
    object_key: Mapped[str]
    status: Mapped[str] = mapped_column(default="running")
    stage: Mapped[str] = mapped_column(default="read")
    error_code: Mapped[str | None]
    message: Mapped[str | None]
    snapshot_id: Mapped[str | None]


class TraceRow(Base):
    """最小诊断记录，不保存问题正文或提供方原始异常。"""

    __tablename__ = "traces"
    id: Mapped[str] = mapped_column(primary_key=True)
    snapshot_id: Mapped[str | None]
    status: Mapped[str]
    error_code: Mapped[str | None]
    hit_count: Mapped[int] = mapped_column(default=0)


class Database:
    """持有元数据连接；业务事务由执行流程显式打开。"""

    def __init__(self, path: Path) -> None:
        """创建连接工厂但不执行 SQL，生命周期由 RAG.open/close 管理。"""
        self.engine = create_engine(
            URL.create("sqlite+pysqlite", database=str(path)), connect_args={"timeout": 30}
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.closed = False

    def close(self) -> None:
        """释放本实例的连接池并让查询入口拒绝自动重连。"""
        self.engine.dispose()
        self.closed = True

    def prepare(self, model_name: str, model_revision: str, *, fresh: bool) -> None:
        """仅为新目录建表；打开已有目录时检查格式和模型，拒绝隐式迁移。"""
        if fresh:
            Base.metadata.create_all(self.engine)
            with self.sessions.begin() as session:
                session.add(
                    SpaceRow(id="default", model_name=model_name, model_revision=model_revision)
                )
        with self.sessions.begin() as session:
            space = session.get(SpaceRow, "default")
            if space is None or space.format_version != 1:
                raise Error("invalid_storage", "知识目录不是受支持的新版格式")
            if (space.model_name, space.model_revision) != (model_name, model_revision):
                raise Error("model_conflict", "模型标识与知识目录不一致，请使用新目录")
            # 能获得空间写锁说明没有仍在执行的本地任务，遗留 running 属于中断执行。
            session.execute(
                update(JobRow)
                .where(JobRow.status == "running")
                .values(
                    status="failed", error_code="interrupted", message="上次执行中断，未提交新快照"
                )
            )

    def space(self) -> SpaceRow:
        """读取一个已提交的默认知识库状态，返回后不持有事务。"""
        with self.sessions() as session:
            space = session.get(SpaceRow, "default")
            if space is None:
                raise Error("invalid_storage", "知识库状态缺失")
            return space

    def stage(self, job_id: str, stage: str) -> None:
        """单独提交真实执行阶段，避免耗时 I/O 持有数据库事务。"""
        with self.sessions.begin() as session:
            session.execute(update(JobRow).where(JobRow.id == job_id).values(stage=stage))

    def fail(self, job_id: str, code: str, message: str) -> None:
        """记录失败而不修改当前资料与快照，原始依赖异常不进入记录。"""
        with self.sessions.begin() as session:
            session.execute(
                update(JobRow)
                .where(JobRow.id == job_id, JobRow.status == "running")
                .values(status="failed", error_code=code, message=message)
            )


class Jobs:
    """按任务 ID 查询实际执行状态，不读取整个知识空间。"""

    def __init__(self, database: Database) -> None:
        """借用当前知识目录的连接工厂，不单独拥有连接生命周期。"""
        self._database = database

    def get(self, job_id: str) -> Job:
        """返回单项任务；未找到时抛出业务错误，不构造空任务。"""
        if self._database.closed:
            raise Error("closed", "RAG 已关闭")
        with self._database.sessions() as session:
            row = session.scalar(select(JobRow).where(JobRow.id == job_id))
            if row is None:
                raise Error("not_found", "任务不存在")
            return Job.model_validate(
                {
                    "job_id": row.id,
                    "status": row.status,
                    "stage": row.stage,
                    "error_code": row.error_code,
                    "message": row.message,
                    "snapshot_id": row.snapshot_id,
                }
            )
