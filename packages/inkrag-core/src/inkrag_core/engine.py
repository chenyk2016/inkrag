"""编排解析、切分、两路索引、图和多跳；所有读取共享一个索引快照。"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .contracts import (
    Chunk,
    Embedding,
    Hit,
    Index,
    IndexedChunk,
    IndexProfile,
    IndexReader,
    LexicalAnalyzer,
    validate_vector,
)
from .document import CharacterChunker, Chunker, ParsedDocument, Parser, chunks_for
from .graph import GraphLimits, GraphPath, GraphResult
from .multihop import MultiHopLimits, MultiHopStrategy, run_multihop

type Route = Literal["keyword", "vector", "graph"]
_DEFAULT_CHUNKER = CharacterChunker()
_DEFAULT_GRAPH_LIMITS = GraphLimits()
_DEFAULT_HOP_LIMITS = MultiHopLimits()


class GraphRetriever(Protocol):
    """按图关系找回证据。证据读取必须遵循本次索引快照及资料范围。"""

    @property
    def fingerprint(self) -> str:
        """返回本次关系图的版本标识。"""
        ...

    def seeds_for(self, chunk_ids: Sequence[str], limit: int) -> tuple[str, ...]:
        """从已取得的证据找出图节点。"""
        ...

    def traverse(
        self,
        reader: IndexReader,
        seeds: Sequence[str],
        *,
        document_ids: frozenset[str] | None = None,
        limits: GraphLimits = _DEFAULT_GRAPH_LIMITS,
    ) -> GraphResult:
        """有界沿图查找仍然可读的路径和证据。"""
        ...


@dataclass(frozen=True, slots=True)
class RetrievalStep:
    """一个实际执行的查询和各路按顺序返回的片段 ID。"""

    query: str
    rankings: dict[Route, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class SearchResult:
    """原文证据、实际查询轨迹及图路径。truncated 表示预算截断。"""

    hits: tuple[Hit, ...]
    steps: tuple[RetrievalStep, ...]
    paths: tuple[GraphPath, ...]
    truncated: bool
    graph_id: str | None


def reciprocal_rank_fusion(rankings: Sequence[Sequence[Hit]], limit: int) -> list[Hit]:
    """按名次 1/(60+rank) 加分。不同渠道的原始分数不直接相加。"""
    if len(rankings) == 1:
        return list(rankings[0][:limit])
    chunks: dict[str, Chunk] = {}
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for hit in ranking:
            identity = hit.chunk.id
            if identity not in seen:
                seen.add(identity)
                chunks[identity] = hit.chunk
                scores[identity] = scores.get(identity, 0.0) + 1 / (60 + len(seen))
    ordered = sorted(scores, key=lambda identity: (-scores[identity], identity))
    return [Hit(chunks[identity], scores[identity]) for identity in ordered[:limit]]


def _query(text: str) -> str:
    """在调用模型、图和数据库之前拒绝异常或超大输入。"""
    if not text.strip() or len(text) > 4096 or "\0" in text:
        raise ValueError("问题必须是长度不超过 4096 的非空字符串")
    return text.strip()


def _checked(hits: Sequence[Hit], scope: frozenset[str] | None, limit: int) -> tuple[Hit, ...]:
    """检测替换适配器的越界结果，通道失败直接中止。"""
    if len(hits) > limit or any(
        not math.isfinite(hit.score) or (scope is not None and hit.chunk.document_id not in scope)
        for hit in hits
    ):
        raise ValueError("检索适配器返回了不合法或越出资料范围的结果")
    return tuple(hits)


def _graph_hits(
    reader: IndexReader,
    found: GraphResult,
    scope: frozenset[str] | None,
    limits: GraphLimits,
    capacity: int,
) -> tuple[Hit, ...]:
    """按固定快照复核图路径位置和命中，拒绝过期或越界的图适配器结果。"""
    hits = _checked(found.hits[:capacity], scope, capacity)
    if len(found.paths) > limits.max_nodes:
        raise ValueError("图适配器返回的路径超过节点预算")
    evidence = [hit.chunk for hit in hits]
    for path in found.paths:
        if not 1 <= len(path.edges) == len(path.evidence) <= limits.max_depth:
            raise ValueError("图路径缺少对应证据或超过深度预算")
        previous = path.seed
        for edge, chunk in zip(path.edges, path.evidence, strict=True):
            if edge.source != previous or edge.chunk_id != chunk.id:
                raise ValueError("图路径的关系边、节点和证据不一致")
            previous = edge.target
        evidence.extend(path.evidence)
    identities = tuple(dict.fromkeys(chunk.id for chunk in evidence))
    stored = {chunk.id: chunk for chunk in reader.get(identities, scope)}
    if any(
        stored.get(chunk.id) != chunk or (scope is not None and chunk.document_id not in scope)
        for chunk in evidence
    ):
        raise ValueError("图路径引入了无法在当前范围核对的证据")
    return hits


class Engine:
    """通过组合接口装配一次解析、入库与检索，可直接嵌入现有应用。"""

    def __init__(
        self,
        index: Index,
        embedding: Embedding,
        analyzer: LexicalAnalyzer,
        *,
        chunker: Chunker = _DEFAULT_CHUNKER,
        batch_size: int = 32,
    ) -> None:
        """确认入库与查询使用同一模型维度及分词身份。"""
        if not 1 <= batch_size <= 512:
            raise ValueError("向量批量大小必须在 1 至 512 之间")
        self.index = index
        self.embedding = embedding
        self.analyzer = analyzer
        self.chunker = chunker
        self.batch_size = batch_size
        self._check_profile()

    def _check_profile(self) -> None:
        """模型、词典或宿主更换后须新建集合并重建。"""
        if self.index.profile != IndexProfile(
            self.embedding.fingerprint, self.embedding.dimensions, self.analyzer.fingerprint
        ):
            raise ValueError("模型或分词身份与集合绑定的配置不符，请新建集合并重建")

    def _vectors(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """批量调用模型，完整验证后才可交给索引。"""
        vectors = self.embedding.embed(texts)
        if len(vectors) != len(texts):
            raise ValueError("嵌入模型返回的数量与输入不一致")
        return [validate_vector(vector, self.embedding.dimensions) for vector in vectors]

    def ingest_file(
        self, document_id: str, path: str | Path, *, parser: Parser
    ) -> tuple[Chunk, ...]:
        """解析文件后沿同一入口原子入库。"""
        return self.ingest(document_id, parser.parse(Path(path)))

    def ingest(self, document_id: str, parsed: ParsedDocument) -> tuple[Chunk, ...]:
        """先准备全部词项和向量，成功后原子替换原资料及两路索引。"""
        self._check_profile()
        chunks = chunks_for(document_id, parsed, self.chunker)
        # 向量在事务开始前全部准备，限制单文档的峰值内存。
        if len(chunks) * self.index.profile.dimensions > 2_000_000:
            raise ValueError("资料向量超过单次入库预算，请按原文章节拆分")
        rows: list[IndexedChunk] = []
        for start in range(0, len(chunks), self.batch_size):
            batch = chunks[start : start + self.batch_size]
            vectors = self._vectors([chunk.text for chunk in batch])
            rows.extend(
                IndexedChunk(chunk, self.analyzer.analyze(chunk.text), vector)
                for chunk, vector in zip(batch, vectors, strict=True)
            )
        self.index.replace(document_id, rows)
        return chunks

    def delete(self, document_id: str) -> None:
        """显式移除资料和两路索引。"""
        self.index.replace(document_id, ())

    def retrieve(
        self,
        query: str,
        *,
        routes: tuple[Route, ...] = ("keyword", "vector"),
        top_k: int = 5,
        document_ids: frozenset[str] | None = None,
        graph: GraphRetriever | None = None,
        graph_seeds: tuple[str, ...] = (),
        graph_limits: GraphLimits = _DEFAULT_GRAPH_LIMITS,
        strategy: MultiHopStrategy | None = None,
        hop_limits: MultiHopLimits = _DEFAULT_HOP_LIMITS,
    ) -> SearchResult:
        """可单路、混合及多步检索。每步共用可信范围，资料在快照内保持一致。"""
        question = _query(query)
        self._check_profile()
        if not 1 <= top_k <= 100 or not routes or len(routes) != len(set(routes)):
            raise ValueError("top_k 必须在 1 至 100 之间，且检索通道不能为空或重复")
        if set(routes) - {"keyword", "vector", "graph"}:
            raise ValueError("未知检索通道")
        if "graph" in routes and (graph is None or (routes == ("graph",) and not graph_seeds)):
            raise ValueError("图通道需要关系图，单独走图时需要起始节点")
        if "graph" not in routes and (graph is not None or graph_seeds):
            raise ValueError("只有启用图通道时才可配置关系图或图起点")
        graph_id = graph.fingerprint if graph is not None else None
        if document_ids == frozenset():
            return SearchResult((), (), (), False, graph_id)
        capacity = min(top_k * 3, 100)
        steps: list[RetrievalStep] = []
        paths: list[GraphPath] = []
        truncated = False

        with self.index.read() as reader:

            def search_step(text: str) -> list[Hit]:
                """在当前快照运行一次查询，记录原始排名和图路径。"""
                nonlocal truncated
                text = _query(text)
                rankings: dict[Route, tuple[Hit, ...]] = {}
                if "keyword" in routes:
                    terms = tuple(dict.fromkeys(self.analyzer.analyze(text)))
                    rankings["keyword"] = (
                        _checked(
                            reader.keyword(terms, capacity, document_ids), document_ids, capacity
                        )
                        if terms
                        else ()
                    )
                if "vector" in routes:
                    vector = self._vectors([text])[0]
                    rankings["vector"] = _checked(
                        reader.vector(vector, capacity, document_ids), document_ids, capacity
                    )
                if graph is not None:
                    fused = reciprocal_rank_fusion(list(rankings.values()), 4)
                    seeds = graph_seeds or graph.seeds_for([hit.chunk.id for hit in fused], 4)
                    if seeds:
                        found = graph.traverse(
                            reader, seeds, document_ids=document_ids, limits=graph_limits
                        )
                        rankings["graph"] = _graph_hits(
                            reader, found, document_ids, graph_limits, capacity
                        )
                        paths.extend(found.paths)
                        truncated = truncated or found.truncated
                    else:
                        rankings["graph"] = ()
                steps.append(
                    RetrievalStep(
                        text,
                        {route: tuple(hit.chunk.id for hit in rankings[route]) for route in routes},
                    )
                )
                return reciprocal_rank_fusion([rankings[route] for route in routes], capacity)

            if strategy is None:
                hits = search_step(question)[:top_k]
            else:
                result = run_multihop(question, search_step, strategy, limits=hop_limits)
                hits = list(result.hits[:top_k])
                truncated = truncated or result.truncated
        if graph is not None and graph.fingerprint != graph_id:
            raise ValueError("检索期间关系图发生变化，请重新查询")
        return SearchResult(tuple(hits), tuple(steps), tuple(paths), truncated, graph_id)
