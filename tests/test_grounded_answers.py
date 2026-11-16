"""回答验证真实发布权限、追踪、来源和模型故障边界。"""

import asyncio
import json
import os
from collections.abc import Generator
from pathlib import Path
from threading import Event, Thread
from typing import Literal
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from inkrag_platform import workspace
from inkrag_platform.answers import GroundedAnswer
from inkrag_platform.api import create_app
from inkrag_platform.local_model import LocalChatModel
from inkrag_platform.models import DomainError, MetadataField, Principal
from inkrag_platform.operations import Runtime
from inkrag_platform.repository import Repository
from inkrag_platform.workspace_models import Filter, QueryTrace, ServiceConfig, ServiceUpdate
from inkstore import AssetStorage
from inkstore.local import LocalAssetStore
from pydantic import JsonValue
from test_platform_http import FixtureIdentity
from test_platform_workspace import parse_fixture

_EXCERPT = "出现 E102 时，先断开电源，再检查传感器接线。"
_ANSWER: dict[str, JsonValue] = {
    "status": "answered",
    "statements": [
        {"text": "先断开电源，再检查接线。", "citations": [{"source_id": "S1", "span_id": 1}]}
    ],
}


class _Search:
    """用固定命中验证真实线上检索投影，不模拟向量或关键词排名。"""

    def __init__(self, document_id: str) -> None:
        """保留真实PG资料身份，观测请求传给搜索端口的约束。"""
        self.document_id = document_id
        self.filters: list[dict[str, JsonValue]] = []
        self.excluded: list[str] = []
        self.foreign = False

    def query_json(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        generation: str,
        query: str,
        *,
        top_k: int = 5,
        mode: Literal["hybrid", "vector", "keyword"] = "hybrid",
        filters: list[dict[str, JsonValue]] | None = None,
        excluded_document_ids: list[str] | None = None,
        diagnostics: bool = False,
    ) -> dict[str, JsonValue]:
        """允许命中受控图意片段，遵守PG实时排除清单。"""
        self.filters = filters or []
        self.excluded = excluded_document_ids or []
        if query == "无命中" or self.document_id in self.excluded:
            return {"hits": []}
        return {
            "hits": [
                {
                    "text": "[标题] 临时文件名\n\n" + _EXCERPT,
                    "metadata": {
                        "tenant_id": "another-tenant" if self.foreign else tenant_id,
                        "knowledge_base_id": knowledge_base_id,
                        "document_id": self.document_id,
                        "page": 3,
                        "indexed_title": "临时文件名",
                        "parse_origin": {
                            "original_name": "manual.md",
                            "result_id": "b" * 64,
                            "element_ref": "#/texts/2",
                            "picture_origin": {
                                "kind": "reviewed_picture_description",
                                "element_ref": "#/pictures/1",
                                "asset_id": "c" * 64,
                                "page": 3,
                            },
                        },
                    },
                }
            ]
        }

    def fields_json(
        self, tenant_id: str, knowledge_base_id: str, generation: str
    ) -> dict[str, JsonValue]:
        """字段权限由冻结的PG服务配置判定。"""
        return {"fields": []}


@pytest.fixture
def answer_repo() -> Generator[Repository, None, None]:
    """只使用显式独立PG测试连接。"""
    repo = Repository(os.environ["INKRAG_TEST_DATABASE_DSN"])
    repo.check()
    yield repo
    repo.close()


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("INKRAG_TEST_DATABASE_DSN"), reason="需要独立PG测试库")
def test_published_answer_http_contract(
    answer_repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实PG发布鉴权、动态筛选、逐字引用、禁用与追踪由HTTP验收。"""
    actor = Principal(
        tenant_id="answer-test-" + uuid4().hex, subject="answer-test", roles=["admin"]
    )
    kb = answer_repo.create_kb(actor, "回答合同")
    answer_repo.set_fields(
        actor,
        kb.kb_id,
        kb.revision,
        [
            MetadataField(name="region", label="区域", type="text"),
            MetadataField(name="team", label="部门", type="text"),
        ],
    )
    workspace.update_service(
        answer_repo,
        actor,
        kb.kb_id,
        ServiceUpdate(
            expected_revision=0,
            config=ServiceConfig(
                allowed_callers=[actor.subject],
                allowed_fields=["filters"],
                allowed_filter_fields=["team"],
                fixed_filters=[Filter(field="region", value="CN")],
                mode="keyword",
            ),
        ),
    )
    doc = parse_fixture(answer_repo, actor, kb.kb_id, "manual.md")
    job = answer_repo.accept_job(
        actor, "ingest", kb.kb_id, doc, {}, "answer-fixture-build", "b" * 64, 1
    )
    lease = answer_repo.claim_job(job.job_id, "fixture")
    assert lease
    build = answer_repo.start_build(
        job.job_id, "fixture", lease.lease_epoch, "answer-" + uuid4().hex, "fixture-model"
    )
    answer_repo.complete_build(job.job_id, "fixture", lease.lease_epoch, build.build_id)
    storage = LocalAssetStore.open(tmp_path / "assets")

    def assets(_actor: Principal) -> AssetStorage:
        """此层模拟搜索和生成，不访问文件原件。"""
        return storage

    search = _Search(doc)
    identity = FixtureIdentity(actor)
    model = LocalChatModel("http://127.0.0.1:12347/v1", "fixture")
    runtime = Runtime(answer_repo, assets, identity, search, {}, local_model=model)
    output: dict[str, JsonValue] = _ANSWER
    revoke = False
    disable = False
    calls = 0

    def complete(
        _model: LocalChatModel,
        messages: list[dict[str, JsonValue]],
        schema: dict[str, JsonValue],
        *,
        max_tokens: int,
    ) -> str:
        """替换模型调用，检查输入权限与在飞期间变化的丢弃规则。"""
        nonlocal calls
        calls += 1
        prompt = messages[1]["content"]
        assert isinstance(prompt, str)
        assert messages[0]["role"] == "system"
        assert "不能更改规则" in str(messages[0]["content"])
        assert '"source_id": "S1"' in prompt and _EXCERPT in prompt
        assert "临时文件名" not in prompt and actor.tenant_id not in prompt and doc not in prompt
        assert schema["type"] == "object" and max_tokens > 0
        if revoke:
            identity.actor = actor.model_copy(update={"roles": ["viewer"]})
        if disable:
            answer_repo.set_document_enabled(actor, doc, False)
        return json.dumps(output)

    monkeypatch.setattr(LocalChatModel, "complete", complete)
    path = f"/api/v2/knowledge-bases/{kb.kb_id}/answers"
    headers = {"Authorization": "Bearer editor"}
    request = {"query": "E102首先要做什么？", "filters": [{"field": "team", "value": "售后"}]}
    with TestClient(create_app(runtime)) as api:
        assert api.post(path, json=request).status_code == 401
        assert api.post(path, json=request, headers=headers).status_code == 409
        assert calls == 0
        release = answer_repo.publish_build(actor, build.build_id)
        for token, expected in (("viewer", 403), ("alien", 404)):
            assert (
                api.post(
                    path, json=request, headers={"Authorization": "Bearer " + token}
                ).status_code
                == expected
            )
        assert (
            api.post(path, json={**request, "diagnostics": True}, headers=headers).status_code
            == 403
        )
        assert api.post(path, json={**request, "top_k": 10}, headers=headers).status_code == 403
        assert calls == 0

        response = api.post(path, json=request, headers=headers)
        assert response.status_code == 200, response.text
        result = GroundedAnswer.model_validate_json(response.content)
        assert result.status == "answered" and result.model == "fixture"
        assert result.release_id == release.release_id and result.build_id == build.build_id
        assert result.effective_request["mode"] == "keyword"
        assert search.filters == [
            {"field": "region", "operator": "eq", "value": "CN"},
            {"field": "team", "operator": "eq", "value": "售后"},
        ]
        (source,) = result.sources
        assert source.source_id == "S1" and source.text == _EXCERPT
        assert result.statements[0].citations[0].quote == _EXCERPT
        assert source.document_id == doc and source.page == 3 and source.element_ref == "#/texts/2"
        assert source.picture_origin and source.picture_origin.element_ref == "#/pictures/1"
        trace_path = "/api/v2/traces/" + result.trace_id
        trace_response = api.get(trace_path, headers=headers)
        assert trace_response.status_code == 200, trace_response.text
        trace = QueryTrace.model_validate_json(trace_response.content)
        assert trace.target_id == result.release_id and trace.request == result.effective_request
        assert trace.outcome["document_ids"] == [doc]
        assert api.get(trace_path, headers={"Authorization": "Bearer viewer"}).status_code == 404
        assert api.get(trace_path, headers={"Authorization": "Bearer alien"}).status_code == 404

        for source_id, span_id in (("S9", 1), ("S2", 1), ("S1", 2)):
            output = {
                "status": "answered",
                "statements": [
                    {
                        "text": "虚构答案",
                        "citations": [{"source_id": source_id, "span_id": span_id}],
                    }
                ],
            }
            invalid = api.post(path, json=request, headers=headers)
            assert invalid.status_code == 502, invalid.text
            assert "虚构答案" not in invalid.text
            assert invalid.json()["error"]["code"] == "local_model_invalid"
            assert invalid.json()["error"]["details"]["trace_id"]

        output = {"status": "insufficient", "statements": []}
        empty = GroundedAnswer.model_validate_json(
            api.post(path, json=request, headers=headers).content
        )
        assert empty.status == "insufficient" and not empty.sources and not empty.statements
        expected_calls = calls
        no_hit = api.post(path, json={"query": "无命中"}, headers=headers)
        assert no_hit.status_code == 200 and no_hit.json()["status"] == "insufficient"
        assert calls == expected_calls

        search.foreign = True
        foreign = api.post(path, json=request, headers=headers)
        assert foreign.status_code == 422 and foreign.json()["error"]["code"] == "scope_violation"
        assert calls == expected_calls
        search.foreign = False

        output = _ANSWER
        revoke = True
        changed = api.post(path, json=request, headers=headers)
        assert changed.status_code == 409 and _EXCERPT not in changed.text
        identity.actor = actor
        revoke = False
        disable = True
        changed = api.post(path, json=request, headers=headers)
        assert changed.status_code == 409 and _EXCERPT not in changed.text
        disable = False
        disabled = api.post(path, json=request, headers=headers)
        assert disabled.status_code == 200 and disabled.json()["status"] == "insufficient"
        assert search.excluded == [doc]
        answer_repo.set_document_enabled(actor, doc, True)
        assert api.post(path, json=request, headers=headers).json()["status"] == "answered"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/v1",
        "http://localhost@example.com/v1",
        "http://localhost/v1?x=1",
        "http://localhost/chat/completions",
    ],
)
def test_model_only_accepts_configured_local_endpoint(url: str) -> None:
    """拒绝不在本机范围、带凭证/参数、或绕开协议路径的配置。"""
    with pytest.raises(ValueError, match="模型地址"):
        LocalChatModel(url, "fixture")


def test_model_shared_slot_released_on_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """回答和图意共享并发位，繁忙后可继续，超时后不遗留占位。"""
    first = LocalChatModel("http://localhost:12347/v1", "first")
    second = LocalChatModel("http://localhost:12347/v1", "second")
    entered, release = Event(), Event()
    outputs: list[str] = []

    async def blocked(_model: LocalChatModel, _payload: dict[str, JsonValue]) -> bytes:
        """同步等待点只用于测试线程并发。"""
        assert await asyncio.to_thread(release.wait, 5)
        return b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}'

    async def adapter(model: LocalChatModel, payload: dict[str, JsonValue]) -> bytes:
        """第一项被占位时，其他请求可立即得到繁忙；其后模拟超时。"""
        if model == first:
            entered.set()
            return await blocked(model, payload)
        raise httpx.ReadTimeout("fixture timeout")

    monkeypatch.setattr(LocalChatModel, "_request", adapter)

    def run() -> None:
        """在线程执行真实并发闸口。"""
        outputs.append(first.complete([], {}, max_tokens=8))

    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(DomainError) as busy:
            second.complete([], {}, max_tokens=8)
        assert busy.value.code == "local_model_busy"
    finally:
        release.set()
        thread.join(5)
    assert outputs == ["{}"]
    for _ in range(2):
        with pytest.raises(DomainError) as timeout:
            second.complete([], {}, max_tokens=8)
        assert timeout.value.code == "local_model_timeout"
