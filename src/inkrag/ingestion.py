"""同步与未来后台入口共用的资料处理函数，任务和结果提交只有一套规则。"""

import os
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from inkparse import (
    FileSource,
    ParseError,
    ParseOptions,
    Parser,
    ParseRequest,
    load_result,
    save_stored_result,
)
from inkparse.io import MAX_JSON_BYTES
from inkstore import AssetStorage, StorageError
from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import TypeAdapter

from .database import Database, JobRow, ResourceRow, SpaceRow
from .documents import MAX_FILE_BYTES, local_result_nodes, processing_revision
from .lexical import LEGACY_LEXICAL, LexicalProfile
from .models import Error, IngestionResult, ProcessingConfig, Provenance, Source, StrictModel
from .storage import load_index, persist_index, vector_dimensions


class _IngestionOptions(StrictModel):
    """在已有任务配置列冻结解析、切分和分词版本，兼容历史任务。"""

    processing: ProcessingConfig
    parse_options: ParseOptions | None = None
    lexical_profile: LexicalProfile = LEGACY_LEXICAL


_JOB_OPTIONS = TypeAdapter(ProcessingConfig | _IngestionOptions)


def prepare_ingestion(
    database: Database,
    root: Path,
    path: Path,
    document_id: str,
    format_name: str,
    config: ProcessingConfig,
    *,
    parse_options: ParseOptions | None = None,
    lexical_profile: LexicalProfile = LEGACY_LEXICAL,
) -> str:
    """冻结任务输入并保存原件；同步入口与未来后台提交共用此登记动作。

    先保存任务意图，原件写入失败会留下明确失败状态。成功后任务只引用自己
    拥有的原件与配置，不依赖调用方之后可能改变的文件或配置对象。
    """
    state = database.space()
    frozen = (
        _IngestionOptions(
            processing=config, parse_options=parse_options, lexical_profile=lexical_profile
        )
        if parse_options is not None or lexical_profile != LEGACY_LEXICAL
        else config
    )
    job_id = uuid4().hex
    object_key = f"objects/{job_id}/original{path.suffix.lower()}"
    with database.sessions.begin() as session:
        previous = session.get(ResourceRow, document_id)
        resource_id = previous.resource_id if previous is not None else uuid4().hex
        revision = previous.revision + 1 if previous is not None else 1
        session.add(
            JobRow(
                id=job_id,
                resource_id=resource_id,
                target_revision=state.revision,
                document_id=document_id,
                resource_revision=revision,
                source_name=path.name,
                format_name=format_name,
                processing_json=frozen.model_dump_json(),
                object_key=object_key,
            )
        )
    try:
        original = root / object_key
        original.parent.mkdir(parents=True, exist_ok=False)
        if format_name == "parsed":
            max_bytes = MAX_JSON_BYTES
        else:
            max_bytes = parse_options.max_bytes if parse_options is not None else MAX_FILE_BYTES
        with path.open("rb") as source:
            data = source.read(max_bytes + 1)
        if not data or len(data) > max_bytes:
            raise Error("invalid_document", f"原件为空或超过读取预算（{max_bytes} 字节）")
        with original.open("xb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        return job_id
    except Exception as exc:
        raise_job_failure(database, job_id, exc)


def execute_ingestion(
    database: Database,
    root: Path,
    embedding: BaseEmbedding,
    job_id: str,
    *,
    asset_store: AssetStorage | None = None,
) -> IngestionResult:
    """根据已冻结任务执行唯一处理流程，外部 I/O 不持有 SQL 长事务。

    调用者须持有空间写锁。同步调用和未来 Worker 使用同一 job_id 执行入口；
    解析、嵌入和快照完成后才提交资料版本与任务成功，旧知识始终保持可见。
    """
    with database.sessions() as session:
        intent = session.get(JobRow, job_id)
        if intent is None or intent.status != "running":
            raise Error("invalid_job", "任务不存在或不处于可执行状态", job_id)
    snapshot_id = uuid4().hex
    resource_id, revision = intent.resource_id, intent.resource_revision
    document_id, object_key = intent.document_id, intent.object_key
    original = root / object_key
    commit_started = False
    try:
        state = database.space()
        if state.revision != intent.target_revision:
            raise Error("stale_revision", "资料版本已变化，请创建新的入库意图")
        frozen = _JOB_OPTIONS.validate_json(intent.processing_json)
        config = frozen.processing if isinstance(frozen, _IngestionOptions) else frozen
        lexical_profile = (
            frozen.lexical_profile if isinstance(frozen, _IngestionOptions) else LEGACY_LEXICAL
        )
        database.stage(job_id, "parse")
        parsed = None
        if intent.format_name == "parsed":
            try:
                parsed = load_result(original)
            except (ParseError, StorageError) as exc:
                raise Error(exc.code, str(exc)) from None
        try:
            if parsed is None:
                options = (
                    frozen.parse_options.model_dump()
                    if isinstance(frozen, _IngestionOptions) and frozen.parse_options is not None
                    else {}
                )
                parsed = Parser().parse(
                    ParseRequest(
                        source=FileSource(path=str(original), name=intent.source_name),
                        options=ParseOptions.model_validate(
                            {**options, "format": intent.format_name}
                        ),
                    ),
                    assets=asset_store,
                )
            elif parsed.source.asset_id is not None:
                if asset_store is None:
                    raise Error("storage_required", "此解析结果引用资产，请注入存储服务")
                save_stored_result(parsed, asset_store)
        except (ParseError, StorageError) as exc:
            raise Error(exc.code, str(exc)) from None
        origin = Provenance.model_validate(
            {
                "document_id": document_id,
                "resource_id": resource_id,
                "resource_revision": revision,
                "source": Source(name=intent.source_name, uri=object_key),
                "format": parsed.source.format,
                "processing_revision": processing_revision(config),
            }
        )
        database.stage(job_id, "chunk")
        nodes = local_result_nodes(
            parsed,
            origin,
            config,
            original_name=intent.source_name if intent.format_name != "parsed" else None,
        )
        index = load_index(root, state.snapshot_id, embedding)
        # 只替换同一资料的引用，其他文档及它们已计算的向量保持不变。
        for reference, info in list(index.ref_doc_info.items()):
            if info.metadata.get("resource_id") == resource_id:
                index.delete_ref_doc(reference, delete_from_docstore=True)
        database.stage(job_id, "embed")
        index.insert_nodes(nodes)
        dimensions = vector_dimensions(index, state.dimensions)
        database.stage(job_id, "index")
        persist_index(root, snapshot_id, index, lexical_profile=lexical_profile)
        database.stage(job_id, "commit")
        commit_started = True
        with database.sessions.begin() as session:
            current = session.get(SpaceRow, "default")
            job = session.get(JobRow, job_id)
            if current is None or job is None or current.revision != state.revision:
                raise Error("stale_revision", "处理期间知识版本变化，本次结果未提交")
            resource = session.get(ResourceRow, document_id)
            if resource is None:
                resource = ResourceRow(document_id=document_id, resource_id=resource_id)
                session.add(resource)
            resource.revision, resource.object_key, resource.node_count = (
                revision,
                object_key,
                len(nodes),
            )
            current.revision, current.snapshot_id, current.dimensions = (
                state.revision + 1,
                snapshot_id,
                dimensions,
            )
            job.status, job.stage, job.snapshot_id = "succeeded", "complete", snapshot_id
        return IngestionResult(
            document_id=document_id,
            resource_id=resource_id,
            resource_revision=revision,
            snapshot_id=snapshot_id,
            node_count=len(nodes),
            job_id=job_id,
        )
    except Exception as exc:
        if commit_started:
            committed = recover_committed_result(database, job_id, snapshot_id)
            if committed is not None:
                return committed
        raise_job_failure(database, job_id, exc)


def recover_committed_result(
    database: Database, job_id: str, snapshot_id: str
) -> IngestionResult | None:
    """提交响应异常后独立核对数据库，已提交返回凭据，无法核对保留未知状态。

    SQLite事务可见性是本地事实来源；不依靠原异常猜测已回滚，也不把确定成功
    的任务改成失败。调用方仍持写锁，不会被另一资料更新覆盖本次资料凭据。
    """
    try:
        with database.sessions() as session:
            job = session.get(JobRow, job_id)
            if job is not None and job.status == "running":
                return None
            if job is not None and job.status == "succeeded" and job.snapshot_id == snapshot_id:
                resource = session.get(ResourceRow, job.document_id)
                if resource is not None and resource.revision == job.resource_revision:
                    return IngestionResult(
                        document_id=job.document_id,
                        resource_id=job.resource_id,
                        resource_revision=job.resource_revision,
                        snapshot_id=snapshot_id,
                        node_count=resource.node_count,
                        job_id=job_id,
                    )
    except Exception:
        raise Error(
            "commit_unknown", "提交结果暂时无法核对，请凭任务ID检查，勿自动重放", job_id
        ) from None
    raise Error("commit_unknown", "提交状态不一致，请凭任务ID检查，勿自动重放", job_id)


def raise_job_failure(database: Database, job_id: str, exc: Exception) -> NoReturn:
    """保存安全失败并抛出带任务 ID 的业务错误，不修改当前资料或快照。"""
    code = exc.code if isinstance(exc, Error) else "ingestion_failed"
    message = str(exc) if isinstance(exc, Error) else "资料处理失败：" + type(exc).__name__
    database.fail(job_id, code, message)
    raise Error(code, message, job_id) from None
