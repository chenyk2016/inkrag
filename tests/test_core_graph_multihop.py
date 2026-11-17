"""使用可回查的两段手册核对显式图和自定义多跳，并验证预算及隔离边界。"""

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from inkrag_core.analyzers import BigramAnalyzer
from inkrag_core.contracts import Chunk, Hit, IndexedChunk, IndexProfile, Source
from inkrag_core.document import ParsedDocument, Part
from inkrag_core.engine import Engine
from inkrag_core.graph import GraphEdge, GraphLimits, ReferenceGraph
from inkrag_core.multihop import (
    Hop,
    HopSearch,
    MultiHopLimits,
    decomposition,
    iterative,
    run_multihop,
)
from inkrag_core.sqlite import SQLiteIndex


def section(identity: str, document: str, page: int, text: str) -> Chunk:
    """建立带页码、元素引用和解析版本的合成手册片段。"""
    return Chunk(
        identity,
        document,
        text,
        Source(f"{document}.pdf", f"#/texts/{page - 1}", page, "reviewed-v1"),
    )


def indexed(chunk: Chunk) -> IndexedChunk:
    """为只读证据流程提供最小合法向量和词项。"""
    return IndexedChunk(chunk, ("手册",), (1.0, 0.5))


class FixtureEmbedding:
    """为两段手册的本地索引提供固定向量，只检查编排和证据。"""

    fingerprint = "graph-test-embedding"
    dimensions = 2

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """为每个输入返回相同的有效向量。"""
        return tuple((1.0, 0.5) for _ in texts)


def test_engine_adds_later_section_via_graph_or_multihop(tmp_path: Path) -> None:
    """同一 SQLite 索引上，单次编号查询只命中故障页，两种扩展均找回安全步骤。"""
    embedding = FixtureEmbedding()
    analyzer = BigramAnalyzer()
    store = SQLiteIndex(
        tmp_path / "core.sqlite",
        "manuals",
        IndexProfile(embedding.fingerprint, embedding.dimensions, analyzer.fingerprint),
    )
    engine = Engine(store, embedding, analyzer)
    document = ParsedDocument(
        "维修手册.pdf",
        (
            Part("E102 温度传感器报警。维修要求见 8.2。", "#/texts/0", 4),
            Part("安全步骤：先断电，再检查传感器接线。", "#/texts/1", 82),
        ),
        result_id="reviewed-v1",
    )
    fault, safety = engine.ingest("manual", document)
    single = engine.retrieve("E102", routes=("keyword",), top_k=2)
    assert [hit.chunk for hit in single.hits] == [fault]

    graph = ReferenceGraph(
        [
            GraphEdge("E102", "引用", "8.2", fault.id),
            GraphEdge("8.2", "记录", "安全步骤", safety.id),
        ]
    )
    expanded = engine.retrieve(
        "E102",
        routes=("keyword", "graph"),
        graph=graph,
        top_k=2,
        document_ids=frozenset({"manual"}),
    )
    assert expanded.graph_id == graph.fingerprint
    assert expanded.paths[-1].evidence == (fault, safety)
    assert [hit.chunk for hit in expanded.hits] == [fault, safety]

    def follow_reference(_question: str, seen: tuple[Hop, ...]) -> str | None:
        """只在首跳原文存在已知章节引用时追加规则查询。"""
        if len(seen) == 1 and any("见 8.2" in hit.chunk.text for hit in seen[0].hits):
            return "安全步骤"
        return None

    multi = engine.retrieve(
        "E102",
        routes=("keyword",),
        strategy=iterative(follow_reference),
        top_k=2,
        document_ids=frozenset({"manual"}),
    )
    assert [step.query for step in multi.steps] == ["E102", "安全步骤"]
    assert [hit.chunk for hit in multi.hits] == [fault, safety]

    engine.ingest("manual", replace(document, result_id="reviewed-v2"))
    stale = engine.retrieve("E102", routes=("graph",), graph=graph, graph_seeds=("E102",))
    assert not stale.hits and not stale.paths


def test_graph_finds_two_handbook_sections_and_preserves_each_edge_source(tmp_path: Path) -> None:
    """模拟跨页的明确引用，原文一段只说明故障，另一段才给维修前置条件。"""
    fault = section("fault-v1", "设备手册", 4, "E102 是温度传感器异常，安全要求见 8.2。")
    safety = section("safety-v1", "设备手册", 82, "8.2 安全要求：先断电，再检查传感器接线。")
    store = SQLiteIndex(tmp_path / "graph.sqlite", "manuals", IndexProfile("test", 2, "test"))
    store.replace("设备手册", [indexed(fault), indexed(safety)])
    edges = (
        GraphEdge("E102", "引用", "8.2 安全要求", fault.id),
        GraphEdge("8.2 安全要求", "要求", "检查传感器接线", safety.id),
        GraphEdge("检查传感器接线", "返回", "E102", safety.id),
    )
    graph = ReferenceGraph(edges)
    assert graph.fingerprint == ReferenceGraph([*reversed(edges), edges[0]]).fingerprint
    assert graph.seeds_for(["not-indexed", safety.id, fault.id, safety.id], 3) == (
        "8.2 安全要求",
        "检查传感器接线",
        "E102",
    )
    assert graph.seeds_for([fault.id, safety.id], 1) == ("E102",)

    with store.read() as reader:
        result = graph.traverse(reader, ["E102"], document_ids=frozenset({"设备手册"}))
        short = graph.traverse(reader, ["E102"], limits=GraphLimits(max_depth=1))

    assert [hit.chunk for hit in result.hits] == [fault, safety]
    assert result.paths[-1].edges == edges[:2]
    assert [chunk.source.page for chunk in result.paths[-1].evidence] == [4, 82]
    assert result.paths[-1].evidence[-1].source.result_id == "reviewed-v1"
    assert not result.truncated
    assert [hit.chunk for hit in short.hits] == [fault]
    assert short.truncated

    replacement = section("safety-v2", "设备手册", 82, "8.2 停机后，联系检修人员。")
    store.replace("设备手册", [indexed(fault), indexed(replacement)])
    with store.read() as reader:
        changed = graph.traverse(reader, ["E102"])
    assert [hit.chunk for hit in changed.hits] == [fault]
    assert not changed.truncated
    changed_edges = (
        edges[0],
        GraphEdge(edges[1].source, edges[1].relation, edges[1].target, replacement.id),
        edges[2],
    )
    assert graph.fingerprint != ReferenceGraph(changed_edges).fingerprint


def test_graph_filters_before_traversing_or_spending_visible_fanout(tmp_path: Path) -> None:
    """禁用证据不能成为桥，第二跳原文本身可见也不改变结果。"""
    hidden = section("hidden", "内部通报", 3, "内部路径从工单连到受限内部编号。")
    allowed = section("allowed", "售后手册", 2, "公开步骤和公开例外。")
    store = SQLiteIndex(tmp_path / "graph.sqlite", "manuals", IndexProfile("test", 2, "test"))
    store.replace(hidden.document_id, [indexed(hidden)])
    store.replace(allowed.document_id, [indexed(allowed)])
    graph = ReferenceGraph(
        (
            GraphEdge("工单", "A 内部路径", "隐式桥", hidden.id),
            GraphEdge("隐式桥", "公开证据", "不应可达", allowed.id),
            GraphEdge("工单", "B 公开路径", "公开步骤", allowed.id),
            GraphEdge("工单", "C 公开路径", "公开例外", allowed.id),
        )
    )
    scope = frozenset({allowed.document_id})
    with store.read() as reader:
        result = graph.traverse(reader, ["工单"], document_ids=scope)
        limited = graph.traverse(
            reader, ["工单"], document_ids=scope, limits=GraphLimits(max_fanout=1)
        )
        nodes = graph.traverse(
            reader, ["工单"], document_ids=scope, limits=GraphLimits(max_nodes=2)
        )
        checked = graph.traverse(
            reader, ["工单"], document_ids=scope, limits=GraphLimits(max_edge_checks=1)
        )
        empty = graph.traverse(reader, ["工单"], document_ids=frozenset())
    assert [path.edges[-1].target for path in result.paths] == ["公开步骤", "公开例外"]
    assert [hit.chunk for hit in result.hits] == [allowed]
    assert not result.truncated
    assert [path.edges[-1].target for path in limited.paths] == ["公开步骤"]
    assert limited.truncated and nodes.truncated and checked.truncated
    assert not checked.hits and not empty.hits and not empty.paths and not empty.truncated


class InvalidReader:
    """模拟越权、串索引和存储不可用的后端响应。"""

    def __init__(self, chunks: list[Chunk] | RuntimeError) -> None:
        """保留用于断言的固定返回行或异常。"""
        self.chunks = chunks

    def get(
        self, chunk_ids: Sequence[str], document_ids: frozenset[str] | None = None
    ) -> list[Chunk]:
        """按测试模式返回不合约的证据。"""
        if isinstance(self.chunks, RuntimeError):
            raise self.chunks
        return self.chunks

    def keyword(
        self, terms: Sequence[str], limit: int, document_ids: frozenset[str] | None = None
    ) -> list[Hit]:
        """图路不依赖词项查询。"""
        raise AssertionError("graph must not call keyword")

    def vector(
        self, query: Sequence[float], limit: int, document_ids: frozenset[str] | None = None
    ) -> list[Hit]:
        """图路不依赖模型或向量查询。"""
        raise AssertionError("graph must not call vector")


def test_graph_rejects_wrong_evidence_and_propagates_storage_failure() -> None:
    """伪造证据、读取错误、超预算与错误种子都有显式结果。"""
    graph = ReferenceGraph([GraphEdge("入口", "引用", "第二节", "required")])
    with pytest.raises(ValueError, match="范围外"):
        graph.traverse(
            InvalidReader([section("required", "private", 1, "privileged text")]),
            ["入口"],
            document_ids=frozenset({"public"}),
        )
    with pytest.raises(ValueError, match="范围外"):
        graph.traverse(InvalidReader([section("other", "public", 1, "wrong ID")]), ["入口"])
    with pytest.raises(RuntimeError, match="storage unreachable"):
        graph.traverse(InvalidReader(RuntimeError("storage unreachable")), ["入口"])
    with pytest.raises(ValueError, match="预算"):
        GraphLimits(max_depth=100)
    with pytest.raises(ValueError, match="引用名"):
        graph.traverse(InvalidReader([]), "入口")


def test_iterative_and_decomposition_recover_distinct_evidence_with_a_scoped_reader() -> None:
    """从首跳正文得到下一跳线索，合并后包含两个可分别回查的章节。"""
    fault = Hit(section("fault", "设备手册", 4, "E102 引用 8.2 安全要求。"), 0.9)
    safety = Hit(section("safety", "设备手册", 82, "先断电，再检查传感器接线。"), 0.8)
    distraction = Hit(section("distraction", "设备手册", 16, "保修政策。"), 0.2)
    question = "E102 如何检修？"
    followup = "8.2 安全要求"
    by_query = {question: [fault, distraction], followup: [safety, fault]}

    def next_query(original: str, hops: tuple[Hop, ...]) -> str | None:
        """仅用首跳已经命中的原文选出第二问。"""
        assert original == question
        if len(hops) == 1:
            assert followup in hops[0].hits[0].chunk.text
            return followup
        return None

    def search(query: str) -> list[Hit]:
        """模拟已经限制在合成设备手册的查询入口。"""
        return by_query[query]

    def custom(original: str, retrieve: HopSearch) -> None:
        """调用者可用通用回调实现自己的查询顺序。"""
        retrieve(original)
        retrieve(followup)

    assert [hit.chunk.source.page for hit in search(question)] == [4, 16]
    for strategy in (decomposition(lambda _: [question, followup]), iterative(next_query), custom):
        result = run_multihop(question, search, strategy)
        assert [hop.query for hop in result.hops] == [question, followup]
        assert [hit.chunk.source.page for hit in result.hits] == [4, 82, 16]
        assert not result.truncated


def test_multihop_enforces_budgets_and_propagates_strategy_errors() -> None:
    """循环、空计划和无权继续使用的回调不会意外发起查询。"""
    hits = [Hit(section(str(n), "设备手册", n, "测试内容"), 1.0) for n in range(1, 6)]
    queries: list[str] = []

    def retrieve(query: str) -> list[Hit]:
        """记录实际执行了哪些调用。"""
        queries.append(query)
        return hits

    def endless_plan(_question: str) -> Iterator[str]:
        """用无限拆题输入验证上限掌握在执行器。"""
        number = 1
        while True:
            yield f"子查询 {number}"
            number += 1

    bounded = run_multihop(
        "故障",
        retrieve,
        decomposition(endless_plan),
        limits=MultiHopLimits(max_queries=2, max_evidence=2),
    )
    assert queries == ["子查询 1", "子查询 2"]
    assert bounded.truncated and len(bounded.hits) == 2
    assert all(len(hop.hits) == 2 for hop in bounded.hops)

    queries.clear()
    repeated = run_multihop("故障", retrieve, iterative(lambda question, _hops: " " + question))
    assert queries == ["故障"] and len(repeated.hops) == 1
    assert not repeated.truncated
    queries.clear()
    empty = run_multihop("故障", retrieve, decomposition(lambda _: []))
    assert not queries and not empty.hits and not empty.hops

    with pytest.raises(ValueError, match="长度预算"):
        run_multihop(
            "故障",
            retrieve,
            decomposition(lambda _: ["很长" * 50]),
            limits=MultiHopLimits(max_query_chars=8),
        )
    assert not queries

    def bad_planner(_query: str) -> list[str]:
        """策略错误应该交给调用者处理。"""
        raise RuntimeError("planner unavailable")

    with pytest.raises(RuntimeError, match="planner unavailable"):
        run_multihop("故障", retrieve, decomposition(bad_planner))
    assert not queries

    callbacks: list[HopSearch] = []

    def collect(_query: str, search: HopSearch) -> None:
        """捕获调用边界供反例验证。"""
        callbacks.append(search)

    run_multihop("故障", retrieve, collect)
    with pytest.raises(RuntimeError, match="已经结束"):
        callbacks[0]("超出会话")
    assert not queries
