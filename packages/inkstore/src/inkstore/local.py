"""独立的本地不可变资产与通用记录仓库；对象文件先就绪，SQLite短事务再提交引用。"""

import hashlib
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Annotated

from filelock import FileLock
from pydantic import Field, TypeAdapter, ValidationError

from .contracts import AssetId, AssetInfo, StorageError, StoredRecord
from .files import atomic_write

MAX_BYTES = 20 * 1024 * 1024


class LocalAssetStore:
    """可信本地目录的资产仓库，不提供HTTP、租户鉴权或云存储隐式回退。"""

    def __init__(self, root: Path) -> None:
        """持有目录与跨进程写锁，不打开持久连接。"""
        self._root = root
        self._lock = FileLock(str(root / ".write.lock"), timeout=30)

    @classmethod
    def open(cls, path: str | Path) -> "LocalAssetStore":
        """只初始化空目录，既有目录必须带本仓库标记及完整元数据。"""
        root = Path(path).resolve()
        marker = root / "inkstore.json"
        if root.exists() and (not root.is_dir() or (any(root.iterdir()) and not marker.is_file())):
            raise StorageError("invalid_store", "请选择全新资产目录或已初始化仓库")
        root.mkdir(parents=True, exist_ok=True)
        store = cls(root)
        with store._lock:
            if marker.exists():
                try:
                    if (
                        TypeAdapter(dict[str, int]).validate_json(marker.read_bytes(), strict=True)
                        != {"format": 1}
                        or not (root / "metadata.sqlite3").is_file()
                    ):
                        raise ValueError()
                    with store._session() as db:
                        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                            raise ValueError()
                        # 只核对已知模式，不扫描对象或修改既有仓库。
                        db.execute("SELECT id,size FROM assets LIMIT 0")
                        db.execute("SELECT id,source_id,payload FROM records LIMIT 0")
                        db.execute("SELECT result_id,asset_id,locator,role,page FROM links LIMIT 0")
                except (OSError, ValueError, StorageError):
                    raise StorageError("invalid_store", "资产仓库格式或元数据不完整") from None
            else:
                (root / "objects").mkdir()
                with store._session() as db:
                    db.executescript("""
                        CREATE TABLE assets (id TEXT PRIMARY KEY, size INTEGER NOT NULL);
                        CREATE TABLE records (id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES assets(id), payload TEXT NOT NULL);
                        CREATE TABLE links (result_id TEXT NOT NULL REFERENCES records(id), asset_id TEXT NOT NULL REFERENCES assets(id), locator TEXT NOT NULL, role TEXT NOT NULL, page INTEGER);
                        CREATE INDEX links_asset ON links(asset_id);
                        CREATE INDEX records_source ON records(source_id);
                        PRAGMA user_version=1;
                    """)
                atomic_write(marker, b'{"format":1}\n')
        return store

    def _connect(self) -> sqlite3.Connection:
        """每次操作创建短连接，强制外键并让调用方显式关闭。"""
        db = sqlite3.connect(self._root / "metadata.sqlite3", timeout=30)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _session(self) -> Generator[sqlite3.Connection, None, None]:
        """显式提交或回滚并关闭连接，数据库异常转换为安全存储错误。"""
        try:
            with closing(self._connect()) as db, db:
                yield db
        except sqlite3.Error:
            raise StorageError("storage_failed", "资产元数据读取或提交失败") from None

    def _path(self, asset_id: str) -> Path:
        """只从校验后的摘要产生内部路径，拒绝路径穿越与逃出仓库的符号链接。"""
        identity = TypeAdapter(AssetId).validate_python(asset_id, strict=True)
        path = self._root / "objects" / identity
        if not path.resolve().is_relative_to(self._root):
            raise StorageError("invalid_asset", "资产路径不在仓库内")
        return path

    def put_bytes(self, data: bytes) -> AssetInfo:
        """内容寻址保存并去重；先保存对象再登记元数据，失败不覆盖既有对象。"""
        if not data or len(data) > MAX_BYTES:
            raise StorageError("invalid_asset", "资产为空或超过20MiB")
        identity = hashlib.sha256(data).hexdigest()
        with self._lock:
            path = self._path(identity)
            if path.exists():
                if self._read_object(identity) != data:
                    raise StorageError("corrupt_asset", "已有资产内容不一致")
            else:
                atomic_write(path, data)
            with self._session() as db:
                db.execute(
                    "INSERT OR IGNORE INTO assets(id,size) VALUES (?,?)", (identity, len(data))
                )
        return self.get(identity)

    def put_file(self, path: str | Path) -> AssetInfo:
        """有界读取外部文件一次；同名新内容会获得不同资产ID。"""
        try:
            with Path(path).open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
        except OSError:
            raise StorageError("invalid_asset", "无法读取资产文件") from None
        return self.put_bytes(data)

    def _read_object(self, asset_id: str) -> bytes:
        """验证对象大小和摘要，丢失或损坏时不返回伪造空数据。"""
        try:
            with self._path(asset_id).open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
        except OSError:
            raise StorageError("missing_asset", "资产文件缺失或不可读取") from None
        if not data or len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != asset_id:
            raise StorageError("corrupt_asset", "资产完整性校验失败")
        return data

    def read(self, asset_id: str) -> bytes:
        """读取已登记的资产并复核字节完整性，不执行文件内容。"""
        self.get(asset_id)
        return self._read_object(asset_id)

    def get(self, asset_id: str) -> AssetInfo:
        """查询已登记资产，并检查元数据与实际文件一致。"""
        self._path(asset_id)
        with self._session() as db:
            row = db.execute("SELECT size FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise StorageError("missing_asset", "资产未登记")
        data = self._read_object(asset_id)
        if len(data) != row[0]:
            raise StorageError("corrupt_asset", "资产大小与元数据不一致")
        return AssetInfo(asset_id=asset_id, size_bytes=len(data))

    def save_record(self, record: StoredRecord) -> None:
        """验证原件与全部资源就绪后事务提交结果及关联，重复提交幂等。

        不改变record_id或业务负载；已有记录不可更新。对象成功而结果提交失败时
        可能留下未引用资产，允许显式删除，不把失败候选作为已保存记录。
        """
        record = StoredRecord.model_validate_json(record.model_dump_json())
        with self._lock:
            self.get(record.source_asset_id)
            for ref in record.references:
                self.get(ref.asset_id)
            with self._session() as db:
                existing = db.execute(
                    "SELECT payload FROM records WHERE id=?", (record.record_id,)
                ).fetchone()
                serialized = record.model_dump_json()
                if existing is not None:
                    if self._decode_record(existing[0], record.record_id) != record:
                        raise StorageError("record_conflict", "已有记录内容冲突")
                    return
                db.execute(
                    "INSERT INTO records(id,source_id,payload) VALUES (?,?,?)",
                    (record.record_id, record.source_asset_id, serialized),
                )
                db.executemany(
                    "INSERT INTO links VALUES (?,?,?,?,?)",
                    [
                        (record.record_id, r.asset_id, r.locator, r.role, r.page)
                        for r in record.references
                    ],
                )

    def load_record(self, record_id: str) -> StoredRecord:
        """读取不透明记录并确认引用完整，不解释解析或检索业务内容。"""
        TypeAdapter(AssetId).validate_python(record_id, strict=True)
        with self._session() as db:
            row = db.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise StorageError("missing_record", "记录不存在")
        record = self._decode_record(row[0], record_id)
        self.get(record.source_asset_id)
        for ref in record.references:
            self.get(ref.asset_id)
        return record

    @staticmethod
    def _decode_record(data: str, record_id: str) -> StoredRecord:
        """已持久化内容损坏时使用统一存储错误，不返回底层校验详情。"""
        try:
            record = StoredRecord.model_validate_json(data)
        except ValidationError:
            raise StorageError("corrupt_record", "存储记录格式不完整或已损坏") from None
        if record.record_id != record_id:
            raise StorageError("corrupt_record", "记录身份不一致")
        return record

    def list_assets(self, *, limit: int = 100, offset: int = 0) -> list[AssetInfo]:
        """分页列出元数据；需要使用内容时read/get会再次校验实际字节。"""
        limit = TypeAdapter(Annotated[int, Field(ge=1, le=1000)]).validate_python(
            limit, strict=True
        )
        offset = TypeAdapter(Annotated[int, Field(ge=0)]).validate_python(offset, strict=True)
        with self._session() as db:
            rows = db.execute(
                "SELECT id,size FROM assets ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [AssetInfo(asset_id=str(row[0]), size_bytes=int(row[1])) for row in rows]

    def list_records(self, source_asset_id: str | None = None) -> list[str]:
        """按原件资产查询已提交记录；只返回标识，不隐式读取文件。"""
        with self._session() as db:
            if source_asset_id is None:
                rows = db.execute("SELECT id FROM records ORDER BY rowid").fetchall()
            else:
                TypeAdapter(AssetId).validate_python(source_asset_id, strict=True)
                rows = db.execute(
                    "SELECT id FROM records WHERE source_id=? ORDER BY rowid", (source_asset_id,)
                ).fetchall()
        return [str(row[0]) for row in rows]

    def delete_unreferenced(self, asset_id: str) -> None:
        """只清理无记录引用的资产；已提交结果和其资源保持不可变。"""
        with self._lock:
            path = self._path(asset_id)
            with self._session() as db:
                referenced = db.execute(
                    "SELECT 1 FROM records WHERE source_id=? UNION ALL SELECT 1 FROM links WHERE asset_id=? LIMIT 1",
                    (asset_id, asset_id),
                ).fetchone()
                if referenced:
                    raise StorageError("asset_in_use", "资产被记录引用，不能删除")
                db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            try:
                path.unlink(missing_ok=True)
            except OSError:
                raise StorageError(
                    "write_failed", "资产元数据已移除，但未引用对象清理失败"
                ) from None
