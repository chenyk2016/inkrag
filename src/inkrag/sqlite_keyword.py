"""SQLite FTS5 适配器，管理本地快照词表、匹配语法和 BM25 排名。"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import override

from llama_index.core import VectorStoreIndex
from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import MetadataMode, NodeWithScore, QueryBundle
from pydantic import TypeAdapter

from .lexical import (
    IDENTIFIER_BOOST,
    LEGACY_LEXICAL,
    LexicalAnalyzer,
    LexicalInput,
    LexicalProfile,
    LexicalTerms,
)
from .lexical_adapters import LegacyBigramAnalyzer, lexical_analyzer
from .models import Error

KEYWORD_INDEX = "keyword.sqlite3"
_PROFILE_FILE = "keyword-profile"
_PROFILE = TypeAdapter(LexicalProfile)
_RANKED_ROW = TypeAdapter(tuple[str, float])
_INDEX_VERSION = TypeAdapter(tuple[int])


def keyword_profile(path: Path) -> LexicalProfile:
    """读取快照固定的分词版本；缺少标记的历史快照沿用原规则。"""
    try:
        marker = path.with_name(_PROFILE_FILE).read_bytes()
    except FileNotFoundError:
        return LEGACY_LEXICAL
    if not marker.endswith(b"\n"):
        raise Error("invalid_storage", "关键词索引版本不受支持")
    try:
        profile = _PROFILE.validate_python(marker[:-1].decode(), strict=True)
    except ValueError:
        raise Error("invalid_storage", "关键词索引版本不受支持") from None
    if profile == LEGACY_LEXICAL:
        raise Error("invalid_storage", "关键词索引版本不受支持")
    if not path.is_file():
        raise Error("invalid_storage", "已提交快照缺少关键词索引")
    return profile


def sqlite_analyzer(profile: LexicalProfile) -> LexicalAnalyzer:
    """为本地历史版本指定双字适配器，其余显式版本使用共用规则。"""
    return lexical_analyzer(profile, legacy=LegacyBigramAnalyzer())


@dataclass(frozen=True)
class _FtsLayout:
    """固定 SQLite 词表结构和对应的写入、排名语句。"""

    version: int
    create: str
    insert: str
    score: str
    score_params: tuple[float, ...] = ()


_LEGACY_FTS = _FtsLayout(
    version=0,
    create="CREATE VIRTUAL TABLE chunks USING fts5(node_id UNINDEXED, terms)",
    insert="INSERT INTO chunks (node_id, terms) VALUES (?, ?)",
    score="-bm25(chunks)",
)
_TERMS_FTS = _FtsLayout(
    version=1,
    # 词项已经切好。ascii 保留非 ASCII 文字，编号列额外保留连字符和下划线。
    create="""CREATE VIRTUAL TABLE chunks USING fts5(
        node_id UNINDEXED, terms, identifiers, tokenize = "ascii tokenchars '-_'"
    )""",
    insert="INSERT INTO chunks (node_id, terms, identifiers) VALUES (?, ?, ?)",
    # highlight 判断编号列是否命中，正文 BM25 与一次固定编号加分分别计算。
    score="""-bm25(chunks, 0.0, 1.0, 0.0)
        + CASE WHEN highlight(chunks, 2, '[', ']') <> identifiers THEN ? ELSE 0 END""",
    score_params=(IDENTIFIER_BOOST,),
)


def _as_terms(keywords: LexicalInput) -> LexicalTerms:
    """本地已定义的词表格式均要求由 Python 提供预切词项。"""
    if not isinstance(keywords, LexicalTerms):
        raise Error("invalid_storage", "当前 SQLite 关键词索引需要预切词项")
    return keywords


def _index_rows(
    index: VectorStoreIndex, profile: LexicalProfile, layout: _FtsLayout
) -> Iterator[tuple[str, ...]]:
    """从当前向量快照原文生成 SQLite 词表行，不读取其他快照。"""
    analyzer = sqlite_analyzer(profile)
    for node_id in index.index_struct.nodes_dict.values():
        text = index.docstore.get_node(node_id).get_content(MetadataMode.NONE)
        fields = _as_terms(analyzer.analyze(text))
        row = (node_id, " ".join(fields.terms))
        yield row if layout.version == 0 else (*row, " ".join(fields.identifiers))


def _build(
    connection: sqlite3.Connection, index: VectorStoreIndex, profile: LexicalProfile
) -> None:
    """写入完整词表后提交；调用方负责快照发布前的文件同步。"""
    layout = _LEGACY_FTS if profile == LEGACY_LEXICAL else _TERMS_FTS
    try:
        connection.execute(layout.create)
    except sqlite3.OperationalError as exc:
        if str(exc) == "no such module: fts5":
            raise Error("keyword_unavailable", "当前 Python 的 SQLite 缺少 FTS5") from None
        raise
    connection.executemany(layout.insert, _index_rows(index, profile, layout))
    if layout.version != 0:
        connection.execute(f"PRAGMA user_version = {layout.version}")
    connection.commit()


def persist_keyword_index(
    path: Path, index: VectorStoreIndex, *, lexical_profile: LexicalProfile = LEGACY_LEXICAL
) -> None:
    """写入候选快照的词表及版本，由调用方同步落盘后一起提交快照。"""
    with closing(sqlite3.connect(path)) as connection:
        _build(connection, index, lexical_profile)
    if lexical_profile != LEGACY_LEXICAL:
        path.with_name(_PROFILE_FILE).write_text(lexical_profile + "\n", encoding="utf-8")


def _match_terms(terms: tuple[str, ...]) -> str:
    """以 FTS5 字面值语法安全组合词项，防止查询内容被解释为运算符。"""
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _match(query: LexicalTerms, layout: _FtsLayout) -> str:
    """按词表结构编译查询，正文词项和编号可分别命中自己的列。"""
    terms = _match_terms(query.terms)
    if layout.version == 0:
        return terms
    if query.exact_identifier is not None:
        return f"identifiers: ({_match_terms((query.exact_identifier,))})"
    identifiers = _match_terms(query.identifiers)
    clauses: list[str] = []
    if identifiers:
        clauses.append(f"identifiers: ({identifiers})")
    if terms:
        clauses.append(f"terms: ({terms})")
    return " OR ".join(clauses)


class KeywordRetriever(BaseRetriever):
    """让 SQLite 关键词召回同时服务单路查询和 LlamaIndex 原生融合。"""

    def __init__(
        self,
        path: Path,
        index: VectorStoreIndex,
        query: str,
        keywords: LexicalInput,
        profile: LexicalProfile,
        top_k: int,
    ) -> None:
        """绑定快照和已准备的词项，实例不持有数据库连接。"""
        super().__init__()
        self._path, self._index, self._top_k = path, index, top_k
        self._query = query
        self._layout = _LEGACY_FTS if profile == LEGACY_LEXICAL else _TERMS_FTS
        self._expression = _match(_as_terms(keywords), self._layout)

    @override
    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        """历史词表缺失时从原节点生成内存词表，其他版本要求匹配的落盘词表。"""
        if query_bundle.query_str != self._query:
            raise Error("invalid_retrieval_options", "查询与已准备的词项不一致")
        if not self._expression:
            return []
        exists = self._path.is_file()
        if not exists and self._layout.version != 0:
            raise Error("invalid_storage", "已提交快照缺少关键词索引")
        target = self._path.as_uri() + "?mode=ro&immutable=1" if exists else ":memory:"
        with closing(sqlite3.connect(target, uri=True)) as connection:
            if not exists:
                _build(connection, self._index, LEGACY_LEXICAL)
            (version,) = _INDEX_VERSION.validate_python(
                connection.execute("PRAGMA user_version").fetchone(), strict=True
            )
            if version != self._layout.version:
                raise Error("invalid_storage", "关键词索引与快照声明的版本不一致")
            rows = connection.execute(
                f"SELECT node_id, {self._layout.score} AS score FROM chunks WHERE chunks MATCH ? "
                "ORDER BY score DESC, node_id LIMIT ?",
                (*self._layout.score_params, self._expression, self._top_k),
            )
            hits: list[NodeWithScore] = []
            for row in rows:
                node_id, score = _RANKED_ROW.validate_python(row, strict=True)
                hits.append(NodeWithScore(node=self._index.docstore.get_node(node_id), score=score))
            return hits
