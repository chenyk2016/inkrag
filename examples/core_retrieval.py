"""无密钥演示原文、中文 BM25、向量、图与多跳怎样走同一个 SQLite 索引。"""

from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from inkrag_core import BigramAnalyzer, Engine, IndexProfile, ParsedDocument, Part
from inkrag_core.graph import GraphEdge, ReferenceGraph
from inkrag_core.multihop import decomposition
from inkrag_core.sqlite import SQLiteIndex


class DemoEmbedding:
    """固定规则向量仅用于验证接口流程，示例无需下载或登录模型。"""

    fingerprint = "core-demo-fixed-v1"
    dimensions = 2

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """按输入顺序返回两维测试向量。"""
        return tuple((1.0, 0.0) if "E102" in text else (0.0, 1.0) for text in texts)


def main() -> None:
    """从编号定位到跨章节安全步骤，打印路径与页码。"""
    analyzer, embedding = BigramAnalyzer(), DemoEmbedding()
    profile = IndexProfile(embedding.fingerprint, embedding.dimensions, analyzer.fingerprint)
    with TemporaryDirectory(prefix="inkrag-core-demo-") as directory:
        db = SQLiteIndex(Path(directory) / "manual.sqlite3", "manual", profile)
        engine = Engine(db, embedding, analyzer)
        manual = ParsedDocument(
            "设备维修手册.pdf",
            (
                Part("E102 温度传感器故障，检查前请参阅第八章。", "#/chapter/2", 4),
                Part("安全步骤：先断开电源，再检查接线。", "#/chapter/8", 82),
            ),
        )
        overview, procedure = engine.ingest(
            "maintenance", ParsedDocument.from_json(manual.to_json())
        )
        graph = ReferenceGraph(
            (
                GraphEdge("E102", "参阅", "第八章", overview.id),
                GraphEdge("第八章", "说明", "安全步骤", procedure.id),
            )
        )
        keyword = engine.retrieve("E102", routes=("keyword",))
        hybrid = engine.retrieve("E102")
        related = engine.retrieve(
            "E102", routes=("keyword", "vector", "graph"), graph=graph, graph_seeds=("E102",)
        )
        multi_hop = engine.retrieve(
            "E102 安全处理",
            routes=("keyword",),
            strategy=decomposition(lambda _: ("E102", "安全步骤")),
        )
        assert [hit.chunk.source.page for hit in keyword.hits] == [4]
        assert {hit.chunk.source.page for hit in related.hits} == {4, 82}
        assert {hit.chunk.source.page for hit in multi_hop.hits} == {4, 82}
        assert any(len(path.edges) == 2 for path in related.paths)
        for label, result in (
            ("关键词", keyword),
            ("混合", hybrid),
            ("混合加图", related),
            ("多跳", multi_hop),
        ):
            print(
                label, [f"第 {hit.chunk.source.page} 页：{hit.chunk.text}" for hit in result.hits]
            )
        print("实际查询：", [step.query for step in multi_hop.steps])


if __name__ == "__main__":
    main()
