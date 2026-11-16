"""验证生产索引的可信过滤、冻结、投影复制及失败语义，不以替身证明检索质量。"""

import json
import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from typing import override
from uuid import uuid4

import pytest
from inkparse import (
    AppendTextOperation,
    ExcludeOperation,
    FileSource,
    Parser,
    ParseRequest,
    revise_structure,
)
from llama_index.core.embeddings import MockEmbedding
from opensearchpy import OpenSearch
from pydantic import JsonValue

from inkrag import Error
from inkrag.documents import read_documents
from inkrag.lexical import JIEBA_LEXICAL, LEGACY_LEXICAL, LexicalTerms
from inkrag.models import ProcessingConfig, Provenance, Source
from inkrag.opensearch_keyword import keyword_query, opensearch_analyzer
from inkrag.retrieval import retrieval_mode, validate_query
from inkrag.search import SearchBackend, object_value, result_nodes
from inkrag.search_diagnostics import native_explanation
from inkrag.search_models import SearchFilter
from inkrag.search_queries import compile_filters, validate_attributes
from inkrag.sqlite_keyword import sqlite_analyzer


def test_fresh_sdk_import_keeps_strict_warnings(tmp_path: Path) -> None:
    """无字节码缓存时加载固定分词依赖，调用方的其他告警仍按错误处理。"""
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-W",
            "error",
            "-X",
            f"pycache_prefix={tmp_path / 'pycache'}",
            "-c",
            """
import warnings

from inkrag.lexical_jieba import JiebaAnalyzer

assert "传感器" in JiebaAnalyzer().analyze("传感器").terms
try:
    warnings.warn("unrelated syntax warning", SyntaxWarning)
except SyntaxWarning:
    pass
else:
    raise AssertionError("SDK import relaxed the caller's warning policy")
""",
        ],
        check=True,
        timeout=30,
    )


def test_keyword_query_uses_shared_tokens_exact_ids_and_original_vector_text() -> None:
    """两种关键词版本同用可信范围；规范化不会覆盖向量查询原文。"""
    fullwidth = "\uff41\uff42\uff0d\uff11\uff12\uff13"
    query = fullwidth + " 温度传感器故障"
    analyzer = opensearch_analyzer(JIEBA_LEXICAL)
    fields = analyzer.analyze(query)
    assert isinstance(fields, LexicalTerms)
    assert fields == sqlite_analyzer(JIEBA_LEXICAL).analyze(query)
    assert fields.identifiers == ("AB-123",)
    assert "温度传感器" in fields.terms
    assert "故障" in fields.terms
    mixed = validate_query(query, 5, "hybrid")
    keywords = mixed.keywords(analyzer)
    assert mixed.text == query and retrieval_mode(mixed, keywords) == "hybrid"
    assert keywords == fields
    scope: list[JsonValue] = [{"term": {"tenant_id": "tenant-a"}}]
    assert keyword_query(keywords, scope) == {
        "bool": {
            "should": [
                {"match": {"lexical_terms": " ".join(fields.terms)}},
                {
                    "constant_score": {
                        "filter": {"terms": {"identifiers": ["AB-123"]}},
                        "boost": 8.0,
                    }
                },
            ],
            "minimum_should_match": 1,
            "filter": scope,
        }
    }
    exact = validate_query(fullwidth, 5, "hybrid")
    keywords = exact.keywords(analyzer)
    assert retrieval_mode(exact, keywords) == "keyword"
    assert keyword_query(keywords, scope) == {
        "bool": {"must": [{"term": {"identifiers": "AB-123"}}], "filter": scope}
    }
    legacy = validate_query("AB-123", 5, "hybrid")
    native = legacy.keywords(opensearch_analyzer(LEGACY_LEXICAL))
    assert retrieval_mode(legacy, native) == "hybrid"
    assert keyword_query(native, scope) == {
        "bool": {"must": [{"match": {"text": "AB-123"}}], "filter": scope}
    }
    vector = validate_query(fullwidth, 5, "vector")
    assert retrieval_mode(vector, keywords) == "vector" and vector.text == fullwidth


def test_alternate_analyzer_uses_existing_keyword_index_contract() -> None:
    """更换词项实现时，查询路由和 OpenSearch 编译无需识别新的分词器。"""

    class AlternateAnalyzer:
        """为查询提供一组与原文不同的词项。"""

        def analyze(self, text: str, *, query: bool = False) -> LexicalTerms:
            """只接受查询分析并返回可直接索引的词项。"""
            assert query and text == "故障"
            return LexicalTerms(("故障", "异常"))

    request = validate_query("故障", 5, "hybrid")
    keywords = request.keywords(AlternateAnalyzer())
    assert retrieval_mode(request, keywords) == "hybrid"
    assert keyword_query(keywords, []) == {
        "bool": {
            "should": [{"match": {"lexical_terms": "故障 异常"}}],
            "minimum_should_match": 1,
            "filter": [],
        }
    }
    assert sqlite_analyzer(LEGACY_LEXICAL).analyze("温度异常") == LexicalTerms(
        ("温", "度", "异", "常", "温度", "度异", "异常")
    )
    assert sqlite_analyzer(LEGACY_LEXICAL).analyze("温度异常", query=True) == LexicalTerms(
        ("温度", "度异", "异常")
    )


def test_typed_filters_reject_scope_paths_and_numeric_bools() -> None:
    """条件只能访问声明的标量字段，ne保留缺失字段语义与可信范围。"""
    with pytest.raises(Error, match="未定义"):
        compile_filters("tenant-a", "kb-a", [SearchFilter(field="tenant_id", value="other")], {})
    with pytest.raises(Error, match="有限数字"):
        validate_attributes({"price": True}, {"price": "number"})
    clauses = compile_filters(
        "tenant-a",
        "kb-a",
        [SearchFilter(field="region", operator="ne", value="CN")],
        {"region": "text"},
    )
    assert clauses[0] == {"term": {"tenant_id": "tenant-a"}}
    assert clauses[1] == {"term": {"knowledge_base_id": "kb-a"}}
    assert clauses[2] == {
        "bool": {
            "filter": [{"exists": {"field": "attributes.region"}}],
            "must_not": [{"term": {"attributes.region": "CN"}}],
        }
    }


def test_production_nodes_preserve_parse_origin_and_native_split(tmp_path: Path) -> None:
    """多租户来源复用原生切分，元数据不进入正文且原件身份保持。"""
    source = tmp_path / "manual.md"
    source.write_text("# A100\n保修12个月。")
    parsed = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    nodes = result_nodes("tenant-a", "kb-a", "document-a", parsed, ProcessingConfig())
    assert nodes
    assert nodes[0].metadata["tenant_id"] == "tenant-a"
    assert nodes[0].metadata["parse_origin"]["result_id"] == parsed.result_id
    assert nodes[0].metadata["parse_origin"]["element_ref"] == "#/texts/0"
    assert "tenant-a" not in nodes[0].get_content()
    assert nodes[0].excluded_embed_metadata_keys == list(nodes[0].metadata)


def test_foreign_generation_is_rejected_before_backend_or_model() -> None:
    """物理名不属于范围时在网络调用之前拒绝，不能借用户索引路径扩大查询。"""
    client = OpenSearch(["http://127.0.0.1:1"], timeout=1, max_retries=0)
    try:
        backend = SearchBackend(
            client, MockEmbedding(embed_dim=4), model_revision="fixture", dimensions=4, replicas=0
        )
        generation = backend.generation_name("tenant-b", "kb-a", "build")
        with pytest.raises(Error) as caught:
            backend.query("tenant-a", "kb-a", generation, "query")
        assert caught.value.code == "invalid_scope"
    finally:
        client.close()


def test_native_explanation_projection_handles_single_channel_and_strips_terms() -> None:
    """压缩单向量解释不误标关键词；未知解释不猜，正文词项及嵌套内容不落盘。"""
    contribution = 1 / 62
    native: dict[str, JsonValue] = {
        "value": contribution,
        "description": "rrf combination of:",
        "details": [
            {
                "value": contribution,
                "description": "rrf, rank_constant [60] normalization of:",
                "details": [
                    {
                        "value": 0.9,
                        "description": "within top 3 docs",
                        "details": [{"description": "REVOKED-SECRET-VECTOR"}],
                    }
                ],
            }
        ],
    }
    projection = native_explanation(native, contribution, "hybrid", 1)
    assert projection["status"] == "captured"
    channels = projection["channels"]
    assert isinstance(channels, list) and len(channels) == 1
    assert isinstance(channels[0], dict)
    assert channels[0]["channel"] == "vector" and channels[0]["rank"] == 2
    assert "REVOKED" not in json.dumps(projection)
    malformed = native.copy()
    malformed["description"] = "unrecognized future combination"
    assert native_explanation(malformed, contribution, "hybrid", 1) == {
        "status": "unrecognized",
        "channels": [],
    }


class RecordingSearch(SearchBackend):
    """记录真实搜索请求以验证诊断没有另发通道查询，不代替真实搜索后端。"""

    def __init__(self, backend: SearchBackend) -> None:
        """借入本次测试的真实客户端和固定测试向量。"""
        super().__init__(
            backend.client,
            backend.embedding,
            model_revision=backend.model_revision,
            dimensions=backend.dimensions,
            replicas=backend.replicas,
        )
        self.search_requests: list[dict[str, JsonValue]] = []

    @override
    def _request(
        self, method: str, path: str, body: object = None, params: dict[str, str] | None = None
    ) -> dict[str, JsonValue]:
        """只记录发送形状，不打印正文、凭据或原始向量。"""
        if method == "POST" and path.endswith("/_search"):
            self.search_requests.append(object_value(body))
        return super()._request(method, path, body, params)


@pytest.mark.integration
def test_real_diagnostics_same_response_scope_revocation_and_default_cost(
    search_backend: SearchBackend, tmp_path: Path
) -> None:
    """真实OpenSearch同次RRF解释、普通无explain、跨租户/撤销/过滤零命中保持。"""
    backend = RecordingSearch(search_backend)
    tenant, kb = uuid4().hex, uuid4().hex
    generation = backend.create_build(tenant, kb, uuid4().hex, field_schema={"region": "text"})
    a, b, c, alien = [uuid4().hex for _ in range(4)]
    try:
        for document_id, region, text in [
            (a, "CN", "APPROVED-ALPHA"),
            (b, "CN", "REVOKED-SECRET"),
            (c, "US", "FILTERED-SECRET"),
        ]:
            path = tmp_path / f"{document_id}.md"
            path.write_text(f"# {text}\n保修12个月。")
            parsed = Parser().parse(ParseRequest(source=FileSource(path=str(path))))
            backend.upsert_result(tenant, kb, generation, document_id, parsed, {"region": region})
        # 同物理索引故意放入另一租户节点，验证两个召回通道都先执行可信过滤。
        node = result_nodes("alien-tenant", kb, alien, parsed, ProcessingConfig())[0]
        backend.client.transport.perform_request(
            "PUT",
            f"/{generation}/_doc/{alien}",
            body=object_value(
                {
                    "tenant_id": "alien-tenant",
                    "knowledge_base_id": kb,
                    "document_id": alien,
                    "result_id": parsed.result_id,
                    "text": node.get_content(),
                    "embedding": [0.5] * 4,
                    "attributes": {"region": "CN"},
                    "node": node.model_dump(mode="json", exclude={"embedding"}),
                }
            ),
            params={"refresh": "true"},
        )
        backend.freeze(tenant, kb, generation)
        backend.search_requests.clear()
        normal = backend.query_json(
            tenant,
            kb,
            generation,
            "保修",
            filters=[{"field": "region", "value": "CN"}],
            excluded_document_ids=[b],
        )
        assert "diagnostics" not in normal
        assert len(backend.search_requests) == 1
        assert (
            "explain" not in backend.search_requests[0]
            and "search_pipeline" not in backend.search_requests[0]
        )
        for mode in ("hybrid", "keyword", "vector"):
            backend.search_requests.clear()
            response = backend.query_json(
                tenant,
                kb,
                generation,
                "保修",
                mode=mode,
                filters=[{"field": "region", "value": "CN"}],
                excluded_document_ids=[b],
                diagnostics=True,
            )
            assert (
                len(backend.search_requests) == 1 and backend.search_requests[0]["explain"] is True
            )
            diagnostic = object_value(response["diagnostics"])
            assert diagnostic["same_response"] is True
            assert diagnostic["pre_filter_candidate_count"] is None
            explanations = diagnostic["hits"]
            assert isinstance(explanations, list) and explanations
            for explanation in explanations:
                hit = object_value(explanation)
                assert hit["document_id"] == a and hit["status"] == "captured"
                channels = hit["channels"]
                assert isinstance(channels, list) and channels
                assert {str(object_value(channel)["channel"]) for channel in channels} == (
                    {"keyword", "vector"} if mode == "hybrid" else {mode}
                )
            projected = json.dumps(diagnostic)
            assert all(
                secret not in projected
                for secret in [
                    "SECRET",
                    "APPROVED",
                    "embedding",
                    "description",
                    "details",
                    alien,
                    b,
                    c,
                ]
            )
            if mode == "hybrid":
                assert response["hits"] == normal["hits"]
        empty = backend.query_json(
            tenant,
            kb,
            generation,
            "保修",
            filters=[{"field": "region", "value": "NO-SUCH-REGION"}],
            diagnostics=True,
        )
        assert empty["hits"] == [] and object_value(empty["diagnostics"])["hits"] == []
        with pytest.raises(Error, match="详细诊断"):
            backend.query_json(tenant, kb, generation, "保修", top_k=21, diagnostics=True)
        with pytest.raises(Error) as denied:
            backend.query_json("other-tenant", kb, generation, "保修", diagnostics=True)
        assert denied.value.code == "invalid_scope"
    finally:
        backend.client.transport.perform_request("DELETE", "/" + generation)


@pytest.fixture
def search_backend() -> Generator[SearchBackend, None, None]:
    """显式启用真实OpenSearch；固定向量只隔离模型，不证明业务召回质量。"""
    if os.environ.get("INKRAG_SEARCH_INTEGRATION") != "1":
        pytest.skip("需要显式启用专用OpenSearch集成环境")
    endpoint = os.environ["INKRAG_OPENSEARCH_TEST_URL"]
    username, password = (
        os.environ.get("INKRAG_OPENSEARCH_TEST_USER"),
        os.environ.get("INKRAG_OPENSEARCH_TEST_PASSWORD"),
    )
    client = OpenSearch(
        [endpoint],
        http_auth=(username, password) if username and password else None,
        timeout=30,
        max_retries=1,
        verify_certs=True,
    )
    backend = SearchBackend(
        client,
        MockEmbedding(model_name="search-integration-only", embed_dim=4),
        model_revision="fixture:deterministic4",
        dimensions=4,
        replicas=0,
    )
    yield backend
    client.close()


@pytest.mark.integration
def test_real_search_filter_freeze_copy_and_immutable_release(
    search_backend: SearchBackend, tmp_path: Path
) -> None:
    """真实索引验证前置过滤、RRF、原生复制和后端拒绝迟到写，清理仅本次物理名。"""
    backend, tenant, kb = search_backend, uuid4().hex, uuid4().hex
    generations: list[str] = []
    try:
        generation = backend.create_build(
            tenant, kb, uuid4().hex, field_schema={"region": "text", "price": "number"}
        )
        generations.append(generation)
        source = tmp_path / "manual.md"
        source.write_text("# A100\nA100保修12个月。")
        first = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
        backend.upsert_result(tenant, kb, generation, "doc-a", first, {"region": "CN", "price": 10})
        source.write_text("# B200\nB200保修24个月。")
        second = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
        backend.upsert_result(
            tenant, kb, generation, "doc-b", second, {"region": "US", "price": 20}
        )
        manifest = backend.freeze(tenant, kb, generation)
        assert manifest.document_count == 2 and manifest.node_count >= 2
        for mode in ("keyword", "vector", "hybrid"):
            hits = backend.query(
                tenant,
                kb,
                generation,
                "保修",
                mode=mode,
                filters=[SearchFilter(field="region", value="CN")],
            )
            assert hits and all(hit.metadata["document_id"] == "doc-a" for hit in hits)
        with pytest.raises(Error) as frozen:
            backend.upsert_result(tenant, kb, generation, "doc-a", second)
        assert frozen.value.code == "index_frozen"
        candidate = backend.create_build(
            tenant,
            kb,
            uuid4().hex,
            previous_generation=generation,
            field_schema={"region": "text", "price": "number"},
        )
        generations.append(candidate)
        backend.upsert_result(tenant, kb, candidate, "doc-a", second, {"region": "CN", "price": 12})
        changed = backend.freeze(tenant, kb, candidate)
        assert changed.document_count == manifest.document_count
        old_hits = backend.query(tenant, kb, generation, "A100", mode="keyword")
        new_hits = backend.query(
            tenant,
            kb,
            candidate,
            "B200",
            mode="keyword",
            filters=[SearchFilter(field="region", value="CN")],
        )
        assert old_hits and old_hits[0].metadata["parse_origin"]["result_id"] == first.result_id
        assert new_hits and new_hits[0].metadata["parse_origin"]["result_id"] == second.result_id
        print("真实OpenSearch：可信过滤/RRF/冻结拒绝写/向量复制/旧发布不变通过")
    finally:
        for generation in generations:
            backend.client.transport.perform_request("DELETE", "/" + generation)


def test_reviewed_exclusion_is_shared_by_sdk_and_production_nodes(tmp_path: Path) -> None:
    """人工排除内容不会进入任一检索入口，结果原文仍完整可追溯。"""
    source = tmp_path / "review.html"
    source.write_text(
        "<h1>保修</h1><p>保修12个月。</p><footer><p>NOISE-FOOTER 不应检索。</p></footer>"
    )
    original = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    noise = next(item for item in original.document.texts if "NOISE-FOOTER" in item.text)
    revised = revise_structure(
        original,
        [
            ExcludeOperation(kind="exclude", element_ref=noise.self_ref, excluded=True),
            AppendTextOperation(kind="append_text", text="人工确认：换新7天。"),
        ],
    )
    nodes = result_nodes("tenant-a", "kb-a", "document-a", revised, ProcessingConfig())
    assert "NOISE-FOOTER" not in " ".join(node.get_content() for node in nodes)
    assert "人工确认" in " ".join(node.get_content() for node in nodes)
    assert "保修12个月" in " ".join(node.get_content() for node in nodes)
    origin = Provenance(
        document_id="document-a",
        resource_id="resource-a",
        resource_revision=1,
        source=Source(name=source.name, uri="fixture"),
        format="html",
        processing_revision="fixture",
    )
    sdk_documents = read_documents(source, origin, revised)
    assert "NOISE-FOOTER" not in " ".join(document.get_content() for document in sdk_documents)
    assert "人工确认" in " ".join(document.get_content() for document in sdk_documents)
    assert (
        next(item for item in revised.document.texts if item.self_ref == noise.self_ref).text
        == noise.text
    )
    assert all(node.metadata["parse_origin"]["result_id"] == revised.result_id for node in nodes)
