"""唯一 SDK 入口，装配业务能力并管理本地资源，不依赖 HTTP 或全局模型设置。"""

import hashlib
import math
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from threading import Lock
from types import TracebackType
from uuid import uuid4

from filelock import FileLock
from inkparse import ParseOptions
from inkstore import AssetStorage
from llama_index.core import VectorStoreIndex
from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import TypeAdapter

from .database import Database, Jobs, TraceRow
from .documents import FORMATS
from .ingestion import execute_ingestion, prepare_ingestion
from .lexical import LEGACY_LEXICAL, LexicalProfile
from .local_retrieval import retrieve_local
from .models import (
    DocumentId,
    Error,
    HybridConfig,
    IngestionResult,
    ProcessingConfig,
    RetrievalMode,
    RetrievalResult,
)
from .published import PublishedQuery, PublishedRetrieval
from .retrieval import validate_query
from .storage import load_index, snapshot_lexical_profile, snapshot_path, vector_dimensions


class RAG:
    """打开本地知识目录，或借入宿主连接查询已发布知识库。"""

    def __init__(
        self,
        root: Path,
        embedding: BaseEmbedding,
        database: Database,
        assets: AssetStorage | None = None,
        *,
        write_lock_timeout: float = 30,
        lexical_profile: LexicalProfile | None = None,
    ) -> None:
        """借入模型并持有自己创建的连接；构造不进行网络或文件操作。"""
        self._root, self._embedding, self._database = root, embedding, database
        self._lock = FileLock(str(root / ".write.lock"), timeout=write_lock_timeout)
        self.jobs = Jobs(database)
        self._assets = assets
        self._lexical_profile: LexicalProfile | None = lexical_profile
        self._read_lock = Lock()
        self._cached_snapshot: str | None = None
        self._cached_index: VectorStoreIndex | None = None

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        embedding: BaseEmbedding,
        model_revision: str | None = None,
        assets: AssetStorage | None = None,
        write_lock_timeout: float = 30,
        lexical_profile: LexicalProfile | None = None,
    ) -> "RAG":
        """显式初始化新目录或打开新版目录，拒绝旧格式及不同模型。

        先判断目录再创建写锁，避免误写用户的旧知识或资料目录。首次初始化
        在写锁内完成；已有目录不会自动建表、迁移或修复丢失的快照。关键词设置
        只用于本次实例新建的快照。未指定时，入库继承当前快照的词项版本；
        空库以原规则开始，查询始终按已提交快照的版本执行。
        """
        if lexical_profile is not None:
            lexical_profile = TypeAdapter(LexicalProfile).validate_python(
                lexical_profile, strict=True
            )
        if (
            isinstance(write_lock_timeout, bool)
            or not math.isfinite(write_lock_timeout)
            or (write_lock_timeout < 0 and write_lock_timeout != -1)
        ):
            raise Error("invalid_storage", "write_lock_timeout 必须大于等于 0，或为 -1")
        root = Path(path).resolve()
        marker = root / "inkrag.json"
        if root.exists() and (not root.is_dir() or (any(root.iterdir()) and not marker.is_file())):
            raise Error("invalid_storage", "目录包含其他内容，请选择全新知识目录")
        name = embedding.model_name.strip()
        if not name or name == "unknown":
            raise Error("invalid_model", "传入的模型必须设置明确 model_name")
        revision = model_revision if model_revision is not None else name
        if not revision.strip():
            raise Error("invalid_model", "model_revision 不能为空")
        root.mkdir(parents=True, exist_ok=True)
        database = Database(root / "metadata.sqlite3")
        rag = cls(
            root,
            embedding,
            database,
            assets,
            write_lock_timeout=write_lock_timeout,
            lexical_profile=lexical_profile,
        )
        try:
            with rag._lock:
                fresh = not marker.exists()
                if not fresh and (
                    TypeAdapter(dict[str, int]).validate_json(marker.read_bytes(), strict=True)
                    != {"format": 1}
                    or not (root / "metadata.sqlite3").is_file()
                ):
                    raise Error("invalid_storage", "知识目录格式或元数据不完整")
                database.prepare(name, revision, fresh=fresh)
                if fresh:
                    marker.write_text('{"format":1}\n', encoding="utf-8")
                state = database.space()
                if state.snapshot_id is not None:
                    index = rag._read_index(state.snapshot_id)
                    vector_dimensions(index, state.dimensions)
            return rag
        except Error:
            database.close()
            raise
        except Exception as exc:
            database.close()
            raise Error("invalid_storage", "知识目录读取失败：" + type(exc).__name__) from None

    @staticmethod
    @contextmanager
    def open_published(
        connection: AbstractContextManager[PublishedQuery],
    ) -> Generator[PublishedRetrieval]:
        """打开宿主提供的已发布库查询连接；离开上下文后停止使用该连接。"""
        with connection as execute:
            reader = PublishedRetrieval(execute)
            try:
                yield reader
            finally:
                reader.close()

    @property
    def assets(self) -> AssetStorage:
        """返回调用方借入的资产服务；未配置时明确拒绝，不隐式创建本地目录。"""
        self._ensure_open()
        if self._assets is None:
            raise Error("storage_required", "未注入资产存储服务")
        return self._assets

    def ingest(
        self,
        source: str | Path,
        *,
        document_id: str | None = None,
        processing: ProcessingConfig | None = None,
        parse_options: ParseOptions | None = None,
    ) -> IngestionResult:
        """使用独立解析核心读取本地原件，同步提交知识，失败保留旧快照。"""
        return self._ingest_file(
            source, document_id=document_id, processing=processing, parse_options=parse_options
        )

    def ingest_parsed(
        self,
        source: str | Path,
        *,
        document_id: str | None = None,
        processing: ProcessingConfig | None = None,
    ) -> IngestionResult:
        """读取inkparse交换JSON；验证、切分、嵌入与提交复用原入库流程。

        本地路径只作为输入载体，包内原件名称与SHA另存来源；不会抓取包中
        的外部链接或原始路径。损坏/未知协议留下失败任务且不替换旧知识。
        """
        return self._ingest_file(
            source,
            document_id=document_id,
            processing=processing,
            parsed=True,
        )

    def _ingest_file(
        self,
        source: str | Path,
        *,
        document_id: str | None = None,
        processing: ProcessingConfig | None = None,
        parsed: bool = False,
        parse_options: ParseOptions | None = None,
    ) -> IngestionResult:
        """同步提交一个本地文件，失败保留原知识，同身份更新产生新资料版本。"""
        self._ensure_open()
        try:
            path = Path(source).resolve(strict=True)
        except OSError:
            raise Error("invalid_document", "无法读取资料文件") from None
        format_name = "parsed" if parsed else FORMATS.get(path.suffix.lower())
        if parse_options is not None and parse_options.format != "auto":
            format_name = parse_options.format
        if not path.is_file() or format_name is None:
            raise Error("unsupported_document", "文件格式未支持，请查看独立解析包的格式能力")
        identity = TypeAdapter(DocumentId).validate_python(
            document_id
            if document_id is not None
            else hashlib.sha256(str(path).encode()).hexdigest(),
            strict=True,
        )
        config = processing if processing is not None else ProcessingConfig()
        with self._lock:
            profile = self._lexical_profile
            if profile is None:
                snapshot_id = self._database.space().snapshot_id
                profile = (
                    snapshot_lexical_profile(self._root, snapshot_id)
                    if snapshot_id is not None
                    else LEGACY_LEXICAL
                )
            job_id = prepare_ingestion(
                self._database,
                self._root,
                path,
                identity,
                format_name,
                config,
                parse_options=parse_options,
                lexical_profile=profile,
            )
            return execute_ingestion(
                self._database, self._root, self._embedding, job_id, asset_store=self._assets
            )

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 3,
        mode: RetrievalMode = "vector",
        hybrid_config: HybridConfig | None = None,
    ) -> RetrievalResult:
        """从同一已提交快照执行向量、关键词或混合检索，默认沿用向量模式。

        读取者只持有自己加载的快照，不阻塞写入，也不会读到候选目录。
        模型或存储异常会先保存失败诊断，绝不保留默认成功状态。
        """
        self._ensure_open()
        prepared = validate_query(query, top_k, mode)
        if hybrid_config is not None and prepared.mode != "hybrid":
            raise Error("invalid_retrieval_options", "hybrid_config 仅适用于 hybrid 模式")
        state = self._database.space()
        trace_id = uuid4().hex
        trace = TraceRow(id=trace_id, snapshot_id=state.snapshot_id, status="failed")
        try:
            hits = []
            if state.snapshot_id is not None:
                hits = retrieve_local(
                    snapshot_path(self._root, state.snapshot_id),
                    self._read_index(state.snapshot_id),
                    self._embedding,
                    state.dimensions,
                    prepared,
                    hybrid_config,
                )
            trace.status, trace.hit_count = "success" if hits else "empty", len(hits)
            return RetrievalResult(hits=hits, trace_id=trace_id, snapshot_id=state.snapshot_id)
        except Exception as exc:
            code = exc.code if isinstance(exc, Error) else "retrieval_failed"
            trace.error_code = code
            message = str(exc) if isinstance(exc, Error) else "检索失败：" + type(exc).__name__
            raise Error(code, message, trace_id) from None
        finally:
            with self._database.sessions.begin() as session:
                session.add(trace)

    def _read_index(self, snapshot_id: str) -> VectorStoreIndex:
        """只缓存最近一个已提交快照，按数据库版本失效；写入始终使用独立索引。

        短锁仅保护加载与指针替换，并发读取在锁外使用不可变实例。不同进程提交后
        下次查询读取的新snapshot_id会换缓存；磁盘完整性在加载/重开时校验。
        """
        with self._read_lock:
            if self._cached_snapshot == snapshot_id and self._cached_index is not None:
                return self._cached_index
            index = load_index(self._root, snapshot_id, self._embedding)
            self._cached_index = index
            self._cached_snapshot = snapshot_id
            return index

    def _ensure_open(self) -> None:
        """关闭后拒绝继续操作，防止 SQLAlchemy 自动重连掩盖生命周期错误。"""
        if self._database.closed:
            raise Error("closed", "RAG 已关闭")

    def close(self) -> None:
        """关闭自有数据库连接，不释放调用方借入的模型对象。"""
        self._database.close()
        with self._read_lock:
            self._cached_index = None
            self._cached_snapshot = None

    def __enter__(self) -> "RAG":
        """进入已打开的知识目录上下文。"""
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """无论业务是否抛错都释放当前实例的自有连接。"""
        self.close()
