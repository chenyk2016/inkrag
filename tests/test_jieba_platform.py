"""以专用 PostgreSQL 与 OpenSearch 验证分词版本、真实 Worker 构建及接入一致性。"""

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, override
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from inkparse import FileSource, Parser, ParseRequest
from inkrag_platform import sdk, worker, workspace
from inkrag_platform.api import create_app
from inkrag_platform.auth import OIDCVerifier
from inkrag_platform.models import Build, DomainError, MetadataField, Principal
from inkrag_platform.operations import Runtime
from inkrag_platform.repository import Repository
from inkrag_platform.settings import Settings
from inkrag_platform.wiring import Authentication
from inkrag_platform.workspace_models import (
    SelectionRequest,
    ServiceConfig,
    ServiceUpdate,
    TrialRequest,
)
from inkrag_platform.workspace_query import query_target
from inkstore import AssetStorage
from inkstore.local import LocalAssetStore
from llama_index.core.embeddings import MockEmbedding
from opensearchpy import OpenSearch, TransportError
from pydantic import JsonValue, PrivateAttr, SecretStr, TypeAdapter

from inkrag import RAG, Error
from inkrag.lexical import JIEBA_LEXICAL
from inkrag.published import PublishedRetrieval
from inkrag.search import SearchBackend

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("INKRAG_TEST_DATABASE_DSN")
        or not os.environ.get("INKRAG_OPENSEARCH_TEST_URL")
        or os.environ.get("INKRAG_SEARCH_INTEGRATION") != "1",
        reason="需要独立 PostgreSQL 测试库及显式启用 OpenSearch 集成",
    ),
]


class CountingEmbedding(MockEmbedding):
    """固定测试向量隔离模型，记录两路调用量以检查复用及纯编号短路。"""

    _document_calls: int = PrivateAttr(default=0)
    _query_calls: int = PrivateAttr(default=0)

    @override
    def _get_text_embedding(self, text: str) -> list[float]:
        """计数每个重新生成的正文向量。"""
        self._document_calls += 1
        return super()._get_text_embedding(text)

    @override
    def _get_query_embedding(self, query: str) -> list[float]:
        """计数进入语义召回的请求。"""
        self._query_calls += 1
        return super()._get_query_embedding(query)

    @property
    def calls(self) -> tuple[int, int]:
        """返回正文和查询的嵌入调用量。"""
        return self._document_calls, self._query_calls


def ordered_hits(payload: dict[str, JsonValue]) -> list[str]:
    """校验同次返回的原文定位，并保留数据库返回顺序。"""
    hits = payload["hits"]
    assert isinstance(hits, list)
    result: list[str] = []
    for hit in hits:
        assert isinstance(hit, dict)
        metadata = hit["metadata"]
        assert isinstance(metadata, dict)
        origin = metadata["parse_origin"]
        assert isinstance(origin, dict) and origin["element_ref"] == "#/texts/0"
        assert isinstance(origin["result_id"], str) and len(origin["result_id"]) == 64
        identity = metadata["document_id"]
        assert isinstance(identity, str)
        result.append(identity)
    return result


def test_jieba_worker_release_and_published_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实构建、发布、回滚及撤销共用版本规则；资产使用临时目录，模型为计数替身。"""
    dsn = os.environ["INKRAG_TEST_DATABASE_DSN"]
    repo = Repository(dsn)
    username = os.environ.get("INKRAG_OPENSEARCH_TEST_USER")
    password = os.environ.get("INKRAG_OPENSEARCH_TEST_PASSWORD")
    client = OpenSearch(
        [os.environ["INKRAG_OPENSEARCH_TEST_URL"]],
        http_auth=(username, password) if username and password else None,
        timeout=30,
        max_retries=1,
    )
    embedding = CountingEmbedding(embed_dim=4)
    legacy = SearchBackend(
        client, embedding, model_revision="jieba-test4", dimensions=4, replicas=0
    )
    jieba = SearchBackend(
        client,
        embedding,
        model_revision=legacy.model_revision,
        dimensions=4,
        replicas=0,
        lexical_profile=JIEBA_LEXICAL,
    )
    actor = Principal(tenant_id="JIEBA-VERIFY-" + uuid4().hex, subject="admin", roles=["admin"])
    repo.check()
    kb = repo.create_kb(actor, "分词发布验收")
    repo.set_fields(
        actor, kb.kb_id, kb.revision, [MetadataField(name="region", label="区域", type="text")]
    )
    workspace.update_service(
        repo,
        actor,
        kb.kb_id,
        ServiceUpdate(
            expected_revision=0,
            config=ServiceConfig(
                mode="keyword",
                top_k=10,
                allowed_fields=["mode", "filters"],
                allowed_filter_fields=["region"],
            ),
        ),
    )
    storage = LocalAssetStore.open(tmp_path / "assets")
    settings = Settings.model_construct(capabilities="rag", database_url=SecretStr(dsn))
    auth = Authentication(repo, OIDCVerifier("https://identity.invalid", "inkrag-test"))
    active_search = legacy

    def assets(_principal: Principal) -> AssetStorage:
        """临时资产仅含本次真实 Parser 结果。"""
        return storage

    def selected_document(filename: str, text: str, region: str) -> str:
        """保存解析产物、完成持久解析任务，再按正式业务规则明确选用。"""
        path = tmp_path / filename
        path.write_text(text, encoding="utf-8")
        parsed = Parser().parse(ParseRequest(source=FileSource(path=str(path))), assets=storage)
        assert parsed.source.asset_id
        job = repo.submit_parse(
            actor, kb.kb_id, filename, parsed.source.asset_id, filename, {"region": region}
        )
        lease = repo.claim_job(job.job_id, "lexical-fixture")
        assert lease
        doc = repo.complete_parse(
            job.job_id, "lexical-fixture", lease.lease_epoch, parsed.result_id, 1, None, []
        )
        workspace.select_result(
            repo,
            actor,
            doc.document_id,
            SelectionRequest(expected_document_revision=doc.revision, result_id=parsed.result_id),
        )
        return doc.document_id

    @contextmanager
    def connect(_settings: Settings, *, batch: bool = False) -> Generator[Runtime]:
        """只替换基础设施装配，Worker 执行、两种查询入口和真实检索沿用产品代码。"""
        yield Runtime(repo, assets, auth, active_search if batch else jieba, {})

    monkeypatch.setattr(worker, "open_runtime", connect)
    monkeypatch.setattr(sdk, "open_runtime", connect)

    def build(key: str) -> Build:
        """走真实投递入口，由 Worker 提交候选及任务状态。"""
        job = repo.accept_job(actor, "build", kb.kb_id, None, {}, key)
        worker.execute_job(settings, job.job_id)
        done = repo.get_job(actor, job.job_id)
        assert done.status == "succeeded", done.error_code
        return next(item for item in repo.list_builds(actor, kb.kb_id) if item.job_id == job.job_id)

    def compare(
        rag: PublishedRetrieval,
        http: TestClient,
        query: str,
        mode: Literal["keyword", "hybrid"],
        filters: list[dict[str, JsonValue]] | None = None,
    ) -> tuple[dict[str, JsonValue], list[str]]:
        """同一发布的 HTTP 和 Python 输出有序一致，分别生成可查追踪。"""
        direct = rag.retrieve(query, mode=mode, filters=filters)
        body: dict[str, JsonValue] = {"kb_id": kb.kb_id, "query": query, "mode": mode}
        if filters is not None:
            body["filters"] = [*filters]
        result = http.post("/api/v2/retrieve", json=body)
        result.raise_for_status()
        response = TypeAdapter(dict[str, JsonValue]).validate_json(result.content)
        assert direct.pop("trace_id") != response.pop("trace_id")
        assert direct == response
        return direct, ordered_hits(direct)

    try:
        correct = selected_document("correct.txt", "AB-123 电机过热，先切断电源。", "CN")
        near = selected_document("near.txt", "AB-1234 电机过热，检查环境温度。", "CN")
        outside = selected_document("outside.txt", "AB-123 电机过热，联系售后。", "US")
        identifier_only = selected_document("only-id.txt", "ZX-900", "CN")
        outsider = Principal(
            tenant_id="JIEBA-OTHER-" + uuid4().hex, subject="admin", roles=["admin"]
        )
        foreign_kb = repo.create_kb(outsider, "其他租户知识库")
        first_build = build("legacy")
        first = repo.publish_build(actor, first_build.build_id)
        key = repo.create_api_key(actor, kb.kb_id, "lexical-contract")
        runtime = Runtime(repo, assets, auth, jieba, {})
        with (
            TestClient(
                create_app(runtime), headers={"Authorization": "Bearer " + key.token}
            ) as http,
            RAG.open_published(
                sdk.published_connection(kb.kb_id, key.token, settings=settings)
            ) as rag,
        ):
            denied_query = http.post(
                "/api/v2/retrieve", json={"kb_id": foreign_kb.kb_id, "query": "AB-123"}
            )
            assert denied_query.status_code == 404
            old, old_ids = compare(rag, http, "AB-123", "keyword")
            assert old["release_id"] == first.release_id and set(old_ids) == {
                correct,
                near,
                outside,
            }
            active_search = jieba
            before, _ = embedding.calls
            second_build = build("switch-profile")
            assert embedding.calls[0] == before + 4
            still_old, _ = compare(rag, http, "AB-123", "keyword")
            assert still_old == old
            # 编号单独成块时没有正文词项，仍须完整建入、冻结并生成真实通道解释。
            exact_trial = query_target(
                runtime,
                actor,
                second_build.build_id,
                TrialRequest(query="ZX-900", mode="hybrid", diagnostics=True),
            )
            assert ordered_hits(exact_trial) == [identifier_only]
            exact_explanation = exact_trial["diagnostics"]
            assert isinstance(exact_explanation, dict) and exact_explanation["mode"] == "keyword"
            mixed_trial = query_target(
                runtime,
                actor,
                second_build.build_id,
                TrialRequest(query="ZX-900 拆解步骤", mode="hybrid", diagnostics=True),
            )
            mixed_explanation = mixed_trial["diagnostics"]
            assert isinstance(mixed_explanation, dict)
            details = mixed_explanation["hits"]
            assert isinstance(details, list)
            for detail in details:
                assert isinstance(detail, dict)
                if detail["document_id"] == identifier_only:
                    assert detail["status"] == "captured"
                    channels = detail["channels"]
                    assert isinstance(channels, list)
                    channel_names: set[str] = set()
                    for channel in channels:
                        assert isinstance(channel, dict)
                        name = channel["channel"]
                        assert isinstance(name, str)
                        channel_names.add(name)
                    assert channel_names == {"keyword", "vector"}
                    break
            else:
                pytest.fail("混合检索的同次解释缺少编号命中")
            second = repo.publish_build(actor, second_build.build_id, first.release_id)
            before_query = embedding.calls[1]
            for mode in ("keyword", "hybrid"):
                current, current_ids = compare(
                    rag, http, "\uff41\uff42\uff0d\uff11\uff12\uff13", mode
                )
                assert current["release_id"] == second.release_id
                assert set(current_ids) == {correct, outside}
                assert compare(rag, http, "AB-1230", mode)[1] == []
            assert embedding.calls[1] == before_query
            assert compare(rag, http, "AB-123 电机过热", "keyword")[1][0] in {correct, outside}
            _, scoped = compare(
                rag, http, "AB-123", "hybrid", filters=[{"field": "region", "value": "CN"}]
            )
            assert scoped == [correct]
            with pytest.raises(Error) as frozen:
                jieba.remove_document(actor.tenant_id, kb.kb_id, second_build.index_name, correct)
            assert frozen.value.code == "index_frozen"
            before_copy = embedding.calls[0]
            copied = build("same-profile-copy")
            assert embedding.calls[0] == before_copy
            third = repo.publish_build(actor, copied.build_id, second.release_id)
            assert set(compare(rag, http, "AB-123", "keyword")[1]) == {correct, outside}
            rolled = repo.rollback_release(actor, first.release_id, third.release_id)
            old_again, old_again_ids = compare(rag, http, "AB-123", "keyword")
            assert old_again["release_id"] == rolled.release_id
            assert set(old_again_ids) == {correct, near, outside}
            doc = repo.get_document(actor, correct)
            repo.delete_document(actor, correct, doc.revision)
            assert correct not in compare(rag, http, "AB-123", "keyword")[1]
            with pytest.raises(DomainError) as denied:
                repo.rollback_release(actor, second.release_id, rolled.release_id)
            assert denied.value.code == "authorization_pending"
    finally:
        try:
            for built in repo.list_builds(actor, kb.kb_id):
                try:
                    client.transport.perform_request("DELETE", "/" + built.index_name)
                except TransportError as exc:
                    if exc.status_code != 404:
                        raise
        finally:
            client.close()
            repo.close()
