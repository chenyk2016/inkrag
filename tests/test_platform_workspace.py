"""工作台必须使用真实PG验证选用、全局范围、并发比较及不可变候选配置。"""

import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from inkparse import FileSource, Parser, ParseRequest, ParseResult, save_stored_result
from inkrag_platform import sdk, workspace
from inkrag_platform.api import create_app
from inkrag_platform.auth import OIDCVerifier
from inkrag_platform.models import Document, DomainError, ExpectedSource, MetadataField, Principal
from inkrag_platform.operations import Runtime
from inkrag_platform.quality import create_quality_sample, save_quality_sample
from inkrag_platform.repository import Repository
from inkrag_platform.settings import Settings
from inkrag_platform.wiring import Authentication
from inkrag_platform.workspace_models import (
    AdoptionRequest,
    Filter,
    QualitySampleRequest,
    SelectionRequest,
    ServiceConfig,
    ServiceUpdate,
    TrialRequest,
)
from inkrag_platform.workspace_query import evaluate_candidate, query_target
from inkstore import AssetStorage
from inkstore.local import LocalAssetStore
from pydantic import JsonValue, TypeAdapter

from inkrag import RAG

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("INKRAG_TEST_DATABASE_DSN"), reason="需要真实PG测试DSN"),
]


@pytest.fixture
def repo() -> Generator[Repository, None, None]:
    """显式本地PG运行账户，不用SQLite模拟生产。"""
    repository = Repository(os.environ["INKRAG_TEST_DATABASE_DSN"])
    repository.check()
    yield repository
    repository.close()


@pytest.fixture
def actor() -> Principal:
    """每项验收创建独立命名租户，旧资料从不作为测试写入对象。"""
    return Principal(tenant_id="PM-MIGRATION-" + uuid4().hex, subject="pm-review", roles=["admin"])


def parse_fixture(
    repo: Repository,
    actor: Principal,
    kb_id: str,
    name: str,
    *,
    asset_ids: list[str] | None = None,
) -> str:
    """只创建事务事实；真实字节解析另由贯穿用例验证。"""
    job = repo.submit_parse(actor, kb_id, name, "a" * 64, uuid4().hex)
    lease = repo.claim_job(job.job_id, "fixture")
    assert lease
    return repo.complete_parse(
        job.job_id, "fixture", lease.lease_epoch, "b" * 64, 1, None, asset_ids or []
    ).document_id


def test_global_counts_pagination_scope_and_ownership(repo: Repository, actor: Principal) -> None:
    """跨库汇总超过100项仍准确，分页/搜索保留真实集合归属且绑定凭证不越界。"""
    kb = repo.create_kb(actor, "PM-MIGRATION-知识库")
    independent = repo.create_kb(actor, "PM-MIGRATION-独立解析", "parse")
    for index in range(105):
        repo.submit_parse(
            actor,
            independent.kb_id if index % 2 else kb.kb_id,
            str(index) + ".md",
            "a" * 64,
            str(index),
        )
    value = workspace.overview(repo, actor)
    assert value.document_count == value.task_count == 105
    assert value.knowledge_base_count == value.parse_collection_count == 1
    first = workspace.jobs(repo, actor, limit=17)
    second = workspace.jobs(repo, actor, limit=17, offset=17)
    assert first.total == second.total == 105
    assert not {item.job.job_id for item in first.items} & {
        item.job.job_id for item in second.items
    }
    filtered = workspace.jobs(repo, actor, collection_kind="parse", search="独立解析", limit=10)
    assert filtered.total == 52 and all(
        item.kb_name.endswith("独立解析") for item in filtered.items
    )
    caller = Principal(
        tenant_id=actor.tenant_id, subject="scoped", roles=["viewer"], kb_ids=[kb.kb_id]
    )
    assert workspace.overview(repo, caller).task_count == 53
    assert workspace.collections(repo, caller).total == 1
    with pytest.raises(DomainError):
        workspace.jobs(repo, caller, kb_id=independent.kb_id)
    foreign = Principal(
        tenant_id="PM-MIGRATION-alien-" + uuid4().hex, subject="alien", roles=["admin"]
    )
    assert workspace.jobs(repo, foreign).total == 0


def test_only_explicit_selection_is_built_and_edits_do_not_change_selected(
    repo: Repository, actor: Principal
) -> None:
    """解析A/B后仅采用A，构建不得夹带B；A修订也不得暗中改变批准版本。"""
    kb = repo.create_kb(actor, "PM-MIGRATION-明确选用")
    a = parse_fixture(repo, actor, kb.kb_id, "a.md")
    b = parse_fixture(repo, actor, kb.kb_id, "b.md")
    job = repo.accept_job(actor, "ingest", kb.kb_id, a, {}, "only-A", "b" * 64, 1)
    repo.save_revision(actor, a, "b" * 64, "c" * 64, 2, "b" * 64, [])
    # 整体构建新意图只使用保存的选用，不依赖单文档ingest任务的过期触发器。
    candidate = repo.accept_job(actor, "build", kb.kb_id, None, {}, "selected-build")
    lease = repo.claim_job(candidate.job_id, "fixture")
    assert lease
    build = repo.start_build(
        candidate.job_id, "fixture", lease.lease_epoch, "pm-" + uuid4().hex, "fixture-model"
    )
    assert [item.document_id for item in build.manifest] == [a]
    assert build.manifest[0].result_id == "b" * 64 and build.manifest[0].revision == 1
    assert repo.get_document(actor, b).selected_result_id is None
    assert repo.get_document(actor, a).result_id == "c" * 64
    assert repo.get_version(actor, a, "b" * 64).review_status == "pending"
    assert job.expected_result_id == "b" * 64


@pytest.mark.parametrize("source_review", ["pending", "approved"])
def test_independent_parse_adoption_reuses_assets_and_cas_review(
    repo: Repository,
    actor: Principal,
    source_review: Literal["pending", "approved"],
) -> None:
    """复用只建立目标选用，来源两种审核事实均保留，目标独立待审核且资产/属性幂等。"""
    source = repo.create_kb(actor, "PM-MIGRATION-解析", "parse")
    target = repo.create_kb(actor, "PM-MIGRATION-采用目标")
    repo.set_fields(
        actor,
        target.kb_id,
        target.revision,
        [MetadataField(name="region", label="地区", type="text")],
    )
    identity = parse_fixture(repo, actor, source.kb_id, "source.md", asset_ids=["e" * 64])
    with pytest.raises(DomainError):
        repo.accept_job(actor, "build", source.kb_id, None, {}, "bad-build")
    if source_review == "approved":
        reviewer = Principal(
            tenant_id=actor.tenant_id, subject="original-reviewer", roles=["admin"]
        )
        approved = workspace.review_result(repo, reviewer, identity, "b" * 64, 1, "approved")
        assert approved.revision == 2
    source_document = repo.get_document(actor, identity)
    source_version = repo.get_version(actor, identity, "b" * 64)
    assert source_version.review_status == source_review
    with pytest.raises(DomainError):
        workspace.review_result(repo, actor, identity, "b" * 64, 0, "approved")
    adopted = workspace.adopt(
        repo,
        actor,
        target.kb_id,
        AdoptionRequest(source_document_id=identity, result_id="b" * 64, metadata={"region": "CN"}),
        source_asset_id="a" * 64,
    )
    adopted_version = repo.get_version(actor, adopted.document_id, "b" * 64)
    assert adopted_version.review_status == "pending"
    assert adopted_version.reviewed_by is None and adopted_version.reviewed_at is None
    assert adopted_version.asset_ids == source_version.asset_ids == ["a" * 64, "e" * 64]
    assert adopted.metadata == adopted.selected_metadata == {"region": "CN"}
    target_revision = repo.get_kb(actor, target.kb_id).revision
    same = workspace.adopt(
        repo,
        actor,
        target.kb_id,
        AdoptionRequest(source_document_id=identity, result_id="b" * 64, metadata={"region": "CN"}),
        source_asset_id="a" * 64,
    )
    assert same == adopted and repo.get_kb(actor, target.kb_id).revision == target_revision
    assert repo.get_version(actor, adopted.document_id, "b" * 64) == adopted_version
    assert repo.get_version(actor, identity, "b" * 64) == source_version
    assert repo.get_document(actor, identity) == source_document
    target_only = Principal(
        tenant_id=actor.tenant_id, subject="target-reader", roles=["viewer"], kb_ids=[target.kb_id]
    )
    assert repo.has_asset(target_only, "a" * 64) and repo.has_asset(target_only, "e" * 64)
    with pytest.raises(DomainError, match="属性"):
        workspace.adopt(
            repo,
            actor,
            target.kb_id,
            AdoptionRequest(
                source_document_id=identity, result_id="b" * 64, metadata={"region": "US"}
            ),
            source_asset_id="a" * 64,
        )
    assert adopted.source_asset_id == "a" * 64 and adopted.selected_result_id == "b" * 64
    assert workspace.jobs(repo, actor, kb_id=target.kb_id).total == 0
    # 采用后原解析结果revision可能>1；PG历史顺序和产物内部revision独立。
    edited = repo.save_revision(actor, adopted.document_id, "b" * 64, "d" * 64, 5, "b" * 64, [])
    assert edited.revision == 2 and repo.get_version(actor, edited.document_id).revision == 2
    cancelled = workspace.select_result(
        repo,
        actor,
        adopted.document_id,
        SelectionRequest(expected_document_revision=2, result_id=None),
    )
    assert cancelled.selected_result_id is None


def test_service_draft_frozen_in_candidate_and_trace_isolation(
    repo: Repository, actor: Principal
) -> None:
    """候选冻结服务草稿；调用者只可查自己的请求，租户之间trace身份不可通用。"""
    kb = repo.create_kb(actor, "PM-MIGRATION-服务")
    doc = parse_fixture(repo, actor, kb.kb_id, "service.md")
    workspace.select_result(
        repo, actor, doc, SelectionRequest(expected_document_revision=1, result_id="b" * 64)
    )
    draft = workspace.update_service(
        repo,
        actor,
        kb.kb_id,
        ServiceUpdate(expected_revision=0, config=ServiceConfig(mode="keyword", allowed_fields=[])),
    )
    job = repo.accept_job(actor, "build", kb.kb_id, None, {}, "candidate")
    lease = repo.claim_job(job.job_id, "fixture")
    assert lease
    build = repo.start_build(
        job.job_id, "fixture", lease.lease_epoch, "pm-" + uuid4().hex, "fixture-model"
    )
    assert build.service_config["mode"] == "keyword"
    workspace.update_service(
        repo,
        actor,
        kb.kb_id,
        ServiceUpdate(expected_revision=draft.revision, config=ServiceConfig(mode="vector")),
    )
    assert repo.get_build(actor, build.build_id).service_config["mode"] == "keyword"
    trace = workspace.record_trace(
        repo,
        actor,
        kb.kb_id,
        build.build_id,
        "build",
        {"query": "fixture"},
        {"status": "succeeded"},
    )
    other = Principal(tenant_id=actor.tenant_id, subject="another-viewer", roles=["viewer"])
    assert workspace.traces(repo, other, kb.kb_id).total == 0
    with pytest.raises(DomainError):
        workspace.get_trace(repo, other, trace.trace_id)
    assert workspace.get_trace(repo, actor, trace.trace_id).target_id == build.build_id


def test_reparse_commits_source_only_after_fenced_success(
    repo: Repository, actor: Principal
) -> None:
    """替换原件在成功前保持旧原件可读，成功后保留旧结果、源资产和连续历史序号。"""
    kb = repo.create_kb(actor, "PM-MIGRATION-替换")
    doc = parse_fixture(repo, actor, kb.kb_id, "old.md")
    job = repo.submit_reparse(actor, doc, 1, "replace", "e" * 64, "new.md")
    assert repo.get_document(actor, doc).source_asset_id == "a" * 64
    assert repo.submit_reparse(actor, doc, 1, "replace", "e" * 64, "new.md").job_id == job.job_id
    lease = repo.claim_job(job.job_id, "fixture")
    assert lease
    changed = repo.complete_parse(job.job_id, "fixture", lease.lease_epoch, "f" * 64, 1, None, [])
    assert changed.source_asset_id == "e" * 64 and changed.name == "new.md"
    assert repo.get_version(actor, doc).revision == 2
    assert "a" * 64 in repo.get_version(actor, doc, "b" * 64).asset_ids


class FixtureIdentity:
    """领域验证只替换令牌签名边界，不替代真实PG范围检查。"""

    def __init__(self, actor: Principal) -> None:
        """借入独立测试主体。"""
        self.actor = actor

    def verify(self, token: str) -> Principal:
        """输入不用于声明租户或KB。"""
        return self.actor


class FixtureSearch:
    """固定搜索响应只用于验证策略/指标边界，不作为真实检索运行证据。"""

    def __init__(
        self,
        document_id: str,
        before_response: Callable[[], None] | None = None,
        *,
        respect_exclusions: bool = False,
    ) -> None:
        """真实PG资料身份；排除开关可模拟正常适配器或适配器越界。"""
        self.before_response = before_response
        self.document_id = document_id
        self.respect_exclusions = respect_exclusions
        self.excluded: list[str] = []
        self.filters: list[dict[str, JsonValue]] = []

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
        """保留可信排除投影，返回同文档错误元素反例。"""
        self.excluded = excluded_document_ids or []
        self.filters = filters or []
        if self.before_response is not None:
            callback, self.before_response = self.before_response, None
            callback()
        if query == "无答案" or (self.respect_exclusions and self.document_id in self.excluded):
            return {"hits": []}
        return {
            "hits": [
                {
                    "text": "fixture",
                    "score": 1.0,
                    "metadata": {
                        "document_id": self.document_id,
                        "page": 1,
                        "parse_origin": {"result_id": "b" * 64, "element_ref": "#/texts/0"},
                    },
                }
            ]
        }

    def fields_json(
        self, tenant_id: str, knowledge_base_id: str, generation: str
    ) -> dict[str, JsonValue]:
        """此测试不验证真实搜索字段映射。"""
        return {"fields": []}


def test_published_sdk_and_http_enforce_the_same_release_policy(
    repo: Repository, actor: Principal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用同一PG发布和真实调用凭证比较两种接入，隔离搜索及模型基础设施。"""
    kb = repo.create_kb(actor, "Python 与 HTTP 同一发布")
    repo.set_fields(
        actor,
        kb.kb_id,
        kb.revision,
        [MetadataField(name=name, label=name, type="text") for name in ("region", "team")],
    )
    workspace.update_service(
        repo,
        actor,
        kb.kb_id,
        ServiceUpdate(
            expected_revision=0,
            config=ServiceConfig(
                mode="keyword",
                top_k=3,
                allowed_fields=["mode", "filters"],
                allowed_filter_fields=["team"],
                fixed_filters=[Filter(field="region", value="CN")],
            ),
        ),
    )
    doc = parse_fixture(repo, actor, kb.kb_id, "manual.md")
    job = repo.accept_job(actor, "ingest", kb.kb_id, doc, {}, "sdk-build", "b" * 64, 1)
    lease = repo.claim_job(job.job_id, "fixture")
    assert lease
    build = repo.start_build(
        job.job_id, "fixture", lease.lease_epoch, "pm-" + uuid4().hex, "fixture-model"
    )
    repo.complete_build(job.job_id, "fixture", lease.lease_epoch, build.build_id)
    release = repo.publish_build(actor, build.build_id)
    key = repo.create_api_key(actor, kb.kb_id, "sdk-http")
    foreign_kb = repo.create_kb(actor, "凭证范围外的库")
    storage = LocalAssetStore.open(tmp_path / "unused-assets")

    def assets(_actor: Principal) -> AssetStorage:
        """已发布检索只比较命中所携带的原文定位。"""
        return storage

    auth = Authentication(repo, OIDCVerifier("https://identity.invalid", "inkrag-test"))
    search = FixtureSearch(doc, respect_exclusions=True)
    runtime = Runtime(repo, assets, auth, search, {})

    @contextmanager
    def connect(_settings: Settings) -> Generator[Runtime]:
        """两种入口共享同一受限PG连接和确定性搜索适配器。"""
        yield runtime

    monkeypatch.setattr(sdk, "open_runtime", connect)
    settings = Settings.model_construct(capabilities="rag")
    decode = TypeAdapter(dict[str, JsonValue])
    query = "E102"
    fixed: dict[str, JsonValue] = {"field": "region", "operator": "eq", "value": "CN"}
    dynamic: list[dict[str, JsonValue]] = [{"field": "team", "operator": "eq", "value": "研发"}]
    with (
        TestClient(create_app(runtime), headers={"Authorization": "Bearer " + key.token}) as http,
        RAG.open_published(sdk.published_connection(kb.kb_id, key.token, settings=settings)) as rag,
    ):

        def post(**fields: JsonValue) -> dict[str, JsonValue]:
            """只接收成功的实际 HTTP 检索响应。"""
            response = http.post(
                "/api/v2/retrieve", json={"kb_id": kb.kb_id, "query": query, **fields}
            )
            response.raise_for_status()
            return decode.validate_json(response.content)

        def assert_parity(direct: dict[str, JsonValue], response: dict[str, JsonValue]) -> None:
            """每次查询独立记录诊断，其他字段包含相同版本、策略和原文定位。"""
            trace_id = direct.pop("trace_id")
            assert isinstance(trace_id, str) and trace_id != response.pop("trace_id")
            assert workspace.get_trace(repo, actor, trace_id).target_id == release.release_id
            assert direct == response and direct["release_id"] == release.release_id

        default = rag.retrieve(query)
        assert default["effective_request"] == {
            "query": query,
            "mode": "keyword",
            "top_k": 3,
            "filters": [fixed],
        }
        assert_parity(default, post())
        for mode in ("keyword", "vector", "hybrid"):
            result = rag.retrieve(query, mode=mode, filters=dynamic)
            assert result["effective_request"] == {
                "query": query,
                "mode": mode,
                "top_k": 3,
                "filters": [fixed, *dynamic],
            }
            hits = result["hits"]
            assert isinstance(hits, list) and len(hits) == 1
            assert decode.validate_python(hits[0])["metadata"] == {
                "document_id": doc,
                "page": 1,
                "parse_origin": {"result_id": "b" * 64, "element_ref": "#/texts/0"},
            }
            assert_parity(result, post(mode=mode, filters=[*dynamic]))

        with pytest.raises(DomainError) as forbidden:
            rag.retrieve(query, filters=[{"field": "region", "value": "US"}])
        denied = http.post(
            "/api/v2/retrieve",
            json={
                "kb_id": kb.kb_id,
                "query": query,
                "filters": [{"field": "region", "value": "US"}],
            },
        )
        assert forbidden.value.code == "forbidden" and denied.status_code == 403
        with RAG.open_published(
            sdk.published_connection(foreign_kb.kb_id, key.token, settings=settings)
        ) as other:
            with pytest.raises(DomainError) as out_of_scope:
                other.retrieve(query)
        denied = http.post("/api/v2/retrieve", json={"kb_id": foreign_kb.kb_id, "query": query})
        assert out_of_scope.value.code == "not_found" and denied.status_code == 404

        repo.set_document_enabled(actor, doc, False)
        empty = rag.retrieve(query)
        assert search.excluded == [doc] and empty["hits"] == []
        assert_parity(empty, post())
        repo.revoke_api_key(actor, key.key_id)
        with pytest.raises(DomainError) as revoked:
            rag.retrieve(query)
        denied = http.post("/api/v2/retrieve", json={"kb_id": kb.kb_id, "query": query})
        assert revoked.value.code == "unauthorized" and denied.status_code == 401


def test_candidate_quality_evaluation_never_counts_wrong_element_as_correct(
    repo: Repository, actor: Principal, tmp_path: Path
) -> None:
    """同文档错误条款文档Recall可为1，证据Recall必须为0；无答案单独计分。"""
    kb = repo.create_kb(actor, "PM-MIGRATION-证据评测")
    doc = parse_fixture(repo, actor, kb.kb_id, "clauses.md")
    job = repo.accept_job(actor, "ingest", kb.kb_id, doc, {}, "quality-build", "b" * 64, 1)
    lease = repo.claim_job(job.job_id, "fixture")
    assert lease
    build = repo.start_build(
        job.job_id, "fixture", lease.lease_epoch, "pm-" + uuid4().hex, "fixture-model"
    )
    repo.complete_build(job.job_id, "fixture", lease.lease_epoch, build.build_id)
    exact = save_quality_sample(
        repo,
        actor,
        kb.kb_id,
        QualitySampleRequest(
            query="条款",
            expected_sources=[
                ExpectedSource(document_id=doc, result_id="b" * 64, element_ref="#/texts/1")
            ],
        ),
    )
    no_answer = save_quality_sample(
        repo, actor, kb.kb_id, QualitySampleRequest(query="无答案", no_answer=True)
    )
    storage = LocalAssetStore.open(tmp_path / "unused-assets")

    def assets(_actor: Principal) -> AssetStorage:
        """此层用PG夹具，未加载或伪造生产解析字节。"""
        return storage

    search = FixtureSearch(doc)
    runtime = Runtime(repo, assets, FixtureIdentity(actor), search, {})
    evaluation_job = repo.accept_job(
        actor,
        "evaluate",
        kb.kb_id,
        None,
        {
            "build_id": build.build_id,
            "name": "wrong-clause",
            "sample_ids": [exact.sample_id, no_answer.sample_id],
            "top_k": 5,
        },
        "evaluation",
    )
    evaluation_lease = repo.claim_job(evaluation_job.job_id, "evaluator")
    assert evaluation_lease
    report = evaluate_candidate(runtime, evaluation_lease, "evaluator")
    assert report.metrics["recall"] == 1.0 and report.metrics["evidence_recall"] == 0.0
    assert report.metrics["no_answer_accuracy"] == 1.0
    assert repo.get_kb(actor, kb.kb_id).active_release_id is None
    assert repo.get_job(actor, evaluation_job.job_id).status == "succeeded"
    detail = next(item for item in report.details if item["sample_id"] == exact.sample_id)
    assert detail["missing_sources"] and detail["trace_id"]
    # 取消必须在最终提交前生效，已取消任务不能保存第二份成功报告。
    with pytest.raises(DomainError):
        evaluate_candidate(runtime, evaluation_lease, "evaluator")


def test_quality_annotation_validates_native_refs_and_keeps_versions(
    repo: Repository, actor: Principal, tmp_path: Path
) -> None:
    """人工来源需要在真实保存的原生结构里存在，修订标注追加身份而非覆盖报告。"""
    kb = repo.create_kb(actor, "PM-MIGRATION-标注")
    storage = LocalAssetStore.open(tmp_path / "assets")
    source = tmp_path / "clauses.md"
    source.write_text("# 合同\n\n第一条：交付日期。", encoding="utf-8")
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=storage)
    save_stored_result(result, storage)
    assert result.source.asset_id
    job = repo.submit_parse(actor, kb.kb_id, source.name, result.source.asset_id, "parse")
    lease = repo.claim_job(job.job_id, "fixture")
    assert lease and job.document_id
    repo.complete_parse(
        job.job_id,
        "fixture",
        lease.lease_epoch,
        result.result_id,
        result.revision,
        result.previous_result_id,
        [asset.asset_id for asset in result.assets],
    )

    def assets(_actor: Principal) -> AssetStorage:
        """使用完整性验证过的临时真实资产。"""
        return storage

    runtime = Runtime(repo, assets, FixtureIdentity(actor), None, {})
    label = create_quality_sample(
        runtime,
        actor,
        kb.kb_id,
        QualitySampleRequest(
            query="日期？",
            expected_sources=[
                ExpectedSource(
                    document_id=job.document_id, element_ref=result.document.texts[-1].self_ref
                )
            ],
        ),
    )
    assert label.expected_sources[0].result_id == result.result_id
    with pytest.raises(DomainError) as invalid:
        create_quality_sample(
            runtime,
            actor,
            kb.kb_id,
            QualitySampleRequest(
                query="不存在",
                expected_sources=[
                    ExpectedSource(document_id=job.document_id, element_ref="#/texts/999")
                ],
            ),
        )
    assert invalid.value.code == "invalid_request"
    newer = create_quality_sample(
        runtime,
        actor,
        kb.kb_id,
        QualitySampleRequest(query="没有答案", no_answer=True, supersedes_id=label.sample_id),
    )
    assert newer.annotation_revision == 2 and newer.supersedes_id == label.sample_id
    with pytest.raises(DomainError) as stale:
        create_quality_sample(
            runtime,
            actor,
            kb.kb_id,
            QualitySampleRequest(query="过期标注", no_answer=True, supersedes_id=label.sample_id),
        )
    assert stale.value.code == "version_conflict"
    assert len(repo.list_samples(actor, kb.kb_id)) == 2


def test_published_policy_rejects_undeclared_fields_and_trace_reads_are_scoped(
    repo: Repository, actor: Principal, tmp_path: Path
) -> None:
    """已发布配置真实约束调用请求，候选试查无需先发布，错误来源不能泄出。"""
    kb = repo.create_kb(actor, "PM-MIGRATION-调用策略")
    doc = parse_fixture(repo, actor, kb.kb_id, "policy.md")
    workspace.update_service(
        repo,
        actor,
        kb.kb_id,
        ServiceUpdate(expected_revision=0, config=ServiceConfig(mode="keyword", allowed_fields=[])),
    )
    job = repo.accept_job(actor, "ingest", kb.kb_id, doc, {}, "policy-build", "b" * 64, 1)
    lease = repo.claim_job(job.job_id, "fixture")
    assert lease
    build = repo.start_build(
        job.job_id, "fixture", lease.lease_epoch, "pm-" + uuid4().hex, "fixture-model"
    )
    repo.complete_build(job.job_id, "fixture", lease.lease_epoch, build.build_id)
    storage = LocalAssetStore.open(tmp_path / "unused-assets")

    def assets(_actor: Principal) -> AssetStorage:
        """请求策略测试无需资产读取。"""
        return storage

    search = FixtureSearch(doc)
    runtime = Runtime(repo, assets, FixtureIdentity(actor), search, {})
    candidate = query_target(runtime, actor, build.build_id, TrialRequest(query="policy"))
    assert candidate["target_kind"] == "build" and candidate["trace_id"]
    release = repo.publish_build(actor, build.build_id)
    assert (
        repo.publish_build(actor, build.build_id, release.release_id).release_id
        == release.release_id
    )
    with pytest.raises(DomainError) as denied:
        query_target(
            runtime, actor, kb.kb_id, TrialRequest(query="policy", top_k=10), candidate=False
        )
    assert denied.value.code == "forbidden"
    allowed = query_target(runtime, actor, kb.kb_id, TrialRequest(query="policy"), candidate=False)
    assert allowed["target_kind"] == "release"
    assert (
        workspace.get_trace(repo, actor, str(allowed["trace_id"])).target_id == release.release_id
    )

    def disable_during_query() -> None:
        """在检索适配器返回前提交真实禁用事务，重现并发状态变化。"""
        repo.set_document_enabled(actor, doc, False)

    for candidate, target in ((True, build.build_id), (False, kb.kb_id)):
        search.before_response = disable_during_query
        with pytest.raises(DomainError) as raced:
            query_target(runtime, actor, target, TrialRequest(query="policy"), candidate=candidate)
        assert raced.value.code == "authorization_pending"
        # 不遵守排除约定的适配器也无法返回被禁用内容。
        with pytest.raises(DomainError) as disabled:
            query_target(runtime, actor, target, TrialRequest(query="policy"), candidate=candidate)
        assert disabled.value.code == "scope_violation"
        repo.set_document_enabled(actor, doc, True)
    old_epoch = repo.get_kb(actor, kb.kb_id).retrieval_epoch
    repo.set_document_enabled(actor, doc, False)
    with pytest.raises(DomainError) as stale_evaluation:
        repo.record_evaluation(actor, kb.kb_id, release.release_id, "stale", {}, old_epoch)
    assert stale_evaluation.value.code == "version_conflict"
    assert repo.list_evaluations(actor, kb.kb_id) == []
    repo.set_document_enabled(actor, doc, True)
    current = repo.get_document(actor, doc)
    repo.delete_document(actor, doc, current.revision)
    # 即使注入搜索实现违反可信排除约定，领域边界也不能返回被撤销资料。
    with pytest.raises(DomainError) as revoked:
        query_target(runtime, actor, kb.kb_id, TrialRequest(query="policy"), candidate=False)
    assert revoked.value.code == "scope_violation" and search.excluded == [doc]


def test_real_search_selection_and_deleted_projection_all_channels(
    repo: Repository, actor: Principal, tmp_path: Path
) -> None:
    """真实PG+OS验证三通道范围、同次诊断持久化与权限；不补查或伪造解释。"""
    if os.environ.get("INKRAG_SEARCH_INTEGRATION") != "1":
        pytest.skip("需要显式启用真实OpenSearch测试地址")
    from inkrag_platform.models import Build, MetadataField
    from inkrag_platform.workspace_models import Filter
    from llama_index.core.embeddings import MockEmbedding
    from opensearchpy import OpenSearch

    from inkrag.search import SearchBackend

    client = OpenSearch([os.environ["INKRAG_OPENSEARCH_TEST_URL"]], timeout=30, max_retries=1)
    # 固定向量只隔离模型变量；所有索引、ANN/关键词/RRF和过滤都由真实OpenSearch执行。
    search = SearchBackend(
        client,
        MockEmbedding(embed_dim=4),
        model_revision="pm-native-search4",
        dimensions=4,
        replicas=0,
    )
    kb = repo.create_kb(actor, "PM-MIGRATION-真实检索")
    repo.set_fields(
        actor,
        kb.kb_id,
        kb.revision,
        [MetadataField(name="region", label="区域", type="enum", options=["CN", "US"])],
    )
    storage = LocalAssetStore.open(tmp_path / "native-assets")
    results: dict[str, ParseResult] = {}
    identities: list[str] = []
    for filename, text, region in (
        ("A.md", "# 保修\n\n保修 APPROVEDALPHA 十二个月。", "CN"),
        ("B.md", "# 保修\n\n保修 UNREVIEWEDBETA 二十四个月。", "US"),
    ):
        path = tmp_path / filename
        path.write_text(text, encoding="utf-8")
        result = Parser().parse(ParseRequest(source=FileSource(path=str(path))), assets=storage)
        save_stored_result(result, storage)
        assert result.source.asset_id
        job = repo.submit_parse(
            actor, kb.kb_id, filename, result.source.asset_id, filename, {"region": region}
        )
        lease = repo.claim_job(job.job_id, "fixture")
        assert lease and job.document_id
        repo.complete_parse(
            job.job_id,
            "fixture",
            lease.lease_epoch,
            result.result_id,
            result.revision,
            result.previous_result_id,
            [asset.asset_id for asset in result.assets],
        )
        identities.append(job.document_id)
        results[job.document_id] = result
    a, b = identities
    generations: list[str] = []

    def assets(_actor: Principal) -> AssetStorage:
        """真实原件及解析结果保存在本次临时资产空间。"""
        return storage

    runtime = Runtime(repo, assets, FixtureIdentity(actor), search, {})

    def build_native(key: str) -> Build:
        """显式串联真实冻结清单和原生索引，容器Worker贯穿由另一层验收。"""
        job = repo.accept_job(actor, "build", kb.kb_id, None, {}, key)
        lease = repo.claim_job(job.job_id, "native-builder")
        assert lease
        build_id = uuid4().hex
        generation = search.generation_name(actor.tenant_id, kb.kb_id, build_id)
        build = repo.start_build(
            job.job_id,
            "native-builder",
            lease.lease_epoch,
            generation,
            search.model_revision,
            build_id,
        )
        actual = search.create_build(
            actor.tenant_id, kb.kb_id, build_id, field_schema=build.field_schema
        )
        generations.append(actual)
        assert actual == generation
        for item in build.manifest:
            assert results[item.document_id].result_id == item.result_id
            search.upsert_result(
                actor.tenant_id,
                kb.kb_id,
                generation,
                item.document_id,
                results[item.document_id],
                {"region": "CN" if item.document_id == a else "US"},
            )
        frozen = search.freeze(actor.tenant_id, kb.kb_id, generation)
        assert frozen.document_count == len(build.manifest)
        return repo.complete_build(job.job_id, "native-builder", lease.lease_epoch, build.build_id)

    def ids(response: dict[str, JsonValue]) -> set[str]:
        """使用真实检索返回身份验证，不按正文关键词猜测来源。"""
        hits = response["hits"]
        assert isinstance(hits, list)
        found: set[str] = set()
        for hit in hits:
            assert isinstance(hit, dict) and isinstance(hit["metadata"], dict)
            identity = hit["metadata"]["document_id"]
            assert isinstance(identity, str)
            found.add(identity)
        return found

    def assert_native_trace(response: dict[str, JsonValue], expected: set[str]) -> None:
        """原生同次解释与PG保存完全相同，阶段未知计数不伪造为零。"""
        assert ids(response) == expected
        trace_id = response["trace_id"]
        assert isinstance(trace_id, str)
        diagnostic = response.get("diagnostics")
        assert isinstance(diagnostic, dict)
        assert diagnostic["kind"] == "opensearch_explain" and diagnostic["same_response"] is True
        assert diagnostic["pre_filter_candidate_count"] is None
        assert diagnostic["channel_candidate_totals"] is None
        explanations = diagnostic["hits"]
        assert isinstance(explanations, list) and explanations
        assert diagnostic["returned_hit_count"] == len(explanations)
        for explanation in explanations:
            assert isinstance(explanation, dict)
            assert explanation["document_id"] in expected and explanation["status"] == "captured"
            channels = explanation["channels"]
            assert isinstance(channels, list) and channels
            names: set[str] = set()
            for channel in channels:
                assert isinstance(channel, dict) and isinstance(channel["channel"], str)
                names.add(channel["channel"])
            assert names == {"keyword", "vector"}
        persisted = workspace.get_trace(repo, actor, trace_id)
        assert persisted.outcome["diagnostics"] == diagnostic
        assert persisted.outcome["document_ids"] == list(expected)

    try:
        workspace.select_result(
            repo,
            actor,
            a,
            SelectionRequest(expected_document_revision=1, result_id=results[a].result_id),
        )
        only_a = build_native("only-A")
        assert [item.document_id for item in only_a.manifest] == [a]
        detailed = TrialRequest(query="保修", diagnostics=True)
        assert_native_trace(query_target(runtime, actor, only_a.build_id, detailed), {a})
        viewer = Principal(tenant_id=actor.tenant_id, subject="viewer", roles=["viewer"])
        with pytest.raises(DomainError) as viewer_denied:
            query_target(runtime, viewer, only_a.build_id, detailed)
        assert viewer_denied.value.code == "forbidden"
        foreign = Principal(
            tenant_id="foreign-" + actor.tenant_id, subject="foreign", roles=["admin"]
        )
        with pytest.raises(DomainError) as foreign_denied:
            query_target(runtime, foreign, only_a.build_id, detailed)
        assert foreign_denied.value.code in {"forbidden", "not_found"}
        first = repo.publish_build(actor, only_a.build_id)
        with pytest.raises(DomainError) as published_denied:
            query_target(runtime, actor, kb.kb_id, detailed, candidate=False)
        assert published_denied.value.code == "forbidden"
        for mode in ("keyword", "vector", "hybrid"):
            assert ids(
                query_target(
                    runtime, actor, kb.kb_id, TrialRequest(query="保修", mode=mode), candidate=False
                )
            ) == {a}
        assert not ids(
            query_target(
                runtime,
                actor,
                kb.kb_id,
                TrialRequest(query="UNREVIEWEDBETA", mode="keyword"),
                candidate=False,
            )
        )
        workspace.select_result(
            repo,
            actor,
            b,
            SelectionRequest(expected_document_revision=1, result_id=results[b].result_id),
        )
        both = build_native("A-and-B")
        assert {item.document_id for item in both.manifest} == {a, b}
        repo.publish_build(actor, both.build_id, first.release_id)
        for mode in ("keyword", "vector", "hybrid"):
            assert ids(
                query_target(
                    runtime, actor, kb.kb_id, TrialRequest(query="保修", mode=mode), candidate=False
                )
            ) == {a, b}
        original_b = repo.get_document(actor, b)
        original_kb = repo.get_kb(actor, kb.kb_id)
        repo.set_document_enabled(actor, b, False)
        repo.set_document_enabled(actor, b, False)
        disabled_kb = repo.get_kb(actor, kb.kb_id)
        assert disabled_kb.retrieval_epoch == original_kb.retrieval_epoch + 1
        assert disabled_kb.authz_epoch == original_kb.authz_epoch
        assert repo.get_document(actor, b).revision == original_b.revision
        for mode in ("keyword", "vector", "hybrid"):
            for candidate, target in ((False, kb.kb_id), (True, both.build_id)):
                assert ids(
                    query_target(
                        runtime,
                        actor,
                        target,
                        TrialRequest(query="保修", mode=mode, top_k=1),
                        candidate=candidate,
                    )
                ) == {a}
        # 新构建和发布不能绕过实时开关；保留索引内容使重新启用无需重新解析。
        rebuilt = build_native("disabled-B-rebuild")
        active = repo.get_active_release(actor, kb.kb_id)
        repo.publish_build(actor, rebuilt.build_id, active.release_id)
        assert ids(
            query_target(runtime, actor, kb.kb_id, TrialRequest(query="保修"), candidate=False)
        ) == {a}
        repo.set_document_enabled(actor, b, True)
        assert ids(
            query_target(runtime, actor, kb.kb_id, TrialRequest(query="保修"), candidate=False)
        ) == {a, b}
        assert repo.get_document(actor, b).result_id == original_b.result_id
        current_b = repo.get_document(actor, b)
        repo.delete_document(actor, b, current_b.revision)
        assert_native_trace(query_target(runtime, actor, both.build_id, detailed), {a})
        for mode in ("keyword", "vector", "hybrid"):
            assert ids(
                query_target(
                    runtime, actor, kb.kb_id, TrialRequest(query="保修", mode=mode), candidate=False
                )
            ) == {a}
            assert ids(
                query_target(
                    runtime,
                    actor,
                    kb.kb_id,
                    TrialRequest(
                        query="保修", mode=mode, filters=[Filter(field="region", value="CN")]
                    ),
                    candidate=False,
                )
            ) == {a}
            assert not ids(
                query_target(
                    runtime,
                    actor,
                    kb.kb_id,
                    TrialRequest(
                        query="保修", mode=mode, filters=[Filter(field="region", value="US")]
                    ),
                    candidate=False,
                )
            )
        print(
            "真实PG+OS：只选A、撤销B保留A、三通道过滤；同次RRF解释与PG trace严格等值、"
            "仅A有通道事实，viewer/跨租户/发布诊断拒绝，全部通过"
        )
    finally:
        for generation in generations:
            client.transport.perform_request("DELETE", "/" + generation)
        client.close()


def test_deleted_projection_is_bounded_by_current_manifest(
    repo: Repository, actor: Principal
) -> None:
    """大量无关历史删除不进入本次检索投影，空清单仍返回同一授权epoch。"""
    from inkrag_platform.database import DocumentRow

    kb = repo.create_kb(actor, "PM-MIGRATION-撤销投影预算")
    live = parse_fixture(repo, actor, kb.kb_id, "live.md")
    revoked = parse_fixture(repo, actor, kb.kb_id, "revoked.md")
    repo.delete_document(actor, revoked, 1)
    # 独立测试租户的历史墓碑夹具，不逐份创建无关任务/资产，也不触碰旧资料。
    historical = [
        DocumentRow(
            id=uuid4().hex,
            tenant_id=actor.tenant_id,
            kb_id=kb.kb_id,
            name="historical-" + str(index),
            source_asset_id="a" * 64,
            deleted=True,
        )
        for index in range(2000)
    ]
    with repo.transaction() as session:
        session.add_all(historical)
    current_epoch = repo.get_kb(actor, kb.kb_id).authz_epoch
    epoch, identities = repo.deleted_document_ids(actor, kb.kb_id, [live, revoked])
    assert epoch == current_epoch and identities == [revoked]
    assert repo.deleted_document_ids(actor, kb.kb_id, [live]) == (current_epoch, [])
    assert repo.deleted_document_ids(actor, kb.kb_id, []) == (current_epoch, [])
    with pytest.raises(DomainError) as unbounded:
        repo.deleted_document_ids(actor, kb.kb_id, [live] * 5001)
    assert unbounded.value.code == "build_budget_exceeded"
    outsider = Principal(
        tenant_id="PM-MIGRATION-foreign-" + uuid4().hex, subject="outsider", roles=["admin"]
    )
    with pytest.raises(DomainError) as denied:
        repo.deleted_document_ids(outsider, kb.kb_id, [])
    assert denied.value.code == "not_found"


def test_release_identity_is_fixed_when_publication_changes_during_query(
    repo: Repository, actor: Principal, tmp_path: Path
) -> None:
    """查询A期间切换到B，响应/trace仍标记命中所用A，不能事后借新发布身份贴标。"""
    kb = repo.create_kb(actor, "PM-MIGRATION-查询发布快照")
    document_id = parse_fixture(repo, actor, kb.kb_id, "release.md")
    first_job = repo.accept_job(
        actor, "ingest", kb.kb_id, document_id, {}, "release-A", "b" * 64, 1
    )
    first_lease = repo.claim_job(first_job.job_id, "fixture")
    assert first_lease
    a = repo.start_build(
        first_job.job_id, "fixture", first_lease.lease_epoch, "pm-" + uuid4().hex, "fixture-A"
    )
    repo.complete_build(first_job.job_id, "fixture", first_lease.lease_epoch, a.build_id)
    release_a = repo.publish_build(actor, a.build_id)
    edited = repo.save_revision(actor, document_id, "b" * 64, "c" * 64, 2, "b" * 64, [])
    workspace.select_result(
        repo,
        actor,
        document_id,
        SelectionRequest(expected_document_revision=edited.revision, result_id=edited.result_id),
    )
    second_job = repo.accept_job(actor, "build", kb.kb_id, None, {}, "release-B")
    second_lease = repo.claim_job(second_job.job_id, "fixture")
    assert second_lease
    b = repo.start_build(
        second_job.job_id, "fixture", second_lease.lease_epoch, "pm-" + uuid4().hex, "fixture-B"
    )
    repo.complete_build(second_job.job_id, "fixture", second_lease.lease_epoch, b.build_id)
    storage = LocalAssetStore.open(tmp_path / "unused-assets")

    def assets(_actor: Principal) -> AssetStorage:
        """发布快照反例不读取或伪造源文件。"""
        return storage

    def switch_publication() -> None:
        """在查询已固定A、搜索结果尚未返回的可重复时序点切换到B。"""
        repo.publish_build(actor, b.build_id, release_a.release_id)

    runtime = Runtime(
        repo, assets, FixtureIdentity(actor), FixtureSearch(document_id, switch_publication), {}
    )
    result = query_target(runtime, actor, kb.kb_id, TrialRequest(query="release"), candidate=False)
    release_b = repo.get_active_release(actor, kb.kb_id)
    assert release_b.build_id == b.build_id and release_b.version == release_a.version + 1
    assert result["release_id"] == result["target_id"] == release_a.release_id
    assert result["release_version"] == release_a.version
    assert result["build_id"] == a.build_id and result["snapshot_id"] == a.index_name
    trace = workspace.get_trace(repo, actor, str(result["trace_id"]))
    assert trace.target_id == release_a.release_id and trace.outcome["index_name"] == a.index_name


def test_service_dynamic_field_whitelist_keeps_fixed_scope_and_schema_valid(
    repo: Repository, actor: Principal, tmp_path: Path
) -> None:
    """动态字段白名单不替代固定范围；关闭/未开放字段被拒绝，schema不得破坏保存策略。"""
    from inkrag_platform.models import MetadataField
    from inkrag_platform.workspace_models import Filter

    kb = repo.create_kb(actor, "PM-MIGRATION-动态字段白名单")
    definitions = [
        MetadataField(name="region", label="区域", type="text"),
        MetadataField(name="team", label="部门", type="text"),
    ]
    repo.set_fields(actor, kb.kb_id, kb.revision, definitions)
    document_id = parse_fixture(repo, actor, kb.kb_id, "filter-policy.md")
    draft = workspace.update_service(
        repo,
        actor,
        kb.kb_id,
        ServiceUpdate(
            expected_revision=0,
            config=ServiceConfig(
                allowed_fields=["filters"],
                allowed_filter_fields=["team"],
                fixed_filters=[Filter(field="region", value="CN")],
                mode="keyword",
            ),
        ),
    )
    job = repo.accept_job(
        actor, "ingest", kb.kb_id, document_id, {}, "field-whitelist", "b" * 64, 1
    )
    lease = repo.claim_job(job.job_id, "fixture")
    assert lease
    build = repo.start_build(
        job.job_id, "fixture", lease.lease_epoch, "pm-" + uuid4().hex, "fixture-model"
    )
    repo.complete_build(job.job_id, "fixture", lease.lease_epoch, build.build_id)
    storage = LocalAssetStore.open(tmp_path / "unused-assets")

    def assets(_actor: Principal) -> AssetStorage:
        """此反例只验证真实PG冻结策略及传入搜索端口的有效条件。"""
        return storage

    search = FixtureSearch(document_id)
    runtime = Runtime(repo, assets, FixtureIdentity(actor), search, {})
    query_target(
        runtime,
        actor,
        build.build_id,
        TrialRequest(query="policy", filters=[Filter(field="team", value="研发")]),
    )
    assert search.filters == [
        {"field": "region", "operator": "eq", "value": "CN"},
        {"field": "team", "operator": "eq", "value": "研发"},
    ]
    with pytest.raises(DomainError) as denied:
        query_target(
            runtime,
            actor,
            build.build_id,
            TrialRequest(query="policy", filters=[Filter(field="region", value="US")]),
        )
    assert denied.value.code == "forbidden"
    with pytest.raises(DomainError) as unknown:
        workspace.update_service(
            repo,
            actor,
            kb.kb_id,
            ServiceUpdate(
                expected_revision=draft.revision,
                config=ServiceConfig(allowed_filter_fields=["missing"]),
            ),
        )
    assert unknown.value.code == "invalid_filter"
    with pytest.raises(DomainError) as schema:
        repo.set_fields(actor, kb.kb_id, repo.get_kb(actor, kb.kb_id).revision, definitions[:1])
    assert schema.value.code == "invalid_filter"
    with pytest.raises(ValueError):
        ServiceConfig(allowed_filter_fields=["team", "team"])
    # 空白名单确实关闭所有动态字段；保存后旧候选仍冻结此前允许team的配置。
    workspace.update_service(
        repo,
        actor,
        kb.kb_id,
        ServiceUpdate(
            expected_revision=draft.revision,
            config=ServiceConfig(
                allowed_fields=["filters"],
                allowed_filter_fields=[],
                fixed_filters=[Filter(field="region", value="CN")],
            ),
        ),
    )
    assert repo.get_build(actor, build.build_id).service_config["allowed_filter_fields"] == ["team"]
    closed_job = repo.accept_job(actor, "build", kb.kb_id, None, {}, "closed-dynamic-fields")
    closed_lease = repo.claim_job(closed_job.job_id, "fixture")
    assert closed_lease
    closed = repo.start_build(
        closed_job.job_id, "fixture", closed_lease.lease_epoch, "pm-" + uuid4().hex, "fixture-model"
    )
    repo.complete_build(closed_job.job_id, "fixture", closed_lease.lease_epoch, closed.build_id)
    with pytest.raises(DomainError) as closed_field:
        query_target(
            runtime,
            actor,
            closed.build_id,
            TrialRequest(query="policy", filters=[Filter(field="team", value="研发")]),
        )
    assert closed_field.value.code == "forbidden"
    query_target(runtime, actor, closed.build_id, TrialRequest(query="policy"))
    assert search.filters == [{"field": "region", "operator": "eq", "value": "CN"}]


def test_publication_preview_frozen_diff_pagination_and_expected_release(
    repo: Repository, actor: Principal
) -> None:
    """发布审阅精确区分新增/更新/移除/不变，分页总数稳定且版本来自冻结清单。"""
    from inkrag_platform.workspace_publication import publication_preview

    kb = repo.create_kb(actor, "PM-MIGRATION-发布审阅")
    a, b, c = [parse_fixture(repo, actor, kb.kb_id, name + ".md") for name in ("A", "B", "C")]
    for identity in (a, b, c):
        workspace.select_result(
            repo,
            actor,
            identity,
            SelectionRequest(expected_document_revision=1, result_id="b" * 64),
        )

    def build(key: str) -> str:
        """只验证PG冻结差异，索引贯穿已有独立原生验证。"""
        job = repo.accept_job(actor, "build", kb.kb_id, None, {}, key)
        lease = repo.claim_job(job.job_id, "fixture")
        assert lease
        candidate = repo.start_build(
            job.job_id, "fixture", lease.lease_epoch, "pm-" + uuid4().hex, "same-model"
        )
        repo.complete_build(job.job_id, "fixture", lease.lease_epoch, candidate.build_id)
        return candidate.build_id

    initial = build("initial")
    first = repo.publish_build(actor, initial)
    same = publication_preview(repo, actor, initial)
    assert same.summary.unchanged == 3 and not same.strategy_changed
    current_b = repo.get_document(actor, b)
    edited_b = repo.save_revision(actor, b, "b" * 64, "c" * 64, 2, "b" * 64, [])
    selected_b = workspace.select_result(
        repo,
        actor,
        b,
        SelectionRequest(
            expected_document_revision=edited_b.revision, result_id=edited_b.result_id
        ),
    )
    assert selected_b.revision > current_b.revision
    current_c = repo.get_document(actor, c)
    workspace.select_result(
        repo,
        actor,
        c,
        SelectionRequest(expected_document_revision=current_c.revision, result_id=None),
    )
    d = parse_fixture(repo, actor, kb.kb_id, "D.md")
    workspace.select_result(
        repo, actor, d, SelectionRequest(expected_document_revision=1, result_id="b" * 64)
    )
    workspace.update_service(
        repo, actor, kb.kb_id, ServiceUpdate(expected_revision=0, config=ServiceConfig(top_k=7))
    )
    candidate = build("changed")
    page = publication_preview(repo, actor, candidate, limit=2)
    rest = publication_preview(repo, actor, candidate, limit=2, offset=2)
    assert page.expected_release_id == first.release_id and page.publishable
    assert page.summary.model_dump() == {"added": 1, "changed": 1, "removed": 1, "unchanged": 1}
    assert page.total == rest.total == 4 and page.strategy_changed
    assert [(item.document_id, item.change) for item in page.items] == [
        (d, "added"),
        (b, "changed"),
    ]
    assert [(item.document_id, item.change) for item in rest.items] == [
        (c, "removed"),
        (a, "unchanged"),
    ]
    assert rest.items[0].revision == current_c.revision
    assert page.items[1].result_id == "c" * 64 and page.items[1].revision == selected_b.revision
    assert page.service_config.top_k == 7
    repo.publish_build(actor, candidate, page.expected_release_id)
    with pytest.raises(DomainError) as stale:
        repo.publish_build(actor, candidate, page.expected_release_id)
    assert stale.value.code == "version_conflict"
    outsider = Principal(
        tenant_id="PM-MIGRATION-foreign-" + uuid4().hex, subject="outsider", roles=["admin"]
    )
    with pytest.raises(DomainError):
        publication_preview(repo, outsider, candidate)


def test_audit_scope_filters_non_document_targets_and_global_events(
    repo: Repository, actor: Principal
) -> None:
    """本库审计真正按持久归属筛选，job/key/sample等目标和全租户事件不会混库。"""
    first = repo.create_kb(actor, "PM-MIGRATION-审计A")
    second = repo.create_kb(actor, "PM-MIGRATION-审计B")
    a = parse_fixture(repo, actor, first.kb_id, "A.md")
    parse_fixture(repo, actor, second.kb_id, "B.md")
    first_key = repo.create_api_key(actor, first.kb_id, "A-key")
    second_key = repo.create_api_key(actor, second.kb_id, "B-key")
    sample = repo.create_sample(actor, first.kb_id, "question", [a])
    workspace.review_result(repo, actor, a, "b" * 64, 1, "approved")
    with repo.transaction() as session:
        repo.audit_in_transaction(session, actor, "tenant.policy", actor.tenant_id)
    only_a = repo.list_audit(actor, first.kb_id)
    assert only_a and all(item.kb_id == first.kb_id for item in only_a)
    targets = {item.target_id for item in only_a}
    assert first_key.key_id in targets and sample.sample_id in targets
    assert second_key.key_id not in targets and actor.tenant_id not in targets
    assert any(item.action == "job.accept" for item in only_a)
    assert any(item.action == "result.review:approved" for item in only_a)
    all_events = repo.list_audit(actor)
    assert any(item.target_id == actor.tenant_id and item.kb_id is None for item in all_events)
    scoped = Principal(
        tenant_id=actor.tenant_id, subject=actor.subject, roles=["admin"], kb_ids=[first.kb_id]
    )
    assert all(item.kb_id == first.kb_id for item in repo.list_audit(scoped))


def test_exact_document_job_filter_survives_many_identical_filenames(
    repo: Repository, actor: Principal
) -> None:
    """超过首30份同名资料仍准确取得指定资料的失败诊断，计数与分页不混同名任务。"""
    kb = repo.create_kb(actor, "PM-MIGRATION-同名资料恢复")
    target = repo.submit_parse(actor, kb.kb_id, "same-name.md", "a" * 64, "target")
    lease = repo.claim_job(target.job_id, "fixture")
    assert lease and target.document_id
    repo.fail_job(
        target.job_id,
        "fixture",
        lease.lease_epoch,
        "invalid_document",
        diagnostic={"message": "目标原件损坏", "retryable": False},
    )
    for index in range(35):
        repo.submit_parse(actor, kb.kb_id, "same-name.md", "a" * 64, "same-" + str(index))
    first_page = workspace.jobs(repo, actor, kb_id=kb.kb_id, search="same-name.md", limit=30)
    assert first_page.total == 36
    assert all(item.job.document_id != target.document_id for item in first_page.items)
    exact = workspace.jobs(repo, actor, kb_id=kb.kb_id, document_id=target.document_id)
    assert exact.total == 1 and len(exact.items) == 1
    assert exact.items[0].job.job_id == target.job_id
    assert exact.items[0].job.error_diagnostic == {"message": "目标原件损坏", "retryable": False}
    assert (
        workspace.jobs(repo, actor, document_id=target.document_id, limit=1, offset=1).items == []
    )
    assert workspace.jobs(repo, actor, document_id=target.document_id, status="queued").total == 0
    outsider = Principal(
        tenant_id="PM-MIGRATION-foreign-" + uuid4().hex, subject="outsider", roles=["admin"]
    )
    assert workspace.jobs(repo, outsider, document_id=target.document_id).total == 0
    with pytest.raises(DomainError) as invalid:
        workspace.jobs(repo, actor, document_id="invalid")
    assert invalid.value.code == "invalid_request"


@pytest.mark.parametrize("selection_path", ["workspace", "legacy_ingest"])
@pytest.mark.parametrize("initial_review", ["pending", "approved"])
def test_selection_preserves_manual_review_facts_and_metadata_updates(
    repo: Repository,
    actor: Principal,
    selection_path: Literal["workspace", "legacy_ingest"],
    initial_review: Literal["pending", "approved"],
) -> None:
    """新旧选用入口均不冒充审核，重复及更新属性快照时保留原审核人和时间。"""
    kb = repo.create_kb(actor, "PM-MIGRATION-选用不审核")
    repo.set_fields(
        actor, kb.kb_id, kb.revision, [MetadataField(name="region", label="地区", type="text")]
    )
    identity = parse_fixture(repo, actor, kb.kb_id, "selection.md")
    if initial_review == "approved":
        reviewer = Principal(
            tenant_id=actor.tenant_id, subject="original-reviewer", roles=["admin"]
        )
        workspace.review_result(repo, reviewer, identity, "b" * 64, 1, "approved")
    original_version = repo.get_version(actor, identity)
    assert original_version.review_status == initial_review

    def select_current(key: str, revision: int) -> Document:
        """两种业务入口使用同一份真实PG资料，不替换审核或仓库实现。"""
        if selection_path == "workspace":
            return workspace.select_result(
                repo,
                actor,
                identity,
                SelectionRequest(expected_document_revision=revision, result_id="b" * 64),
            )
        repo.accept_job(actor, "ingest", kb.kb_id, identity, {}, key, "b" * 64, revision)
        return repo.get_document(actor, identity)

    first_input = repo.get_document(actor, identity)
    selected = select_current("initial-selection", first_input.revision)
    assert selected.selected_result_id == "b" * 64 and selected.selected_metadata == {}
    assert repo.get_version(actor, identity) == original_version
    current_kb_revision = repo.get_kb(actor, kb.kb_id).revision
    replay = select_current(
        "initial-selection",
        first_input.revision if selection_path == "legacy_ingest" else selected.revision,
    )
    assert replay == selected
    assert repo.get_kb(actor, kb.kb_id).revision == current_kb_revision
    assert repo.get_version(actor, identity) == original_version
    updated = repo.update_metadata(actor, identity, selected.revision, {"region": "CN"})
    assert updated.selected_metadata == {}
    selected_again = select_current("metadata-selection", updated.revision)
    assert selected_again.selected_metadata == {"region": "CN"}
    assert repo.get_version(actor, identity) == original_version
    if initial_review == "pending":
        assert original_version.reviewed_by is None and original_version.reviewed_at is None
    else:
        assert (
            original_version.reviewed_by == "original-reviewer"
            and original_version.reviewed_at is not None
        )


def test_incremental_update_retains_deferred_online_version(
    repo: Repository, actor: Principal
) -> None:
    """默认更新沿用线上基线，暂缓保旧、审核与选择原子提交，冲突要求重做审阅。"""
    from inkrag_platform.workspace_publication import publication_preview

    kb = repo.create_kb(actor, "增量更新")
    retained, changed = [parse_fixture(repo, actor, kb.kb_id, name) for name in ("A.txt", "B.txt")]
    for identity in (retained, changed):
        workspace.select_result(
            repo,
            actor,
            identity,
            SelectionRequest(expected_document_revision=1, result_id="b" * 64),
        )

    def build(key: str, *, incremental: bool = False) -> str:
        """按真实持久任务生成候选，测试仅省略搜索物理写入。"""
        payload: dict[str, JsonValue] = {"incremental": True} if incremental else {}
        job = repo.accept_job(actor, "build", kb.kb_id, None, payload, key)
        lease = repo.claim_job(job.job_id, "fixture")
        assert lease
        candidate = repo.start_build(job.job_id, "fixture", lease.lease_epoch, uuid4().hex, "model")
        repo.complete_build(job.job_id, "fixture", lease.lease_epoch, candidate.build_id)
        return candidate.build_id

    first = repo.publish_build(actor, build("initial-incremental"))
    old = repo.get_document(actor, retained)
    workspace.select_result(
        repo, actor, retained, SelectionRequest(expected_document_revision=old.revision)
    )
    edited = repo.save_revision(actor, changed, "b" * 64, "c" * 64, 2, "b" * 64, [])
    workspace.select_result(
        repo,
        actor,
        changed,
        SelectionRequest(
            expected_document_revision=edited.revision,
            result_id=edited.result_id,
            confirm_content=True,
        ),
    )
    workspace.update_service(
        repo, actor, kb.kb_id, ServiceUpdate(expected_revision=0, config=ServiceConfig(top_k=7))
    )
    candidate = build("incremental-change", incremental=True)
    competing = build("incremental-competitor", incremental=True)
    preview = publication_preview(repo, actor, candidate)
    assert preview.publishable and not preview.strategy_changed
    assert preview.expected_release_id == first.release_id
    assert preview.summary.model_dump() == {"added": 0, "changed": 1, "removed": 0, "unchanged": 1}
    assert {item.document_id: item.result_id for item in preview.items} == {
        retained: "b" * 64,
        changed: "c" * 64,
    }
    published = repo.publish_build(actor, candidate, first.release_id)
    assert repo.publish_build(actor, candidate, first.release_id) == published
    stale = publication_preview(repo, actor, competing)
    assert not stale.publishable and stale.expected_release_id == first.release_id
    with pytest.raises(DomainError, match="线上发布已变化"):
        repo.publish_build(actor, competing, published.release_id)


def test_document_catalog_filters_before_pagination(repo: Repository, actor: Principal) -> None:
    """同一快照按资料实际状态筛选，替换失败不能掩盖当前线上版本。"""
    kb = repo.create_kb(actor, "资料列表")
    review_id = parse_fixture(repo, actor, kb.kb_id, "退款政策.md")
    ready_id = parse_fixture(repo, actor, kb.kb_id, "保修手册.md")
    workspace.select_result(
        repo, actor, ready_id, SelectionRequest(expected_document_revision=1, result_id="b" * 64)
    )
    pending = repo.submit_parse(actor, kb.kb_id, "配送说明.md", "a" * 64, "catalog-queued")
    broken = repo.submit_parse(actor, kb.kb_id, "退款附件.md", "a" * 64, "catalog-failed")
    lease = repo.claim_job(broken.job_id, "catalog-test")
    assert lease is not None
    repo.fail_job(broken.job_id, "catalog-test", lease.lease_epoch, "parse_failed")

    first = workspace.documents(repo, actor, kb.kb_id, limit=1)
    assert first.total == 4 and len(first.items) == 1
    assert first.counts == {"review": 1, "ready": 1, "processing": 1, "failed": 1}
    filtered = workspace.documents(repo, actor, kb.kb_id, state="review", search="退款", limit=1)
    assert filtered.total == 1 and filtered.items[0].document_id == review_id
    assert filtered.items[0].activity_at >= filtered.items[0].created_at
    assert filtered.counts == {"review": 1, "failed": 1}
    processing = workspace.documents(repo, actor, kb.kb_id, state="processing")
    assert processing.items[0].document_id == pending.document_id


def test_automatic_activation_and_opt_out(repo: Repository, actor: Principal) -> None:
    """上传自动生效，关闭后保留旧发布，手动保存可独立生效。"""
    kb = repo.create_kb(actor, "自动生效验收")
    assert kb.auto_activate

    def upload(name: str, result: str) -> str:
        """模拟已完成清洗的 Worker 提交，不创建人工审核记录。"""
        job = repo.submit_parse(actor, kb.kb_id, name, "a" * 64, name)
        lease = repo.claim_job(job.job_id, "fixture")
        assert lease and job.document_id
        repo.complete_parse(
            job.job_id, "fixture", lease.lease_epoch, result, 1, None, [], prepare_update=True
        )
        return job.document_id

    def pending_build() -> tuple[str, int, str]:
        """取持久任务并冻结相应索引输入。"""
        job = next(
            row
            for row in repo.list_jobs(actor, kb.kb_id)
            if row.kind == "build" and row.status == "queued"
        )
        lease = repo.claim_job(job.job_id, "fixture")
        assert lease
        candidate = repo.start_build(job.job_id, "fixture", lease.lease_epoch, uuid4().hex, "model")
        return job.job_id, lease.lease_epoch, candidate.build_id

    online = upload("first.txt", "b" * 64)
    assert repo.get_kb(actor, kb.kb_id).active_release_id is None
    assert workspace.documents(repo, actor, kb.kb_id).counts == {"activating": 1}
    job_id, epoch, build_id = pending_build()
    repo.complete_build(job_id, "fixture", epoch, build_id)
    first = repo.get_kb(actor, kb.kb_id).active_release_id
    assert first and repo.get_document(actor, online).indexed_result_id == "b" * 64

    in_flight = upload("second.txt", "c" * 64)
    job_id, epoch, build_id = pending_build()
    repo.set_auto_activate(actor, kb.kb_id, False)
    with pytest.raises(DomainError, match="资料或权限已改变"):
        repo.complete_build(job_id, "fixture", epoch, build_id)
    repo.fail_job(job_id, "fixture", epoch, "activation_stale")
    offline = upload("third.txt", "d" * 64)
    assert repo.get_kb(actor, kb.kb_id).active_release_id == first
    assert repo.get_document(actor, offline).selected_result_id is None

    draft = repo.get_document(actor, offline)
    repo.save_revision(
        actor,
        offline,
        "d" * 64,
        "e" * 64,
        2,
        "d" * 64,
        [],
        activate=True,
        expected_document_revision=draft.revision,
    )
    job_id, epoch, build_id = pending_build()
    completed = repo.complete_build(job_id, "fixture", epoch, build_id)
    assert {item.document_id for item in completed.manifest} == {online, offline}
    assert repo.get_document(actor, offline).indexed_result_id == "e" * 64
    assert repo.get_document(actor, in_flight).indexed_result_id is None


def test_import_processing_is_frozen_in_manifest_and_native_preview(
    repo: Repository, actor: Principal, tmp_path: Path
) -> None:
    """真实解析/切分与PG冻结配置一致，重新解析、修订和资产采用保持处理规则。"""
    from time import time

    from inkrag_platform.processing import normalize_processing, preview_chunks
    from llama_index.core.embeddings import MockEmbedding
    from opensearchpy import OpenSearch

    from inkrag.models import ProcessingConfig
    from inkrag.search import SearchBackend, result_nodes

    storage = LocalAssetStore.open(tmp_path / "processing-assets")
    source = tmp_path / "manual.md"
    source.write_text("# 操作说明\n\n" + "Document processing preserves source evidence. " * 90)
    parsed = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=storage)
    save_stored_result(parsed, storage)
    assert parsed.source.asset_id
    backend = SearchBackend(
        OpenSearch(["http://127.0.0.1:1"]),
        MockEmbedding(embed_dim=4),
        model_revision="preview-test",
        dimensions=4,
        replicas=0,
        max_tokens=128,
    )

    def assets(_actor: Principal) -> AssetStorage:
        """复用本用例的隔离原件存储。"""
        return storage

    runtime = Runtime(repo, assets, FixtureIdentity(actor), backend, {})
    config: dict[str, JsonValue] = {
        "chunking": "sentence",
        "chunk_size": 96,
        "chunk_overlap": 8,
        "include_title": True,
    }
    normalized = normalize_processing(runtime, config)
    assert normalized == config
    kb = repo.create_kb(actor, "分块冻结")
    preview = preview_chunks(runtime, actor.tenant_id, kb.kb_id, "preview", parsed, config)
    nodes = result_nodes(
        actor.tenant_id,
        kb.kb_id,
        "confirmed",
        parsed,
        ProcessingConfig.model_validate(config),
        max_tokens=128,
    )
    assert preview["chunk_count"] == len(nodes)
    chunks = preview["chunks"]
    assert isinstance(chunks, list) and len(chunks) > 1
    assert [item["text"] for item in chunks if isinstance(item, dict)] == [
        node.get_content() for node in nodes[:20]
    ]
    assert all(
        isinstance(item, dict)
        and isinstance(item["token_count"], int)
        and item["token_count"] <= 96
        for item in chunks
    )
    with pytest.raises(DomainError):
        normalize_processing(runtime, {"chunk_size": 256})
    jobs = repo.confirm_import(
        actor,
        kb.kb_id,
        [
            (
                "stage-processing",
                source.name,
                parsed.source.asset_id,
                parsed.result_id,
                {},
                {"engine": "builtin", "processing": config},
                int(time()) + 60,
            )
        ],
    )
    job = jobs[0]
    assert job.document_id
    lease = repo.claim_job(job.job_id, "processing-test")
    assert lease
    repo.complete_parse(
        job.job_id,
        "processing-test",
        lease.lease_epoch,
        parsed.result_id,
        parsed.revision,
        parsed.previous_result_id,
        [],
        prepare_update=True,
    )
    document = repo.get_document(actor, job.document_id)
    assert document.processing == config
    workspace.select_result(
        repo,
        actor,
        document.document_id,
        SelectionRequest(expected_document_revision=document.revision, result_id=parsed.result_id),
    )
    build_job = repo.accept_job(actor, "build", kb.kb_id, None, {}, "processing-build")
    build_lease = repo.claim_job(build_job.job_id, "processing-test")
    assert build_lease
    build = repo.start_build(
        build_job.job_id,
        "processing-test",
        build_lease.lease_epoch,
        "processing-" + uuid4().hex,
        "model",
    )
    assert build.manifest[0].processing == config
    changed = build.manifest[0].model_copy(update={"processing": {**config, "chunk_size": 64}})
    assert changed != build.manifest[0]
    target = repo.create_kb(actor, "采用处理参数")
    adopted = workspace.adopt(
        repo,
        actor,
        target.kb_id,
        AdoptionRequest(
            source_document_id=document.document_id, result_id=parsed.result_id, metadata={}
        ),
        source_asset_id=parsed.source.asset_id,
    )
    assert adopted.processing == config
    repo.submit_reparse(actor, document.document_id, document.revision, "processing-reparse")
    assert repo.get_document(actor, document.document_id).processing == config
    repo.close()
    backend.client.close()


@pytest.mark.parametrize(
    "maximum,expected_size,expected_overlap", [(128, 128, 16), (32, 32, 4), (16, None, None)]
)
def test_processing_defaults_respect_model_budget(
    repo: Repository,
    actor: Principal,
    tmp_path: Path,
    maximum: int,
    expected_size: int | None,
    expected_overlap: int | None,
) -> None:
    """能力默认和空请求一致；不支持最小预算的模型明确不可用。"""
    from inkrag_platform.processing import normalize_processing, processing_capabilities
    from llama_index.core.embeddings import MockEmbedding
    from opensearchpy import OpenSearch

    from inkrag.search import SearchBackend

    backend = SearchBackend(
        OpenSearch(["http://127.0.0.1:1"]),
        MockEmbedding(embed_dim=4),
        model_revision="budget-test",
        dimensions=4,
        replicas=0,
        max_tokens=max(32, maximum),
    )
    # 构造器已限制32；模拟未来装配器或动态模型返回更低预算，能力也须保守拒绝。
    backend.max_tokens = maximum
    storage = LocalAssetStore.open(tmp_path / "budget-assets")

    def assets(_actor: Principal) -> AssetStorage:
        """边界校验不访问外部资源。"""
        return storage

    runtime = Runtime(repo, assets, FixtureIdentity(actor), backend, {})
    capability = processing_capabilities(runtime)
    if expected_size is None:
        assert capability["available"] is False
        assert "32" in str(capability["reason"])
        assert normalize_processing(runtime, None) is None
        with pytest.raises(DomainError):
            normalize_processing(runtime, {})
    else:
        expected = capability["defaults"]
        assert isinstance(expected, dict)
        assert expected["chunk_size"] == expected_size
        assert expected["chunk_overlap"] == expected_overlap
        assert normalize_processing(runtime, None) == normalize_processing(runtime, {}) == expected
        assert normalize_processing(runtime, {"include_title": True}) == {
            **expected,
            "include_title": True,
        }
    backend.client.close()


def test_replacement_rejects_incompatible_frozen_parser_rules(
    repo: Repository, actor: Principal
) -> None:
    """PDF替换为DOCX时不接受失效高级规则，同格式替换仍保留参数。"""
    from time import time

    kb = repo.create_kb(actor, "替换参数范围")
    rules: dict[str, JsonValue] = {
        "engine": "docling",
        "ocr_enabled": True,
        "ocr_mode": "full_page",
        "table_structure": True,
        "table_mode": "accurate",
    }
    job = repo.confirm_import(
        actor,
        kb.kb_id,
        [("replace-stage", "source.pdf", "a" * 64, "b" * 64, {}, rules, int(time()) + 60)],
    )[0]
    assert job.document_id
    before = repo.get_document(actor, job.document_id)
    with pytest.raises(DomainError, match="重新导入") as refused:
        repo.submit_reparse(
            actor, job.document_id, before.revision, "cross-format", "c" * 64, "replacement.docx"
        )
    assert refused.value.code == "invalid_request"
    assert repo.get_document(actor, job.document_id) == before
    same = repo.submit_reparse(
        actor, job.document_id, before.revision, "same-format", "c" * 64, "replacement.pdf"
    )
    assert same.payload["import_rules"] == rules
