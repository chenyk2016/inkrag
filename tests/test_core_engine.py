"""从文件到干净索引检索，并用异常适配器挑战证据与事务边界。"""

from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest
from inkparse import FileSource, Parser, ParseRequest, save_result
from inkrag_core import BigramAnalyzer, Engine, IndexProfile, IndexReader, ParsedDocument, Part
from inkrag_core.embedding import OpenAICompatibleEmbedding
from inkrag_core.graph import GraphEdge, GraphLimits, GraphPath, GraphResult
from inkrag_core.inkparse_adapter import InkparseAdapter
from inkrag_core.sqlite import SQLiteIndex

_GRAPH_LIMITS = GraphLimits()


class FixedEmbedding:
    """两个可区分的方向，记录查询调用以便验证关闭通道是否生效。"""

    fingerprint = "fixed-engine-test"
    dimensions = 2
    failed = False

    def __init__(self) -> None:
        """记录模型真正收到的文本。"""
        self.calls: list[str] = []

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """对特定替换文本模拟坏向量，其他文本映射到两个方向。"""
        self.calls.extend(texts)
        if self.failed and any("失败" in text for text in texts):
            return [(0.0, 0.0) for _ in texts]
        return [(1.0, 0.0) if "E102" in text else (0.0, 1.0) for text in texts]


@pytest.fixture
def engine_and_model(tmp_path: Path) -> tuple[Engine, FixedEmbedding]:
    """每个测试使用全新 SQLite 文件和分词/嵌入身份。"""
    model, analyzer = FixedEmbedding(), BigramAnalyzer()
    index = SQLiteIndex(
        tmp_path / "core.sqlite3",
        "qa",
        IndexProfile(model.fingerprint, model.dimensions, analyzer.fingerprint),
    )
    return Engine(index, model, analyzer, batch_size=1), model


def test_pdf_native_result_to_provenance(
    engine_and_model: tuple[Engine, FixedEmbedding], tmp_path: Path
) -> None:
    """内置 PDF 读取、保全原生 JSON 和通用 JSON，再从索引回查第 1 页。"""
    engine, model = engine_and_model
    pdf = Path(__file__).resolve().parents[1] / "assets/samples/sensor.pdf"
    adapter = InkparseAdapter()
    native = Parser().parse(ParseRequest(source=FileSource(path=str(pdf))))
    artifact = tmp_path / "parsed.json"
    save_result(native, artifact)
    parsed = adapter.parse(pdf)
    assert adapter.load(artifact) == ParsedDocument.from_json(parsed.to_json())
    chunks = engine.ingest_file("manual", pdf, parser=adapter)
    model.calls.clear()
    result = engine.retrieve("\uff25\uff11\uff10\uff12", routes=("keyword",))
    assert result.hits[0].chunk == chunks[0]
    assert chunks[0].source.page == 1 and chunks[0].source.element_ref == "#/texts/0"
    assert chunks[0].source.result_id == native.result_id
    assert result.steps[0].rankings == {"keyword": (chunks[0].id,)}
    assert not model.calls and not engine.retrieve("E1029", routes=("keyword",)).hits
    empty = engine.retrieve("E102", document_ids=frozenset())
    assert not empty.hits and not empty.steps and not model.calls


def test_replacement_preserves_prior_document_after_failed_embedding(
    engine_and_model: tuple[Engine, FixedEmbedding],
) -> None:
    """向量生成中途失败时，关键词与向量索引同时保留旧资料。"""
    engine, model = engine_and_model
    prior = engine.ingest("manual", ParsedDocument("old.md", (Part("E102 先断电", "#/p0"),)))[0]
    model.failed = True
    with pytest.raises(ValueError, match="非零"):
        engine.ingest(
            "manual", ParsedDocument("new.md", (Part("新步骤", "#/p0"), Part("失败", "#/p1")))
        )
    assert engine.retrieve("E102").hits[0].chunk == prior
    assert not engine.retrieve("新步骤", routes=("keyword",)).hits
    engine.delete("manual")
    assert not engine.retrieve("E102").hits


def test_custom_graph_cannot_expose_forbidden_path(
    engine_and_model: tuple[Engine, FixedEmbedding],
) -> None:
    """命中列表为空时，也必须校验自定义图适配器返回的路径。"""
    engine, _ = engine_and_model
    secret = engine.ingest("private", ParsedDocument("secret.md", (Part("机密", "#/p0"),)))[0]

    class BrokenGraph:
        """故意跳过索引并返回越权路径。"""

        fingerprint = "broken"

        def seeds_for(self, chunk_ids: Sequence[str], limit: int) -> tuple[str, ...]:
            return ("start",)

        def traverse(
            self,
            reader: IndexReader,
            seeds: Sequence[str],
            *,
            document_ids: frozenset[str] | None = None,
            limits: GraphLimits = _GRAPH_LIMITS,
        ) -> GraphResult:
            edge = GraphEdge("start", "points-to", "private", secret.id)
            return GraphResult((), (GraphPath("start", (edge,), (secret,)),), False)

    with pytest.raises(ValueError, match="无法在当前范围核对"):
        engine.retrieve(
            "find",
            routes=("graph",),
            graph=BrokenGraph(),
            graph_seeds=("start",),
            document_ids=frozenset({"public"}),
        )


def test_embedding_response_order_and_strict_dimension() -> None:
    """第三方响应乱序不会错配片段，重复下标和坏向量当作失败。"""
    responses = iter(
        (
            b'{"data":[{"index":1,"embedding":[0,1]},{"index":0,"embedding":[1,0]}]}',
            b'{"data":[{"index":0,"embedding":[1,0]},{"index":0,"embedding":[0,1]}]}',
            b'{"data":[{"index":0,"embedding":[true,0]}]}',
        )
    )

    class FakeProvider(BaseHTTPRequestHandler):
        """回环测试端点，按顺序返回预设响应。"""

        def do_POST(self) -> None:
            """收齐 HTTP 请求体并返回下一份测试响应。"""
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = next(responses)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), FakeProvider) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OpenAICompatibleEmbedding(
                f"http://127.0.0.1:{server.server_port}/v1", "demo", 2
            )
            assert client.embed(["first", "second"]) == ((1.0, 0.0), (0.0, 1.0))
            with pytest.raises(RuntimeError, match="数量、索引或维度"):
                client.embed(["first", "second"])
            with pytest.raises(RuntimeError, match="数量、索引或维度"):
                client.embed(["first"])
        finally:
            server.shutdown()
            thread.join()


def test_embedding_identity_distinguishes_provider_and_model() -> None:
    """模型名称与修订的分隔符、服务器协议均不能混用集合。"""
    current = OpenAICompatibleEmbedding("http://localhost/v1", "a:b", 2, revision="c")
    other_model = OpenAICompatibleEmbedding("http://localhost/v1", "a", 2, revision="b:c")
    other_provider = OpenAICompatibleEmbedding("https://localhost/v1", "a:b", 2, revision="c")
    assert len({current.fingerprint, other_model.fingerprint, other_provider.fingerprint}) == 3


def test_portable_document_respects_cross_adapter_scalar_limits() -> None:
    """源页码与标识在解析层拒绝，避免向量成功后才收到数据库格式错误。"""
    with pytest.raises(ValueError, match="页码"):
        Part("content", "#/text", 2**31)
    with pytest.raises(ValueError, match="result_id"):
        ParsedDocument("file.md", (), result_id="name\0hidden")
