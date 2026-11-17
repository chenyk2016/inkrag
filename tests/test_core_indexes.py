"""在实际 SQLite 以及显式专用的 PostgreSQL + pgvector 上验证同一索引契约。"""

import math
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from inkrag_core.contracts import Chunk, Index, IndexedChunk, IndexProfile, Source
from inkrag_core.postgres import PostgresIndex
from inkrag_core.sqlite import SQLiteIndex

PROFILE = IndexProfile(embedding_id="fixture-v1", dimensions=2, analyzer_id="exact-v1")


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
def index_factory(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[Callable[[str, IndexProfile], Index]]:
    """每次测试使用独立集合，真实 PostgreSQL 仅接受标明用途的本机测试库。"""
    if request.param == "sqlite":

        def sqlite(collection: str, profile: IndexProfile) -> Index:
            """从固定数据库文件打开另一个集合或已有集合。"""
            return SQLiteIndex(tmp_path / "fixtures.sqlite3", collection, profile)

        yield sqlite
        return

    dsn = os.environ.get("INKRAG_CORE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("需要显式专用的 PostgreSQL + pgvector 测试连接")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    database = params.get("dbname")
    if (
        params.get("host") not in {"127.0.0.1", "localhost", "::1"}
        or not isinstance(database, str)
        or not database.startswith("inkrag_core_test")
    ):
        pytest.fail("只允许在本机 inkrag_core_test 专用数据库中执行测试")
    identity = uuid4().hex
    prefix = "fixture-" + identity + "-"
    runtime_role = "inkrag_core_" + identity
    role = sql.Identifier(runtime_role)
    runtime_dsn = make_conninfo(dsn, options=f"-c role={runtime_role}")
    collections: set[str] = set()
    with psycopg.connect(dsn) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))

    def postgres(collection: str, profile: IndexProfile) -> Index:
        """创建独立集合，随后用普通打开方式验证已有配置。"""
        name = prefix + collection
        if name not in collections:
            PostgresIndex(dsn, name, profile, initialize=True)
            collections.add(name)
            with psycopg.connect(dsn) as connection:
                connection.execute(sql.SQL("GRANT USAGE ON SCHEMA inkrag_core TO {}").format(role))
                connection.execute(
                    sql.SQL("GRANT SELECT ON inkrag_core.collections TO {}").format(role)
                )
                connection.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, DELETE ON inkrag_core.chunks, inkrag_core.postings TO {}"
                    ).format(role)
                )
        return PostgresIndex(runtime_dsn, name, profile)

    try:
        yield postgres
    finally:
        with psycopg.connect(dsn) as connection:
            if collections:
                names = list(collections)
                connection.execute(
                    "DELETE FROM inkrag_core.chunks WHERE collection=ANY(%s)", (names,)
                )
                connection.execute(
                    "DELETE FROM inkrag_core.collections WHERE name=ANY(%s)", (names,)
                )
            connection.execute(sql.SQL("DROP OWNED BY {}").format(role))
            connection.execute(sql.SQL("DROP ROLE {}").format(role))


def indexed(
    identity: str, document: str, terms: tuple[str, ...], vector: tuple[float, ...] = (1.0, 0.0)
) -> IndexedChunk:
    """创建带完整来源的合成片段，避免使用外部模型或资料。"""
    return IndexedChunk(
        Chunk(
            identity,
            document,
            "原文 " + identity,
            Source("示例.md", "#/texts/0", 2, "a" * 64, "b" * 64),
        ),
        terms,
        vector,
    )


def test_exact_terms_bm25_vector_and_ordered_scoped_get(
    index_factory: Callable[[str, IndexProfile], Index],
) -> None:
    """中文整词、编号、范围过滤和真实 BM25 分数应在两个后端保持一致。"""
    index = index_factory("retrieval", PROFILE)
    public = indexed("public-hit", "visible", ("传感器", "AB-123"), (0.2, 1.0))
    other = indexed("only-composite", "visible-two", ("温度传感器", "AB-1234"), (-1.0, 0.0))
    hidden = indexed("private-hit", "restricted", ("传感器", "传感器", "AB-123"))
    not_the_same_collection = index_factory("other", PROFILE)
    not_the_same_collection.replace("visible", [indexed(public.chunk.id, "visible", ("别的集合",))])
    for row in (public, other, hidden):
        index.replace(row.chunk.document_id, [row])

    with index.read() as reader:
        scoped = frozenset({"visible"})
        assert [hit.chunk for hit in reader.keyword(["温度传感器"], 9)] == [other.chunk]
        assert reader.keyword(["传"], 9) == []
        assert [hit.chunk for hit in reader.keyword(["AB-123"], 9, scoped)] == [public.chunk]
        assert reader.keyword(["AB-1234"], 9, scoped) == []
        scores = reader.keyword(["传感器", "传感器"], 1, scoped)
        assert scores[0].score == pytest.approx(math.log1p(0.5 / 1.5))
        assert reader.vector((1.0, 0), 1)[0].chunk == hidden.chunk
        vector = reader.vector((1.0, 0), 1, scoped)
        assert vector[0].chunk == public.chunk
        assert vector[0].score == pytest.approx(0.2 / math.hypot(0.2, 1.0))
        assert reader.get([other.chunk.id, hidden.chunk.id, public.chunk.id], scoped) == [
            public.chunk
        ]
        assert reader.get([other.chunk.id, public.chunk.id]) == [other.chunk, public.chunk]
        assert reader.keyword(["传感器"], 10, frozenset()) == []
        assert reader.vector((1.0, 0.0), 10, frozenset()) == []
        assert reader.get([public.chunk.id], frozenset()) == []


def test_reopen_profile_and_atomic_replacement_with_read_snapshot(
    index_factory: Callable[[str, IndexProfile], Index],
) -> None:
    """发生坏向量、并发替换以及删除时，读快照维持同一版正文和双路索引。"""
    index = index_factory("snapshot", PROFILE)
    old = indexed("old", "document", ("旧版本",))
    new = indexed("new", "document", ("新版本",), (0.0, 1.0))
    index.replace("document", [old])
    with pytest.raises(ValueError, match="配置不匹配"):
        index_factory("snapshot", IndexProfile("wrong-model", 2, "exact-v1"))
    reopened = index_factory("snapshot", PROFILE)
    assert reopened.profile == PROFILE

    for invalid in (
        indexed("bad", "document", ("新版本",), (1.0,)),
        indexed("bad", "another-document", ("新版本",)),
        indexed("bad", "document", ("新版本",), (float("nan"), 0.0)),
    ):
        with pytest.raises(ValueError):
            index.replace("document", [new, invalid])
        with reopened.read() as view:
            assert view.get([old.chunk.id, new.chunk.id, invalid.chunk.id]) == [old.chunk]

    with reopened.read() as snapshot:
        index.replace("document", [new])
        assert snapshot.keyword(["新版本"], 5) == []
        assert snapshot.vector((0.0, 1.0), 5)[0].chunk == old.chunk
        assert snapshot.get([old.chunk.id, new.chunk.id]) == [old.chunk]
    with reopened.read() as snapshot:
        assert snapshot.keyword(["旧版本"], 5) == []
        assert snapshot.keyword(["新版本"], 5)[0].chunk == new.chunk
        index.replace("document", [])
        assert snapshot.get([new.chunk.id]) == [new.chunk]
    with reopened.read() as snapshot:
        assert snapshot.keyword(["新版本"], 5) == []
        assert snapshot.vector((0.0, 1.0), 5) == []
        assert snapshot.get([old.chunk.id, new.chunk.id]) == []
