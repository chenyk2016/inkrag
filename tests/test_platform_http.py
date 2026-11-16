"""显式PG集成验证HTTP控制面；身份测试适配不替代OIDC贯穿验收。"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from inkparse import FileSource, Parser, ParseRequest, load_stored_result, save_stored_result
from inkrag_platform.api import create_app
from inkrag_platform.models import Document, DomainError, Job, KnowledgeBase, Principal
from inkrag_platform.operations import DocumentView, Runtime
from inkrag_platform.repository import Repository
from inkstore import AssetStorage
from inkstore.local import LocalAssetStore
from pydantic import JsonValue

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("INKRAG_TEST_DATABASE_DSN"), reason="需要显式专用PG测试DSN和迁移"
    ),
]


class FixtureIdentity:
    """仅测试HTTP可信身份注入，不可用于生产wiring。"""

    def __init__(self, actor: Principal) -> None:
        """每个用例使用全新租户，避免触碰其他知识库。"""
        self.actor = actor

    def verify(self, token: str) -> Principal:
        """预设测试主体覆盖viewer和跨租户请求，不测试JWT密码学。"""
        if token == "editor":
            return self.actor
        if token == "viewer":
            return Principal(tenant_id=self.actor.tenant_id, subject="viewer", roles=["viewer"])
        if token == "alien":
            return Principal(
                tenant_id="alien-" + self.actor.tenant_id, subject="alien", roles=["admin"]
            )
        raise DomainError("unauthorized", "测试令牌无效")


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    """真实PG仓库配临时测试资产后端；测试结束只关闭借入连接。"""
    repo = Repository(os.environ["INKRAG_TEST_DATABASE_DSN"])
    repo.check()
    actor = Principal(tenant_id="http-" + uuid4().hex, subject="tester", roles=["admin"])
    storage = LocalAssetStore.open(tmp_path / "assets")

    def scoped(_principal: Principal) -> AssetStorage:
        """测试隔离资产仍由PG归属授权保护。"""
        return storage

    yield Runtime(
        repo,
        scoped,
        FixtureIdentity(actor),
        None,
        {"issuer": "https://identity.example", "client_id": "test"},
        "parse",
    )
    repo.close()


def test_http_persistent_upload_cancel_and_scope(runtime: Runtime) -> None:
    """202持久接受、重试幂等、跨租户和只读写入拒绝由真实PG判定。"""
    with TestClient(create_app(runtime)) as api:
        assert api.get("/api/v2/knowledge-bases").status_code == 401
        headers = {"Authorization": "Bearer editor"}
        kb_response = api.post(
            "/api/v2/knowledge-bases", json={"name": "HTTP测试库"}, headers=headers
        )
        assert kb_response.status_code == 201
        kb = KnowledgeBase.model_validate_json(kb_response.content)
        path = f"/api/v2/knowledge-bases/{kb.kb_id}/documents?filename=manual.md"
        upload_headers = {
            **headers,
            "Idempotency-Key": "http-upload-unique",
            "Content-Type": "application/octet-stream",
        }
        upload = api.post(path, content="# 生产测试\n\n原始内容。".encode(), headers=upload_headers)
        assert upload.status_code == 202
        job = Job.model_validate_json(upload.content)
        assert job.status == "queued" and job.document_id
        repeat = api.post(path, content="# 生产测试\n\n原始内容。".encode(), headers=upload_headers)
        assert Job.model_validate_json(repeat.content).job_id == job.job_id
        assert (
            api.get(
                f"/api/v2/documents/{job.document_id}", headers={"Authorization": "Bearer alien"}
            ).status_code
            == 404
        )
        assert (
            api.post(
                path,
                content=b"viewer cannot write",
                headers={"Authorization": "Bearer viewer", "Idempotency-Key": "viewer-write-test"},
            ).status_code
            == 403
        )
        pending = DocumentView.model_validate_json(
            api.get(f"/api/v2/documents/{job.document_id}", headers=headers).content
        )
        assert pending.result is None
        cancelled = api.post(f"/api/v2/jobs/{job.job_id}/cancel", headers=headers)
        assert Job.model_validate_json(cancelled.content).status == "cancelled"
        assert cancelled.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in cancelled.headers["content-security-policy"]
        assert cancelled.headers["x-request-id"]


def test_http_parse_revision_assets_and_cas(runtime: Runtime, tmp_path: Path) -> None:
    """核心解析产物经资产保存与PG完成后可审校，过期CAS不能覆盖已保存文字。"""
    actor = runtime.auth.verify("editor")
    kb = runtime.repo.create_kb(actor, "审校库")
    source = tmp_path / "manual.md"
    source.write_text("# 原始版本\n\n可追溯内容。", encoding="utf-8")
    storage = runtime.assets_for(actor)
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=storage)
    save_stored_result(result, storage)
    assert result.source.asset_id
    job = runtime.repo.submit_parse(
        actor, kb.kb_id, source.name, result.source.asset_id, "revision-test"
    )
    lease = runtime.repo.claim_job(job.job_id, "http-fixture")
    assert lease and job.document_id
    runtime.repo.complete_parse(
        job.job_id,
        "http-fixture",
        lease.lease_epoch,
        result.result_id,
        result.revision,
        result.previous_result_id,
        [a.asset_id for a in result.assets],
    )
    with TestClient(create_app(runtime)) as api:
        headers = {"Authorization": "Bearer editor"}
        path = f"/api/v2/documents/{job.document_id}"
        initial = DocumentView.model_validate_json(api.get(path, headers=headers).content)
        assert initial.result and initial.result.result_id == result.result_id
        body = {
            "expected_result_id": result.result_id,
            "changes": [{"element_ref": "#/texts/0", "text": "审校后的内容"}],
        }
        saved = api.patch(path, json=body, headers=headers)
        assert saved.status_code == 200
        view = DocumentView.model_validate_json(saved.content)
        assert view.result and view.result.revision == 2
        assert api.patch(path, json=body, headers=headers).status_code == 409
        assert (
            load_stored_result(storage, result.result_id).document.texts[0].text != "审校后的内容"
        )
        asset_path = f"/api/v2/assets/{result.source.asset_id}"
        assert api.get(asset_path, headers=headers).content == source.read_bytes()
        assert api.get(asset_path, headers={"Authorization": "Bearer alien"}).status_code == 404
        deleted = api.delete(f"{path}?expected_revision=2", headers=headers)
        assert Document.model_validate_json(deleted.content).deleted
        assert api.get(asset_path, headers=headers).status_code == 404


def test_heavy_http_capacity_is_bounded_until_response_end() -> None:
    """纯ASGI反例：上传和大响应各两槽，普通控制不抢槽，繁忙不排队。"""
    import asyncio

    from inkrag_platform.http_limits import CapacityGate
    from starlette.types import Message, Receive, Scope, Send

    async def exercise() -> None:
        """用受控发送屏障验证完整响应结束前不释放槽位。"""
        release = asyncio.Event()
        started = asyncio.Event()
        entries: list[str] = []

        async def application(scope: Scope, _receive: Receive, send: Send) -> None:
            """只作为协议级慢操作夹具，不替代任何业务依赖。"""
            path = scope["path"]
            entries.append(path)
            if len(entries) >= 2:
                started.set()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            if path != "/api/v2/jobs":
                await release.wait()
            await send({"type": "http.response.body", "body": b"{}"})

        gate = CapacityGate(application)

        async def invoke(path: str, method: str) -> int:
            """返回协议状态，不读取任何真实外部连接。"""
            statuses: list[int] = []

            async def receive() -> Message:
                """协议正文不存在；排队请求不应尝试读取它。"""
                raise AssertionError("不应读取正文")

            async def send(message: Message) -> None:
                """仅记录状态，测试不伪造成功业务数据。"""
                if message["type"] == "http.response.start":
                    statuses.append(message["status"])

            await gate({"type": "http", "path": path, "method": method}, receive, send)
            assert len(statuses) == 1
            return statuses[0]

        upload = "/api/v2/knowledge-bases/kb/documents"
        first = asyncio.create_task(invoke(upload, "POST"))
        second = asyncio.create_task(invoke(upload, "POST"))
        await started.wait()
        assert await invoke(upload, "POST") == 429
        assert entries.count(upload) == 2
        assert await invoke("/api/v2/jobs", "GET") == 200
        asset_one = asyncio.create_task(invoke("/api/v2/assets/one", "GET"))
        asset_two = asyncio.create_task(invoke("/api/v2/assets/two", "GET"))
        # 显式让两个已创建任务开始领取大响应槽。
        await asyncio.sleep(0)
        assert await invoke("/api/v2/assets/three", "GET") == 429
        release.set()
        assert await asyncio.gather(first, second, asset_one, asset_two) == [200] * 4
        assert await invoke(upload, "POST") == 200
        assert await invoke("/api/v2/assets/recovered", "GET") == 200

    asyncio.run(exercise())


def test_upload_idempotency_survives_metadata_and_schema_change(runtime: Runtime) -> None:
    """已经接受的相同上传优先核对原意图，不被后续字段定义遮蔽；改内容仍409。"""
    from urllib.parse import quote

    from inkrag_platform.models import MetadataField

    actor = runtime.auth.verify("editor")
    kb = runtime.repo.create_kb(actor, "重放上传")
    kb = runtime.repo.set_fields(
        actor, kb.kb_id, kb.revision, [MetadataField(name="department", label="部门", type="text")]
    )
    with TestClient(create_app(runtime)) as api:
        headers = {
            "Authorization": "Bearer editor",
            "Idempotency-Key": "durable-upload-intent",
            "X-Ink-Metadata": quote('{"department":"财务"}', safe=""),
        }
        path = f"/api/v2/knowledge-bases/{kb.kb_id}/documents?filename=replay.md"
        accepted = api.post(path, content=b"durable-original", headers=headers)
        assert accepted.status_code == 202
        job = Job.model_validate_json(accepted.content)
        assert job.document_id
        # 先结束解析，再修改业务字段以复现已完成上传的重放。
        lease = runtime.repo.claim_job(job.job_id, "http-fixture")
        assert lease
        doc = runtime.repo.complete_parse(
            job.job_id, "http-fixture", lease.lease_epoch, "c" * 64, 1, None, []
        )
        runtime.repo.update_metadata(actor, doc.document_id, doc.revision, {})
        latest = runtime.repo.get_kb(actor, kb.kb_id)
        runtime.repo.set_fields(actor, kb.kb_id, latest.revision, [])
        replay = api.post(path, content=b"durable-original", headers=headers)
        assert replay.status_code == 202
        assert Job.model_validate_json(replay.content).job_id == job.job_id
        assert api.post(path, content=b"different-content", headers=headers).status_code == 409
        assert len(runtime.repo.list_documents(actor, kb.kb_id)) == 1


class NoInlineSearch:
    """HTTP接受任务的检索端口夹具；任何同步检索都使该用例失败。"""

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
    ) -> dict[str, JsonValue]:
        """本用例只测试持久任务，不能用返回假命中掩盖同步执行。"""
        raise AssertionError("HTTP不应执行后台检索")

    def fields_json(
        self, tenant_id: str, knowledge_base_id: str, generation: str
    ) -> dict[str, JsonValue]:
        """接受入库意图无需读取搜索索引。"""
        raise AssertionError("HTTP不应读取索引字段")


def test_ingest_idempotency_survives_document_revision_change(
    runtime: Runtime, tmp_path: Path
) -> None:
    """接受后修订资料，同意图重放返回原job，异意图重用键或新键过期提交均409。"""
    from dataclasses import replace

    actor = runtime.auth.verify("editor")
    kb = runtime.repo.create_kb(actor, "入库重放")
    source = tmp_path / "replay.md"
    source.write_text("原始入库正文", encoding="utf-8")
    storage = runtime.assets_for(actor)
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=storage)
    assert result.source.asset_id
    job = runtime.repo.submit_parse(
        actor, kb.kb_id, source.name, result.source.asset_id, "seed-ingest-replay"
    )
    lease = runtime.repo.claim_job(job.job_id, "fixture")
    assert lease and job.document_id
    document = runtime.repo.complete_parse(
        job.job_id,
        "fixture",
        lease.lease_epoch,
        result.result_id,
        result.revision,
        result.previous_result_id,
        [],
    )
    with TestClient(create_app(replace(runtime, search=NoInlineSearch()))) as api:
        path = f"/api/v2/documents/{document.document_id}"
        headers = {"Authorization": "Bearer editor", "Idempotency-Key": "durable-ingest-intent"}
        intent = {
            "expected_result_id": result.result_id,
            "expected_document_revision": document.revision,
        }
        accepted = api.post(path + "/ingest", json=intent, headers=headers)
        assert accepted.status_code == 202
        prior_job = Job.model_validate_json(accepted.content)
        saved = api.patch(
            path,
            json={
                "expected_result_id": result.result_id,
                "changes": [{"element_ref": "#/texts/0", "text": "后续修订正文"}],
            },
            headers=headers,
        )
        current = DocumentView.model_validate_json(saved.content)
        assert current.result
        replay = api.post(path + "/ingest", json=intent, headers=headers)
        assert replay.status_code == 202
        assert Job.model_validate_json(replay.content).job_id == prior_job.job_id
        changed = {
            "expected_result_id": current.result.result_id,
            "expected_document_revision": current.document.revision,
        }
        assert api.post(path + "/ingest", json=changed, headers=headers).status_code == 409
        assert (
            api.post(
                path + "/ingest",
                json=intent,
                headers={**headers, "Idempotency-Key": "new-but-stale-intent"},
            ).status_code
            == 409
        )


@pytest.mark.parametrize("case", ["single", "empty_tail", "streaming", "empty_prefix"])
def test_heavy_response_chunks_preserve_bytes_and_closing_flag(case: str) -> None:
    """大响应每次只向ASGI传64KiB，保持原字节和结束标志，使原生drain可以生效。"""
    import asyncio

    from inkrag_platform.http_limits import CapacityGate
    from starlette.types import Message, Receive, Scope, Send

    async def exercise() -> None:
        """协议夹具只检验发送边界，不作为外部存储或检索替代实现。"""
        raw = b"x" * (65536 * 2 + 17)
        chunks: list[bytes] = []
        flags: list[bool] = []
        parts: list[tuple[bytes, bool]] = [(raw, False)]
        if case == "empty_tail":
            parts = [(raw, True), (b"", False)]
        elif case == "streaming":
            parts = [(raw[:123], True), (raw[123:], False)]
        elif case == "empty_prefix":
            parts = [(b"", True), (raw, False)]

        async def application(_scope: Scope, _receive: Receive, send: Send) -> None:
            """模拟JSONResponse/二进制Response一次提供完整body的原生调用。"""
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", str(len(raw)).encode())],
                }
            )
            for data, more in parts:
                await send({"type": "http.response.body", "body": data, "more_body": more})

        async def receive() -> Message:
            """该响应路径无需请求body。"""
            raise AssertionError("不应读取请求body")

        async def send(message: Message) -> None:
            """记录实际ASGI发送大小，而非只检查实现中的参数常量。"""
            if message["type"] == "http.response.start":
                assert (
                    dict(message.get("headers", [])).get(b"content-length")
                    == str(len(raw)).encode()
                )
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                assert len(chunk) <= 65536
                chunks.append(chunk)
                flags.append(message.get("more_body", False))

        await CapacityGate(application)(
            {"type": "http", "path": "/api/v2/assets/blob", "method": "GET"}, receive, send
        )
        assert b"".join(chunks) == raw
        assert flags[-1] is False and all(flags[:-1])

    asyncio.run(exercise())


def test_heavy_response_cancel_releases_both_slots() -> None:
    """发送期间取消不泄漏大响应槽位；连续取消两次后正常请求仍成功。"""
    import asyncio

    from inkrag_platform.http_limits import CapacityGate
    from starlette.types import Message, Receive, Scope, Send

    async def exercise() -> None:
        """在真实ASGI取消异常边界验证finally释放，而非检查内部计数器。"""
        received: list[bytes] = []

        async def application(_scope: Scope, _receive: Receive, send: Send) -> None:
            """一次提供超过两块的响应，取消发生在分块发送期间。"""
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x" * 140000})

        async def receive() -> Message:
            """该响应测试不读取请求正文。"""
            raise AssertionError("不应读取请求正文")

        async def cancelled_send(message: Message) -> None:
            """模拟客户端中途断开造成的任务取消。"""
            if message["type"] == "http.response.body":
                raise asyncio.CancelledError()

        async def successful_send(message: Message) -> None:
            """取消后仍必须收到完整响应，不能只看状态或内部信号量。"""
            if message["type"] == "http.response.body":
                received.append(message.get("body", b""))

        gate = CapacityGate(application)
        scope: Scope = {"type": "http", "path": "/api/v2/assets/blob", "method": "GET"}
        for _ in range(2):
            with pytest.raises(asyncio.CancelledError):
                await gate(scope, receive, cancelled_send)
        await gate(scope, receive, successful_send)
        assert b"".join(received) == b"x" * 140000

    asyncio.run(exercise())


def test_document_enabled_http_is_persistent_and_scoped(runtime: Runtime) -> None:
    """未解析资料也可禁用，幂等、严格布尔、只读和跨租户边界由真实PG验证。"""
    with TestClient(create_app(runtime)) as api:
        headers = {"Authorization": "Bearer editor"}
        kb = api.post("/api/v2/knowledge-bases", json={"name": "检索开关"}, headers=headers).json()
        uploaded = api.post(
            f"/api/v2/knowledge-bases/{kb['kb_id']}/documents?filename=toggle.md",
            content=b"toggle",
            headers={**headers, "Idempotency-Key": "toggle-source"},
        ).json()
        document_id = uploaded["document_id"]
        path = f"/api/v2/documents/{document_id}/enabled"
        before = api.get(f"/api/v2/documents/{document_id}/info", headers=headers).json()
        assert before["enabled"] is True
        for _ in range(2):
            response = api.patch(path, json={"enabled": False}, headers=headers)
            assert response.status_code == 200
            disabled = response.json()
            assert disabled["enabled"] is False
            assert disabled["revision"] == before["revision"]
            assert disabled["result_id"] == before["result_id"]
        assert (
            api.get(f"/api/v2/documents/{document_id}/info", headers=headers).json()["enabled"]
            is False
        )
        assert api.patch(path, json={"enabled": "false"}, headers=headers).status_code == 422
        assert (
            api.patch(
                path, json={"enabled": True}, headers={"Authorization": "Bearer viewer"}
            ).status_code
            == 403
        )
        assert (
            api.patch(
                path, json={"enabled": True}, headers={"Authorization": "Bearer alien"}
            ).status_code
            == 404
        )
        assert api.patch(path, json={"enabled": True}, headers=headers).json()["enabled"] is True


def test_import_parameter_capabilities_and_parse_only_rejection(runtime: Runtime) -> None:
    """独立解析宿主不假装可切分建索引，非Docling格式拒绝高级解析参数。"""
    with TestClient(create_app(runtime)) as api:
        headers = {"Authorization": "Bearer editor"}
        caps = api.get("/api/v2/capabilities", headers=headers).json()
        assert caps["processing"]["available"] is False
        assert caps["processing"]["max_tokens"] is None
        assert caps["processing"]["qa_pairs"]["available"] is False
        kb = api.post("/api/v2/knowledge-bases", json={"name": "参数边界"}, headers=headers).json()
        base = f"/api/v2/knowledge-bases/{kb['kb_id']}/imports"
        stage = api.post(
            base + "/staged?filename=sample.md", content=b"sample", headers=headers
        ).json()
        invalid_rules: list[dict[str, JsonValue]] = [
            {"processing": {}},
            {"table_mode": "accurate"},
            {"ocr_mode": "auto"},
        ]
        for extra in invalid_rules:
            response = api.post(
                base + "/previews",
                json={"stage_id": stage["stage_id"], "rules": {"engine": "builtin", **extra}},
                headers={**headers, "Idempotency-Key": uuid4().hex},
            )
            assert response.status_code in {422, 503}
        accepted = api.post(
            base + "/previews",
            json={"stage_id": stage["stage_id"], "rules": {"engine": "builtin"}},
            headers={**headers, "Idempotency-Key": uuid4().hex},
        )
        assert accepted.status_code == 202
        assert accepted.json()["payload"]["rules"]["processing"] is None


@pytest.mark.parametrize(
    "rules",
    [
        {"engine": "docling", "ocr_enabled": False, "ocr_mode": "auto"},
        {"engine": "docling", "table_structure": False, "table_mode": "accurate"},
    ],
)
def test_parser_rule_parent_switch_conflicts_are_rejected(rules: dict[str, JsonValue]) -> None:
    """请求父子开关冲突在提交前校验，不交给Worker执行必失败解析。"""
    from inkrag_platform.parser_rules import validate_parser_rules

    with pytest.raises(DomainError) as refused:
        validate_parser_rules("docling", "source.pdf", rules)
    assert refused.value.code == "invalid_request"
