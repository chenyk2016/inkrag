"""用 SQLite 保存本地片段、向量和显式分词后的倒排索引。"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from collections.abc import Generator, Iterator, Sequence
from contextlib import closing, contextmanager
from heapq import nsmallest
from pathlib import Path
from struct import pack, unpack

from .contracts import Chunk, Hit, IndexedChunk, IndexProfile, Source, validate_vector

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inkrag_core_collections (
    name TEXT PRIMARY KEY, embedding_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL, analyzer_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inkrag_core_chunks (
    collection TEXT NOT NULL REFERENCES inkrag_core_collections(name), id TEXT NOT NULL,
    document_id TEXT NOT NULL, text TEXT NOT NULL, name TEXT NOT NULL, element_ref TEXT NOT NULL,
    page INTEGER, result_id TEXT, sha256 TEXT, embedding BLOB NOT NULL, length INTEGER NOT NULL,
    PRIMARY KEY (collection, id)
);
CREATE INDEX IF NOT EXISTS inkrag_core_document ON inkrag_core_chunks (collection, document_id);
CREATE TABLE IF NOT EXISTS inkrag_core_postings (
    collection TEXT NOT NULL, chunk_id TEXT NOT NULL, term TEXT NOT NULL, frequency INTEGER NOT NULL,
    PRIMARY KEY (collection, term, chunk_id),
    FOREIGN KEY (collection, chunk_id) REFERENCES inkrag_core_chunks(collection, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS inkrag_core_postings_chunk ON inkrag_core_postings (collection, chunk_id);
"""

_SCOPE = """collection=:collection AND (
    :scope IS NULL OR document_id IN (SELECT value FROM json_each(:scope))
)"""
_KEYWORD = f"""
WITH scoped AS (SELECT id, length FROM inkrag_core_chunks WHERE {_SCOPE}),
corpus AS (SELECT count(*) AS n, avg(length) AS mean_length FROM scoped),
matches AS (
    SELECT p.chunk_id, p.term, p.frequency, s.length
    FROM inkrag_core_postings p JOIN scoped s ON s.id=p.chunk_id
    WHERE p.collection=:collection AND p.term IN (SELECT value FROM json_each(:terms))
), counts AS (SELECT term, count(*) AS df FROM matches GROUP BY term),
ranked AS (
    SELECT chunk_id, sum(
        ln(1.0 + (n-df+0.5)/(df+0.5))
        * (frequency*2.2)/(frequency+1.2*(0.25+0.75*length/mean_length))
    ) AS score
    FROM matches JOIN counts USING (term) CROSS JOIN corpus GROUP BY chunk_id
)
SELECT c.id, c.document_id, c.text, c.name, c.element_ref, c.page, c.result_id, c.sha256, r.score
FROM ranked r JOIN inkrag_core_chunks c ON c.collection=:collection AND c.id=r.chunk_id
ORDER BY r.score DESC, c.id LIMIT :limit
"""
_CHUNK = "id, document_id, text, name, element_ref, page, result_id, sha256"

type _ChunkRow = tuple[str, str, str, str, str, int | None, str | None, str | None]


def _chunk(row: _ChunkRow) -> Chunk:
    """将数据库行还原为片段及原文引用。"""
    identity, document, text, name, element, page, result, sha = row
    return Chunk(identity, document, text, Source(name, element, page, result, sha))


def _scope(identities: frozenset[str] | None) -> str | None:
    """保留不加范围和明确空范围的区别，参数始终作为 JSON 数据绑定。"""
    return None if identities is None else json.dumps(sorted(identities), ensure_ascii=False)


class SQLiteIndex:
    """在一个数据库文件内按集合隔离索引，创建后固定分词和向量版本。"""

    def __init__(self, path: str | Path, collection: str, profile: IndexProfile) -> None:
        """创建本地索引表或校验已有集合；不同配置需要使用新集合。"""
        self._path, self._collection, self._profile = Path(path).resolve(), collection, profile
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO inkrag_core_collections VALUES (?, ?, ?, ?)",
                    (collection, profile.embedding_id, profile.dimensions, profile.analyzer_id),
                )
                self._check_profile(connection)

    @property
    def profile(self) -> IndexProfile:
        """返回本集合已经校验的索引配置。"""
        return self._profile

    def _connect(self) -> sqlite3.Connection:
        """每次操作借用独立连接，所有写连接启用倒排删除约束。"""
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.create_function("ln", 1, math.log, deterministic=True)
        return connection

    def _check_profile(self, connection: sqlite3.Connection) -> None:
        """拒绝用错误分词或向量配置读取和覆盖数据。"""
        row = connection.execute(
            "SELECT embedding_id, dimensions, analyzer_id FROM inkrag_core_collections WHERE name=?",
            (self._collection,),
        ).fetchone()
        profile = self._profile
        if row != (profile.embedding_id, profile.dimensions, profile.analyzer_id):
            raise ValueError("SQLite 集合缺失或索引配置不匹配")

    def replace(self, document_id: str, rows: Sequence[IndexedChunk]) -> None:
        """在一个写事务替换片段和倒排；空集合删除，失败回滚旧资料。"""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._check_profile(connection)
            connection.execute(
                "DELETE FROM inkrag_core_chunks WHERE collection=? AND document_id=?",
                (self._collection, document_id),
            )
            for row in rows:
                chunk, source = row.chunk, row.chunk.source
                if chunk.document_id != document_id:
                    raise ValueError("片段不属于待替换资料")
                vector = validate_vector(row.vector, self._profile.dimensions)
                norm = math.hypot(*vector)
                embedding = pack(f"<{len(vector)}f", *(value / norm for value in vector))
                connection.execute(
                    "INSERT INTO inkrag_core_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._collection,
                        chunk.id,
                        document_id,
                        chunk.text,
                        source.name,
                        source.element_ref,
                        source.page,
                        source.result_id,
                        source.sha256,
                        embedding,
                        len(row.terms),
                    ),
                )
                connection.executemany(
                    "INSERT INTO inkrag_core_postings VALUES (?, ?, ?, ?)",
                    (
                        (self._collection, chunk.id, term, count)
                        for term, count in Counter(row.terms).items()
                    ),
                )

    @contextmanager
    def read(self) -> Generator[SQLiteReader]:
        """以首次读入固定 WAL 快照，供本次双路检索和证据读取。"""
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            self._check_profile(connection)
            yield SQLiteReader(connection, self._collection, self._profile.dimensions)


class SQLiteReader:
    """在调用方持有的只读事务中检索当前集合。"""

    def __init__(self, connection: sqlite3.Connection, collection: str, dimensions: int) -> None:
        """借用固定快照；离开快照范围后不再可用。"""
        self._connection, self._collection, self._dimensions = connection, collection, dimensions

    def keyword(
        self, terms: Sequence[str], limit: int, document_ids: frozenset[str] | None = None
    ) -> list[Hit]:
        """以限定资料范围内的统计量计算 BM25，词项按完整值比对。"""
        if not terms or limit <= 0 or document_ids == frozenset():
            return []
        rows: list[tuple[*_ChunkRow, float]] = self._connection.execute(
            _KEYWORD,
            {
                "collection": self._collection,
                "scope": _scope(document_ids),
                "terms": json.dumps(sorted(set(terms)), ensure_ascii=False),
                "limit": limit,
            },
        ).fetchall()
        return [Hit(_chunk(row[:8]), row[8]) for row in rows]

    def vector(
        self, query: Sequence[float], limit: int, document_ids: frozenset[str] | None = None
    ) -> list[Hit]:
        """对范围内向量做余弦精确排序，流式选取前 K 个结果。"""
        if limit <= 0 or document_ids == frozenset():
            return []
        vector = validate_vector(query, self._dimensions)
        norm = math.hypot(*vector)
        normalized = tuple(value / norm for value in vector)
        rows: Iterator[tuple[*_ChunkRow, bytes]] = iter(
            self._connection.execute(
                f"SELECT {_CHUNK}, embedding FROM inkrag_core_chunks WHERE {_SCOPE}",
                {"collection": self._collection, "scope": _scope(document_ids)},
            )
        )
        hits = (
            Hit(
                _chunk(row[:8]),
                max(
                    -1.0,
                    min(
                        1.0,
                        math.fsum(
                            left * right
                            for left, right in zip(
                                normalized, unpack(f"<{self._dimensions}f", row[8]), strict=True
                            )
                        ),
                    ),
                ),
            )
            for row in rows
        )
        return nsmallest(limit, hits, key=lambda hit: (-hit.score, hit.chunk.id))

    def get(
        self, chunk_ids: Sequence[str], document_ids: frozenset[str] | None = None
    ) -> list[Chunk]:
        """按请求的片段顺序取证，同时再次限制资料范围。"""
        if not chunk_ids or document_ids == frozenset():
            return []
        rows: list[_ChunkRow] = self._connection.execute(
            f"SELECT {_CHUNK} FROM inkrag_core_chunks WHERE {_SCOPE} "
            "AND id IN (SELECT value FROM json_each(:ids))",
            {
                "collection": self._collection,
                "scope": _scope(document_ids),
                "ids": json.dumps(list(chunk_ids)),
            },
        ).fetchall()
        found = {row[0]: _chunk(row) for row in rows}
        return [found[identity] for identity in chunk_ids if identity in found]
