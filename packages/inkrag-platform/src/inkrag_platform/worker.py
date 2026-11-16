"""独立Celery执行面：所有业务输入取PG，外部产物就绪后以租约条件提交。"""

from __future__ import annotations

import logging
from threading import Event, Lock, Thread
from types import TracebackType
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import httpx
from inkparse import ParseError, ParseOptions, load_stored_result, save_stored_result
from inkstore import AssetInfo, AssetStorage, StorageError, StoredRecord
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .broker import TASK_NAME, create_celery
from .imports import ImportRules, clean_result, execute_preview
from .models import Build, DomainError, Job, Principal
from .operations import Runtime, evaluate_release
from .repository import Repository
from .sandbox import run_parse
from .settings import Settings
from .wiring import open_runtime

if TYPE_CHECKING:
    from inkrag.search import SearchBackend
    from inkrag.search_models import FieldType, Scalar

logger = logging.getLogger(__name__)


class EvaluationInput(BaseModel):
    """只解释已在PG冻结的评测参数，不接受调用方覆盖tenant或索引路径。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    release_id: str
    name: str = Field(min_length=1, max_length=255)
    top_k: int = Field(default=5, ge=1, le=100)
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"
    sample_ids: list[str] = Field(min_length=1, max_length=100)


class BuildInputStorage:
    """按本任务实际读取的解析JSON累计工作量；不宣称网络或进程内存上限。"""

    def __init__(self, storage: AssetStorage, max_bytes: int) -> None:
        """借入底层资产服务，单产物仍遵守解析32MiB契约。"""
        self.storage, self.max_bytes = storage, max_bytes
        self.bytes_read = 0

    def load_record(self, record_id: str) -> StoredRecord:
        """读取一份有界记录后先记账，超额时在解码完整解析模型之前停止。"""
        record = self.storage.load_record(record_id)
        size = len(record.payload.encode())
        self.bytes_read += size
        if size > 32 * 1024 * 1024 or self.bytes_read > self.max_bytes:
            raise DomainError(
                "build_budget_exceeded", "本次构建读取的解析产物超过预算，请拆分知识库或评估扩容"
            )
        return record

    def get(self, asset_id: str) -> AssetInfo:
        """完整性检查仍交给资产实现，不把JSON工作量预算当成资源大小估计。"""
        return self.storage.get(asset_id)

    def read(self, asset_id: str) -> bytes:
        """保持资产只读能力，原件和图片仍遵守各自资源契约。"""
        return self.storage.read(asset_id)

    def put_bytes(self, data: bytes) -> AssetInfo:
        """构建读面不允许意外写入资产。"""
        raise DomainError("unsupported_operation", "构建读取包装器不支持写入")

    def save_record(self, record: StoredRecord) -> None:
        """构建读面不允许意外修改记录。"""
        raise DomainError("unsupported_operation", "构建读取包装器不支持写入")


class LeaseGuard:
    """独立短PG心跳不依赖长耗时解析/嵌入；失权后的检查与提交均失败。"""

    def __init__(self, repo: Repository, job: Job, owner: str, lease_seconds: int = 90) -> None:
        """只借入线程安全连接池，不在线程间共享SQLAlchemy session。"""
        self.repo, self.job, self.owner = repo, job, owner
        self.lease_seconds = lease_seconds
        self._stop, self._lost = Event(), Event()
        self._stage_lock = Lock()
        self._stage = "running"
        self._thread = Thread(target=self._heartbeat, name="inkrag-lease", daemon=True)

    def _heartbeat(self) -> None:
        """续约失败即保守失权；数据库恢复后不允许旧执行者自行复活。"""
        while not self._stop.wait(min(10, self.lease_seconds / 3)):
            with self._stage_lock:
                stage = self._stage
            try:
                self.repo.heartbeat(
                    self.job.job_id, self.owner, self.job.lease_epoch, stage, self.lease_seconds
                )
            except DomainError:
                self._lost.set()
                return

    def checkpoint(self, stage: str) -> None:
        """阶段边界立即核对当前租约，取消任务不会继续提交下一阶段。"""
        if self._lost.is_set():
            raise DomainError("lease_lost", "后台执行权已失效")
        with self._stage_lock:
            self._stage = stage
        self.repo.heartbeat(
            self.job.job_id, self.owner, self.job.lease_epoch, stage, self.lease_seconds
        )

    def __enter__(self) -> LeaseGuard:
        """启用心跳后才允许执行外部副作用。"""
        self.checkpoint("loading")
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """等待有界心跳结束，不吞掉业务失败。"""
        self._stop.set()
        self._thread.join(timeout=10)


def _principal(job: Job) -> Principal:
    """受信Worker从PG任务构造范围，消息只能提供job_id。"""
    return Principal(
        tenant_id=job.tenant_id,
        subject="worker:" + job.job_id,
        roles=["editor"],
        kb_ids=[job.kb_id],
    )


def _parse(runtime: Runtime, settings: Settings, job: Job, owner: str, guard: LeaseGuard) -> None:
    """读取原件并隔离解析，完整资产先入S3，版本与成功状态再同事务提交。"""
    principal = _principal(job)
    if job.document_id is None:
        raise DomainError("invalid_job", "解析任务缺少资料")
    document = runtime.repo.get_document(principal, job.document_id)
    if document.revision != job.expected_document_revision:
        raise DomainError("stale_job", "资料在任务执行前已经改变")
    storage = runtime.assets_for(principal)
    guard.checkpoint("reading_source")
    source_asset_id = job.payload.get("source_asset_id", document.source_asset_id)
    filename = job.payload.get("filename", document.name)
    if not isinstance(source_asset_id, str) or not isinstance(filename, str):
        raise DomainError("invalid_job", "解析任务的冻结原件信息无效")
    guard.checkpoint("parsing")
    parser_engine = job.payload.get("parser_engine", "builtin")
    if parser_engine not in {"builtin", "docling"}:
        raise DomainError("invalid_job", "持久任务中的解析工具无效")
    stored_rules = job.payload.get("import_rules")
    rules_data = stored_rules if isinstance(stored_rules, dict) else {}
    options = ParseOptions.model_validate(
        {
            "engine": parser_engine,
            "ocr_enabled": job.payload.get("ocr_enabled"),
            **{
                key: value
                for key, value in rules_data.items()
                if key in {"ocr_mode", "table_mode", "table_structure"} and value is not None
            },
        }
    )
    prepared = job.payload.get("prepared_result_id")
    result = (
        load_stored_result(storage, prepared)
        if isinstance(prepared, str)
        else run_parse(storage.read(source_asset_id), filename, storage, settings, options)
    )
    rules = job.payload.get("import_rules")
    if prepared is None and rules is not None:
        result = clean_result(result, ImportRules.model_validate(rules))
        save_stored_result(result, storage)
    if result.source.asset_id != source_asset_id:
        raise DomainError("invalid_artifact", "解析输出原件身份与任务输入不一致")
    guard.checkpoint("committing_parse")
    runtime.repo.complete_parse(
        job.job_id,
        owner,
        job.lease_epoch,
        result.result_id,
        result.revision,
        result.previous_result_id,
        [asset.asset_id for asset in result.assets],
        prepare_update=isinstance(prepared, str) or job.payload.get("replacement") is True,
    )


def _search(runtime: Runtime) -> SearchBackend:
    """只在RAG任务加载可选引擎，独立解析Worker无需导入该SDK。"""
    from inkrag.search import SearchBackend

    if not isinstance(runtime.search, SearchBackend):
        raise DomainError("search_unavailable", "当前执行服务未启用实际检索后端")
    return runtime.search


def _attributes(values: dict[str, JsonValue]) -> dict[str, Scalar]:
    """索引元数据只接受明确标量，拒绝把嵌套对象隐式转成字符串。"""
    result: dict[str, Scalar] = {}
    for name, value in values.items():
        if not isinstance(value, (str, int, float, bool)):
            raise DomainError("invalid_metadata", "索引元数据只支持文字、数字及布尔值")
        result[name] = value
    return result


def _previous_build(
    runtime: Runtime, settings: Settings, job: Job, model_revision: str
) -> Build | None:
    """完整恢复跳过旧物理索引，常规构建只复用同模型已发布候选。"""
    if job.payload.get("full_rebuild") is True:
        return None
    principal = _principal(job)
    try:
        release = runtime.repo.get_active_release(principal, job.kb_id)
    except DomainError as exc:
        if exc.code in {"not_published", "authorization_pending"}:
            return None
        raise
    if release.model_revision != model_revision:
        return None
    return runtime.repo.get_build(
        principal,
        release.build_id,
        max_documents=settings.max_build_documents,
        max_manifest_bytes=settings.max_manifest_bytes,
    )


def _build(runtime: Runtime, settings: Settings, job: Job, owner: str, guard: LeaseGuard) -> None:
    """候选复用未变资料向量，仅重嵌入变化项；冻结检查后PG提交ready，等待人工发布。"""
    from inkrag.models import Error, ProcessingConfig

    search = _search(runtime)
    build_id = uuid4().hex
    generation = search.generation_name(job.tenant_id, job.kb_id, build_id)
    build = runtime.repo.start_build(
        job.job_id,
        owner,
        job.lease_epoch,
        generation,
        search.model_revision,
        build_id,
        max_documents=settings.max_build_documents,
        max_manifest_bytes=settings.max_manifest_bytes,
    )
    previous = _previous_build(runtime, settings, job, search.model_revision)
    schema: dict[str, FieldType] = dict(build.field_schema)
    if previous is not None:
        old_schema: dict[str, FieldType] = dict(previous.field_schema)
        if old_schema.keys() - schema.keys() or any(
            name in schema and schema[name] != kind for name, kind in old_schema.items()
        ):
            previous = None
        else:
            schema = {**old_schema, **schema}
    guard.checkpoint("creating_candidate")
    try:
        if previous is not None and not search.reusable_generation(
            job.tenant_id, job.kb_id, previous.index_name
        ):
            previous = None
        actual = search.create_build(
            job.tenant_id,
            job.kb_id,
            build_id,
            previous_generation=previous.index_name if previous else None,
            field_schema=schema,
        )
        if actual != generation:
            raise DomainError("invalid_artifact", "实际候选索引与持久构建身份不一致")
        previous_documents = {d.document_id: d for d in previous.manifest} if previous else {}
        current_ids = {d.document_id for d in build.manifest}
        for removed in previous_documents.keys() - current_ids:
            guard.checkpoint("removing_old_nodes")
            search.remove_document(job.tenant_id, job.kb_id, generation, removed)
        storage = BuildInputStorage(
            runtime.assets_for(_principal(job)), settings.max_build_input_bytes
        )
        for document in build.manifest:
            if previous_documents.get(document.document_id) == document:
                continue
            guard.checkpoint("embedding_documents")
            result = load_stored_result(storage, document.result_id)
            search.upsert_result(
                job.tenant_id,
                job.kb_id,
                generation,
                document.document_id,
                result,
                metadata=_attributes(document.metadata),
                processing=ProcessingConfig.model_validate(document.processing),
            )
        guard.checkpoint("freezing_candidate")
        manifest = search.freeze(job.tenant_id, job.kb_id, generation)
        if manifest.document_count != len(build.manifest):
            raise DomainError("manifest_mismatch", "后端候选资料清单与冻结输入不一致")
        guard.checkpoint("committing_build")
        runtime.repo.complete_build(job.job_id, owner, job.lease_epoch, build_id)
    except Error as exc:
        raise DomainError(exc.code, "候选构建失败，现有发布保持原状态") from None


def _evaluate(runtime: Runtime, job: Job, owner: str, guard: LeaseGuard) -> None:
    """只计算PG已冻结的真实标注样本，并在任务租约事务中保存评测。"""
    if isinstance(job.payload.get("build_id"), str):
        from .workspace_query import evaluate_candidate

        evaluate_candidate(runtime, job, owner, lambda: guard.checkpoint("evaluating_candidate"))
        return
    value = EvaluationInput.model_validate(job.payload)
    guard.checkpoint("evaluating_release")
    evaluate_release(
        runtime,
        _principal(job),
        job.kb_id,
        value.release_id,
        value.name,
        value.top_k,
        value.mode,
        value.sample_ids,
        lease=(job.job_id, owner, job.lease_epoch),
    )


def _failure(exc: Exception) -> tuple[str, bool]:
    """只保存安全类别；确定性文档问题不自动重试，暂时依赖故障最多三次。"""
    if isinstance(exc, (DomainError, ParseError, StorageError)):
        code = exc.code
        return code, code in {
            "database_unavailable",
            "storage_failed",
            "storage_busy",
            "search_unavailable",
            "partial_search_result",
            "index_write_failed",
            "index_copy_failed",
            "activation_stale",
        }
    if isinstance(exc, httpx.HTTPError):
        return "embedding_unavailable", True
    if isinstance(exc, ValueError):
        return "invalid_input", False
    return "execution_failed", False


def execute_job(settings: Settings, job_id: str) -> None:
    """单次投递入口；重复成功/取消任务直接返回，连接故障不猜测事务是否提交。"""
    repo = Repository(settings.database_url.get_secret_value())
    owner = uuid4().hex
    job: Job | None = None
    try:
        repo.check()
        job = repo.claim_job(job_id, owner, lease_seconds=90)
        if job is None:
            return
        # 预览必须装配正式检索的tokenizer和预算；仅纯解析任务可裁剪检索依赖。
        runtime_settings = (
            settings.model_copy(update={"capabilities": "parse"})
            if job.kind == "parse"
            else settings
        )
        with LeaseGuard(repo, job, owner) as guard:
            with open_runtime(runtime_settings, batch=True) as runtime:
                if job.kind == "parse":
                    _parse(runtime, settings, job, owner, guard)
                elif job.kind == "import_preview":
                    guard.checkpoint("parsing")
                    preview = execute_preview(runtime, settings, job)
                    guard.checkpoint("committing_parse")
                    repo.finish_job(job.job_id, owner, job.lease_epoch, preview)
                elif job.kind in {"ingest", "build"}:
                    _build(runtime, settings, job, owner, guard)
                elif job.kind == "evaluate":
                    _evaluate(runtime, job, owner, guard)
                else:
                    raise DomainError("unsupported_job", "当前服务未实现此任务类型")
    except Exception as exc:
        if job is None:
            raise
        code, retry = _failure(exc)
        if code == "lease_lost":
            # 失权可能来自取消、其它副本接管或心跳连接异常；保留PG事实等待回收。
            return
        try:
            current = repo.worker_job(job.job_id)
            if current.status in {"succeeded", "cancelled"}:
                return
            diagnostic = (
                exc.diagnostic().model_copy(update={"retryable": retry}).model_dump(mode="json")
                if isinstance(exc, ParseError)
                else None
            )
            repo.fail_job(
                job.job_id, owner, job.lease_epoch, code, retry=retry, diagnostic=diagnostic
            )
        except DomainError as failure:
            if failure.code != "lease_lost":
                # 提交结果未知时交给 PG 恢复循环；普通异常的消息可能进入死信队列。
                raise DomainError("database_unavailable", "任务提交待数据库恢复后核对") from None
        logger.warning("job_failed job_id=%s code=%s retry=%s", job.job_id, code, retry)
    finally:
        repo.close()


def run_worker(settings: Settings) -> None:
    """启动低预取官方Celery worker，进程丢失重投仍须重新领取PG执行权。"""
    app = create_celery(settings)

    @app.task(name=TASK_NAME, ignore_result=True)
    def dispatch(job_id: str) -> None:
        """只接收消息身份，租户、资源、版本和执行权限始终从PG读取。"""
        execute_job(settings, job_id)

    app.worker_main(
        [
            "worker",
            "--loglevel=INFO",
            "--concurrency=" + str(settings.worker_concurrency),
            "--without-gossip",
            "--without-mingle",
        ]
    )
