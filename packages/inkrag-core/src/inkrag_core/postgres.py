"""用 PostgreSQL 倒排词项和 pgvector 精确余弦提供持久检索。"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

from .contracts import Chunk, Hit, IndexedChunk, IndexProfile, Source, validate_vector

if TYPE_CHECKING:
    from psycopg import Connection

_SCHEMA = (
    "CREATE SCHEMA IF NOT EXISTS inkrag_core",
    """CREATE TABLE IF NOT EXISTS inkrag_core.collections (
        name TEXT COLLATE "C" PRIMARY KEY, embedding_id TEXT NOT NULL,
        dimensions INTEGER NOT NULL, analyzer_id TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS inkrag_core.chunks (
        collection TEXT COLLATE "C" NOT NULL REFERENCES inkrag_core.collections(name),
        id TEXT COLLATE "C" NOT NULL, document_id TEXT COLLATE "C" NOT NULL,
        text TEXT NOT NULL, name TEXT NOT NULL, element_ref TEXT NOT NULL,
        page INTEGER, result_id TEXT, sha256 TEXT,
        embedding public.vector NOT NULL, length INTEGER NOT NULL, PRIMARY KEY (collection, id)
    )""",
    "CREATE INDEX IF NOT EXISTS chunks_document ON inkrag_core.chunks (collection, document_id)",
    """CREATE TABLE IF NOT EXISTS inkrag_core.postings (
        collection TEXT COLLATE "C" NOT NULL, chunk_id TEXT COLLATE "C" NOT NULL,
        term TEXT COLLATE "C" NOT NULL, frequency INTEGER NOT NULL,
        PRIMARY KEY (collection, term, chunk_id),
        FOREIGN KEY (collection, chunk_id) REFERENCES inkrag_core.chunks(collection, id)
            ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS postings_chunk ON inkrag_core.postings (collection, chunk_id)",
)

_SCOPE = """collection=%(collection)s AND (
    %(scope)s::text[] IS NULL OR document_id = ANY(%(scope)s::text[])
)"""
_KEYWORD = f"""
WITH scoped AS (SELECT id, length FROM inkrag_core.chunks WHERE {_SCOPE}),
corpus AS (SELECT count(*) AS n, avg(length)::double precision AS mean_length FROM scoped),
matches AS (
    SELECT p.chunk_id, p.term, p.frequency, s.length
    FROM inkrag_core.postings p JOIN scoped s ON s.id=p.chunk_id
    WHERE p.collection=%(collection)s AND p.term=ANY(%(terms)s::text[])
), counts AS (SELECT term, count(*) AS df FROM matches GROUP BY term),
ranked AS (
    SELECT chunk_id, sum(
        ln(1.0 + (n-df+0.5)/(df+0.5))
        * (frequency*2.2)/(frequency+1.2*(0.25+0.75*length/mean_length))
    ) AS score
    FROM matches JOIN counts USING (term) CROSS JOIN corpus GROUP BY chunk_id
)
SELECT c.id, c.document_id, c.text, c.name, c.element_ref, c.page, c.result_id, c.sha256, r.score
FROM ranked r JOIN inkrag_core.chunks c ON c.collection=%(collection)s AND c.id=r.chunk_id
ORDER BY r.score DESC, c.id LIMIT %(limit)s
"""
_CHUNK = "id, document_id, text, name, element_ref, page, result_id, sha256"


def _chunk(row: Sequence[object]) -> Chunk:
    """从受控数据库字段重建片段，结构损坏时明确失败。"""
    identity, document, text, name, element, page, result, sha = row
    if (
        not isinstance(identity, str)
        or not isinstance(document, str)
        or not isinstance(text, str)
        or not isinstance(name, str)
        or not isinstance(element, str)
        or (page is not None and not isinstance(page, int))
        or (result is not None and not isinstance(result, str))
        or (sha is not None and not isinstance(sha, str))
    ):
        raise ValueError("PostgreSQL 片段或来源格式无效")
    return Chunk(identity, document, text, Source(name, element, page, result, sha))


def _hit(row: Sequence[object]) -> Hit:
    """将数据库的双精度检索分数与原始片段一起返回。"""
    score = row[8]
    if not isinstance(score, (float, int)) or not math.isfinite(score):
        raise ValueError("PostgreSQL 检索分数无效")
    return Hit(_chunk(row[:8]), float(score))


def _embedding(values: Sequence[float], dimensions: int) -> str:
    """校验维度、有限值与非零范数，再生成可绑定的 pgvector 字面值。"""
    vector = validate_vector(values, dimensions)
    norm = math.hypot(*vector)
    return "[" + ",".join(str(value / norm) for value in vector) + "]"


class PostgresIndex:
    """读取或显式初始化独立集合；部署方预先在 public 安装 vector 扩展。"""

    def __init__(
        self, dsn: str, collection: str, profile: IndexProfile, *, initialize: bool = False
    ) -> None:
        """默认只核对已有配置；初始化模式需要专用 DDL 和集合写权限。"""
        self._dsn, self._collection, self._profile = dsn, collection, profile
        with self._connect() as connection:
            if initialize:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO inkrag_core.collections VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (collection, profile.embedding_id, profile.dimensions, profile.analyzer_id),
                )
            self._check_profile(connection)

    @property
    def profile(self) -> IndexProfile:
        """返回集合持久化的分词与向量配置。"""
        return self._profile

    def _connect(self) -> Connection[tuple[object, ...]]:
        """按操作打开连接，SQLite 用户无需安装 PostgreSQL 驱动。"""
        import psycopg

        return psycopg.connect(self._dsn, connect_timeout=5)

    def _check_profile(self, connection: Connection[tuple[object, ...]]) -> None:
        """发现遗漏或配置不匹配时拒绝读取和覆盖集合。"""
        row = connection.execute(
            "SELECT embedding_id, dimensions, analyzer_id FROM inkrag_core.collections WHERE name=%s",
            (self._collection,),
        ).fetchone()
        profile = self._profile
        if row != (profile.embedding_id, profile.dimensions, profile.analyzer_id):
            raise ValueError("PostgreSQL 集合缺失或索引配置不匹配")

    def replace(self, document_id: str, rows: Sequence[IndexedChunk]) -> None:
        """串行替换同一资料，正文、向量、词频和删除在同一事务提交。"""
        with self._connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (self._collection, document_id),
            )
            self._check_profile(connection)
            connection.execute(
                "DELETE FROM inkrag_core.chunks WHERE collection=%s AND document_id=%s",
                (self._collection, document_id),
            )
            with connection.cursor() as cursor:
                for row in rows:
                    chunk, source = row.chunk, row.chunk.source
                    if chunk.document_id != document_id:
                        raise ValueError("片段不属于待替换资料")
                    cursor.execute(
                        "INSERT INTO inkrag_core.chunks VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::public.vector, %s)",
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
                            _embedding(row.vector, self._profile.dimensions),
                            len(row.terms),
                        ),
                    )
                    cursor.executemany(
                        "INSERT INTO inkrag_core.postings VALUES (%s, %s, %s, %s)",
                        (
                            (self._collection, chunk.id, term, count)
                            for term, count in Counter(row.terms).items()
                        ),
                    )

    @contextmanager
    def read(self) -> Generator[PostgresReader]:
        """用可重复读快照固定本次双路检索、来源和删除视图。"""
        with self._connect() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SET LOCAL statement_timeout TO '30s'")
            connection.execute("SET LOCAL idle_in_transaction_session_timeout TO '120s'")
            self._check_profile(connection)
            yield PostgresReader(connection, self._collection, self._profile.dimensions)


class PostgresReader:
    """在只读 PostgreSQL 快照中计算检索结果。"""

    def __init__(
        self, connection: Connection[tuple[object, ...]], collection: str, dimensions: int
    ) -> None:
        """借用事务连接和已经校验的集合配置。"""
        self._connection, self._collection, self._dimensions = connection, collection, dimensions

    def _params(self, document_ids: frozenset[str] | None) -> dict[str, str | list[str] | None]:
        """所有查询从同一可信集合和资料范围建立绑定参数。"""
        return {
            "collection": self._collection,
            "scope": None if document_ids is None else sorted(document_ids),
        }

    def keyword(
        self, terms: Sequence[str], limit: int, document_ids: frozenset[str] | None = None
    ) -> list[Hit]:
        """使用范围内文档统计和预切词项计算 BM25，不使数据库重新分词。"""
        if not terms or limit <= 0 or document_ids == frozenset():
            return []
        rows = self._connection.execute(
            _KEYWORD,
            {**self._params(document_ids), "terms": sorted(set(terms)), "limit": limit},
        )
        return [_hit(row) for row in rows]

    def vector(
        self, query: Sequence[float], limit: int, document_ids: frozenset[str] | None = None
    ) -> list[Hit]:
        """先限制资料，再在 pgvector 中执行精确余弦排序。"""
        if limit <= 0 or document_ids == frozenset():
            return []
        rows = self._connection.execute(
            f"SELECT {_CHUNK}, 1 - (embedding OPERATOR(public.<=>) %(vector)s::public.vector) AS score "
            f"FROM inkrag_core.chunks WHERE {_SCOPE} ORDER BY score DESC, id LIMIT %(limit)s",
            {
                **self._params(document_ids),
                "vector": _embedding(query, self._dimensions),
                "limit": limit,
            },
        )
        return [_hit(row) for row in rows]

    def get(
        self, chunk_ids: Sequence[str], document_ids: frozenset[str] | None = None
    ) -> list[Chunk]:
        """再次核对资料范围，按调用者提供的片段顺序返回正文和来源。"""
        if not chunk_ids or document_ids == frozenset():
            return []
        rows = self._connection.execute(
            f"SELECT {_CHUNK} FROM inkrag_core.chunks WHERE {_SCOPE} AND id=ANY(%(ids)s::text[])",
            {**self._params(document_ids), "ids": list(chunk_ids)},
        )
        found = {chunk.id: chunk for chunk in map(_chunk, rows)}
        return [found[identity] for identity in chunk_ids if identity in found]
