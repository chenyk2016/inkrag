"""独立PG路由验收：审校版本、机器原文、可携带交付及保留资料身份的恢复。"""

import base64
import io
import os
import zipfile
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from inkparse import ParseError, ParseOptions, ParseResult
from inkrag_platform import parse_routes, worker
from inkrag_platform.api import create_app
from inkrag_platform.models import DomainError, Job, MetadataField, Principal
from inkrag_platform.operations import DocumentView, Runtime
from inkrag_platform.repository import Repository
from inkrag_platform.sandbox import run_parse
from inkrag_platform.settings import Settings
from inkstore import AssetStorage
from inkstore.local import LocalAssetStore
from PIL import Image
from pydantic import JsonValue, SecretStr, TypeAdapter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("INKRAG_TEST_DATABASE_DSN"), reason="需显式独立PG测试库"),
]


class RouteIdentity:
    """只替换令牌验证边界，业务归属与并发仍使用真实PG判定。"""

    def __init__(self, actor: Principal) -> None:
        """固定随机测试租户，不接入或修改企业身份。"""
        self.actor = actor

    def verify(self, token: str) -> Principal:
        """提供编辑、只读、跨租户三个真实授权负例。"""
        if token == "editor":
            return self.actor
        if token == "peer":
            return Principal(tenant_id=self.actor.tenant_id, subject="peer", roles=["editor"])
        if token == "viewer":
            return Principal(tenant_id=self.actor.tenant_id, subject="viewer", roles=["viewer"])
        if token == "alien":
            return Principal(
                tenant_id="alien-" + self.actor.tenant_id, subject="alien", roles=["admin"]
            )
        raise DomainError("unauthorized", "测试令牌无效")


@pytest.fixture
def parse_runtime(tmp_path: Path) -> Iterator[tuple[Runtime, Principal, Settings]]:
    """真实PG、临时资产和真实受限子进程；macOS显式使用开发隔离模式。"""
    import sys

    repo = Repository(os.environ["INKRAG_TEST_DATABASE_DSN"])
    try:
        repo.check()
    except Exception:
        repo.close()
        raise
    actor = Principal(tenant_id="parse-routes-" + uuid4().hex, subject="tester", roles=["admin"])
    storage = LocalAssetStore.open(tmp_path / "assets")

    def scoped(_actor: Principal) -> AssetStorage:
        """临时测试资产，访问前仍由PG资料/历史归属核对。"""
        return storage

    settings = Settings(
        capabilities="parse",
        sandbox_required=sys.platform == "linux",
        database_url=SecretStr(os.environ["INKRAG_TEST_DATABASE_DSN"]),
        rabbit_url=SecretStr("amqp://test:test@unused/test"),
        s3_endpoint="http://unused",
        oidc_issuer="http://unused",
    )
    yield Runtime(repo, scoped, RouteIdentity(actor), None, {}, "parse"), actor, settings
    repo.close()


def finish_parse(runtime: Runtime, actor: Principal, settings: Settings, job: Job) -> ParseResult:
    """领取PG真实租约，隔离读取冻结原件，成功再事务提交结果。"""
    assert job.document_id is not None
    lease = runtime.repo.claim_job(job.job_id, "parse-route-test", 60)
    assert lease is not None
    document = runtime.repo.get_document(actor, job.document_id)
    source = job.payload.get("source_asset_id", document.source_asset_id)
    name = job.payload.get("filename", document.name)
    assert isinstance(source, str) and isinstance(name, str)
    storage = runtime.assets_for(actor)
    result = run_parse(
        storage.read(source),
        name,
        storage,
        settings,
        ParseOptions.model_validate(
            {
                "engine": job.payload.get("parser_engine", "builtin"),
                "ocr_enabled": job.payload.get("ocr_enabled"),
            }
        ),
    )
    runtime.repo.complete_parse(
        job.job_id,
        "parse-route-test",
        lease.lease_epoch,
        result.result_id,
        result.revision,
        result.previous_result_id,
        [binding.asset_id for binding in result.assets],
    )
    return result


def test_pg_structure_cas_machine_preview_bundle_and_scope(
    parse_runtime: tuple[Runtime, Principal, Settings],
) -> None:
    """实际HTTP保存结构、冲突、机器对照、ZIP下载与跨身份拒绝。"""
    runtime, actor, settings = parse_runtime
    kb = runtime.repo.create_kb(actor, "解析路由验收")
    raw = b"<!doctype html><html><body><h1>Source</h1><p>noise</p><table><tr><td>17</td></tr></table></body></html>"
    source = runtime.assets_for(actor).put_bytes(raw)
    job = runtime.repo.submit_parse(
        actor, kb.kb_id, "source.html", source.asset_id, "initial-upload"
    )
    original = finish_parse(runtime, actor, settings, job)
    assert job.document_id is not None
    path = "/api/v2/documents/" + job.document_id
    headers = {"Authorization": "Bearer editor"}
    with TestClient(create_app(runtime)) as client:
        schema = client.get("/api/v2/openapi.json", headers=headers).json()
        upload = schema["paths"]["/api/v2/documents/{document_id}/source"]["put"]
        assert upload["requestBody"]["required"] is True
        assert (
            upload["requestBody"]["content"]["application/octet-stream"]["schema"]["format"]
            == "binary"
        )
        assert any(
            parameter["name"] == "idempotency-key" and parameter["required"]
            for parameter in upload["parameters"]
        )
        bundle_schema = schema["paths"]["/api/v2/documents/{document_id}/bundle"]["get"]
        assert (
            bundle_schema["responses"]["200"]["content"]["application/zip"]["schema"]["format"]
            == "binary"
        )
        body = {
            "expected_result_id": original.result_id,
            "operations": [
                {
                    "kind": "table_cell",
                    "element_ref": "#/tables/0",
                    "row": 0,
                    "column": 0,
                    "text": "29",
                },
                {"kind": "exclude", "element_ref": "#/texts/1", "excluded": True},
            ],
        }
        saved = client.patch(path + "/structure", headers=headers, json=body)
        assert saved.status_code == 200, saved.text
        revised = DocumentView.model_validate_json(saved.content).result
        assert revised is not None and revised.schema_version == "inkparse.result.v4"
        assert client.patch(path + "/structure", headers=headers, json=body).status_code == 409
        assert (
            client.patch(
                path + "/structure", headers={"Authorization": "Bearer viewer"}, json=body
            ).status_code
            == 403
        )
        preview = client.get(path + "/preview", headers=headers)
        assert preview.status_code == 200, preview.text
        assert preview.json()["source_kind"] == "native_structure"
        machine = ParseResult.model_validate(preview.json()["result"])
        assert (
            machine.result_id == original.result_id
            and machine.document.tables[0].data.table_cells[0].text == "17"
        )
        packet = client.get(path + "/bundle?include_original=true", headers=headers)
        assert packet.status_code == 200 and packet.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(packet.content)) as archive:
            assert archive.read("original/source.html") == raw
            markdown = archive.read("document.md").decode()
            assert "29" in markdown and "noise" not in markdown
        assert (
            client.get(path + "/preview", headers={"Authorization": "Bearer alien"}).status_code
            == 404
        )
        assert (
            client.get(path + "/bundle", headers={"Authorization": "Bearer alien"}).status_code
            == 404
        )
        assert client.get(path + "/bundle").status_code == 401
        other = runtime.repo.submit_parse(
            actor, kb.kb_id, "other.html", source.asset_id, "another-upload"
        )
        assert other.document_id is not None
        assert (
            client.get(
                "/api/v2/documents/"
                + other.document_id
                + "/preview?result_id="
                + original.result_id,
                headers=headers,
            ).status_code
            == 404
        )


def test_pg_replace_source_and_identical_reparse_preserve_identity(
    parse_runtime: tuple[Runtime, Principal, Settings],
) -> None:
    """坏文件可在同资料修复；旧资料不提前覆盖，同源重跑不能撞内容去重主键。"""
    runtime, actor, settings = parse_runtime
    kb = runtime.repo.create_kb(actor, "恢复路由验收")
    asset = runtime.assets_for(actor).put_bytes(b"Original stable text")
    first = runtime.repo.submit_parse(
        actor, kb.kb_id, "original.txt", asset.asset_id, "original-upload"
    )
    original = finish_parse(runtime, actor, settings, first)
    assert first.document_id is not None
    path = "/api/v2/documents/" + first.document_id
    document = runtime.repo.get_document(actor, first.document_id)
    headers = {"Authorization": "Bearer editor", "Idempotency-Key": "bad-source-replace"}
    with TestClient(create_app(runtime)) as client:
        url = (
            path + "/source?filename=bad.json&expected_document_revision=" + str(document.revision)
        )
        reply = client.put(url, content=b'{"private-key":}', headers=headers)
        assert reply.status_code == 202, reply.text
        bad = Job.model_validate_json(reply.content)
        unchanged = runtime.repo.get_document(actor, first.document_id)
        assert (
            unchanged.source_asset_id == asset.asset_id
            and unchanged.result_id == original.result_id
        )
        with pytest.raises(ParseError) as failure:
            finish_parse(runtime, actor, settings, bad)
        leased = runtime.repo.get_job(actor, bad.job_id)
        runtime.repo.fail_job(
            bad.job_id,
            "parse-route-test",
            leased.lease_epoch,
            failure.value.code,
            diagnostic=failure.value.diagnostic().model_dump(mode="json"),
        )
        failed = runtime.repo.get_job(actor, bad.job_id)
        assert failed.status == "failed" and failed.error_diagnostic is not None
        assert failed.error_diagnostic.get("retryable") is False
        assert failed.error_diagnostic.get("suggestion")
        assert "private-key" not in str(failed.error_diagnostic)
        rejected = client.post("/api/v2/jobs/" + bad.job_id + "/retry", headers=headers)
        assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "invalid_request"
        headers["Idempotency-Key"] = "fixed-source-replace"
        repaired = client.put(
            path
            + "/source?filename=fixed.json&expected_document_revision="
            + str(document.revision),
            content=b'{"items":29}',
            headers=headers,
        )
        assert repaired.status_code == 202, repaired.text
        fixed_job = Job.model_validate_json(repaired.content)
        result = finish_parse(runtime, actor, settings, fixed_job)
        fixed = runtime.repo.get_document(actor, first.document_id)
        assert fixed.document_id == first.document_id and fixed.name == "fixed.json"
        assert (
            fixed.source_asset_id == result.source.asset_id
            and fixed.source_asset_id != asset.asset_id
        )
        old = client.get(
            path + "/preview?result_id=" + original.result_id,
            headers={"Authorization": "Bearer editor"},
        )
        assert old.status_code == 200 and old.json()["result"]["source"]["name"] == "original.txt"
        headers["Idempotency-Key"] = "reparse-current-source"
        rerun = client.post(
            path + "/reparse", json={"expected_document_revision": fixed.revision}, headers=headers
        )
        assert rerun.status_code == 202, rerun.text
        rerun_job = Job.model_validate_json(rerun.content)
        repeated = client.post(
            path + "/reparse", json={"expected_document_revision": fixed.revision}, headers=headers
        )
        assert Job.model_validate_json(repeated.content).job_id == rerun_job.job_id
        rerun_result = finish_parse(runtime, actor, settings, rerun_job)
        assert rerun_result.result_id == result.result_id
        assert runtime.repo.get_job(actor, rerun_job.job_id).status == "succeeded"
        headers["Idempotency-Key"] = "stale-version-source"
        stale = client.put(url, content=b"{}", headers=headers)
        assert stale.status_code == 409


def test_pg_first_parse_failure_recovers_from_revision_zero(
    parse_runtime: tuple[Runtime, Principal, Settings],
) -> None:
    """首传坏JSON失败后，版本0可重解析或替换原件，同资料恢复且保留失败证据。"""
    runtime, actor, settings = parse_runtime
    kb = runtime.repo.create_kb(actor, "首传失败恢复验收")
    headers = {"Authorization": "Bearer editor", "Idempotency-Key": "initial-broken-json"}
    raw = b'{"items":}'
    with TestClient(create_app(runtime)) as client:
        uploaded = client.post(
            "/api/v2/knowledge-bases/" + kb.kb_id + "/documents?filename=broken.json",
            content=raw,
            headers=headers,
        )
        assert uploaded.status_code == 202, uploaded.text
        first = Job.model_validate_json(uploaded.content)
        assert first.document_id is not None
        document_id = first.document_id
        path = "/api/v2/documents/" + document_id
        with pytest.raises(ParseError) as failure:
            finish_parse(runtime, actor, settings, first)
        leased = runtime.repo.get_job(actor, first.job_id)
        failed = runtime.repo.fail_job(
            first.job_id,
            "parse-route-test",
            leased.lease_epoch,
            failure.value.code,
            diagnostic=failure.value.diagnostic().model_dump(mode="json"),
        )
        assert failed.status == "failed" and failed.error_code == "invalid_json"
        initial = runtime.repo.get_document(actor, document_id)
        assert initial.revision == 0 and initial.result_id is None
        original_asset_id = initial.source_asset_id
        not_ready = client.get(path, headers=headers)
        assert not_ready.status_code == 200
        initial_view = DocumentView.model_validate_json(not_ready.content)
        assert initial_view.result is None and initial_view.version is None

        headers["Idempotency-Key"] = "revision-zero-reparse"
        rerun = client.post(
            path + "/reparse", json={"expected_document_revision": 0}, headers=headers
        )
        assert rerun.status_code == 202, rerun.text
        rerun_job = Job.model_validate_json(rerun.content)
        assert rerun_job.document_id == document_id and rerun_job.expected_document_revision == 0
        runtime.repo.cancel_job(actor, rerun_job.job_id)
        assert (
            client.post(
                path + "/reparse", json={"expected_document_revision": -1}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.put(
                path + "/source?filename=fixed.json&expected_document_revision=-1",
                content=b'{"items":29}',
                headers=headers,
            ).status_code
            == 422
        )

        headers["Idempotency-Key"] = "revision-zero-replace"
        replacement = client.put(
            path + "/source?filename=fixed.json&expected_document_revision=0",
            content=b'{"items":29}',
            headers=headers,
        )
        assert replacement.status_code == 202, replacement.text
        recovered_job = Job.model_validate_json(replacement.content)
        assert (
            recovered_job.document_id == document_id
            and recovered_job.expected_document_revision == 0
        )
        pending = runtime.repo.get_document(actor, document_id)
        assert pending.revision == 0 and pending.result_id is None
        assert pending.source_asset_id == original_asset_id and pending.name == "broken.json"
        parsed = finish_parse(runtime, actor, settings, recovered_job)
        reply = client.get(path, headers=headers)
        assert reply.status_code == 200, reply.text
        recovered = DocumentView.model_validate_json(reply.content)
        assert recovered.document.document_id == document_id and recovered.document.revision == 1
        assert recovered.document.name == "fixed.json"
        assert recovered.document.source_asset_id == parsed.source.asset_id != original_asset_id
        assert recovered.result is not None and recovered.result.result_id == parsed.result_id
        assert recovered.version is not None
        assert runtime.repo.get_job(actor, recovered_job.job_id).status == "succeeded"
        assert runtime.repo.get_job(actor, first.job_id).status == "failed"
        assert runtime.assets_for(actor).read(original_asset_id) == raw

        headers["Idempotency-Key"] = "stale-revision-zero"
        stale = client.put(
            path + "/source?filename=stale.json&expected_document_revision=0",
            content=b'{"items":31}',
            headers=headers,
        )
        assert stale.status_code == 409
        assert (
            client.post(
                path + "/reparse", json={"expected_document_revision": 0}, headers=headers
            ).status_code
            == 409
        )


def test_pg_corrupt_image_export_returns_safe_422(
    parse_runtime: tuple[Runtime, Principal, Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实损坏PNG经解析持久化后，原始导出路由返回422并清理目录，保留坏输入证据。"""
    runtime, actor, settings = parse_runtime
    image_stream = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(image_stream, format="PNG")
    corrupted = bytearray(image_stream.getvalue())
    idat_offset = corrupted.index(b"IDAT")
    length = int.from_bytes(corrupted[idat_offset - 4 : idat_offset], "big")
    corrupted[idat_offset + 4 + length] ^= 1
    image_bytes = bytes(corrupted)
    with pytest.raises(SyntaxError, match="checksum"), Image.open(io.BytesIO(image_bytes)) as image:
        image.verify()
    encoded = base64.b64encode(image_bytes).decode()
    raw = (
        f'<p>private-corrupt-image-marker</p><img src="data:image/png;base64,{encoded}">'
    ).encode()
    storage = runtime.assets_for(actor)
    asset = storage.put_bytes(raw)
    kb = runtime.repo.create_kb(actor, "坏CRC导出验收")
    job = runtime.repo.submit_parse(
        actor, kb.kb_id, "corrupt-image.html", asset.asset_id, "bad-crc-upload"
    )
    result = finish_parse(runtime, actor, settings, job)
    assert job.document_id is not None
    assert len(result.assets) == 1 and storage.read(result.assets[0].asset_id) == image_bytes
    exports = tmp_path / "exports"
    exports.mkdir()

    def temporary_export(*, prefix: str) -> TemporaryDirectory[str]:
        """只将路由临时目录定位到本用例，真实导出和异常处理均不替换。"""
        return TemporaryDirectory(prefix=prefix, dir=exports)

    monkeypatch.setattr(parse_routes, "TemporaryDirectory", temporary_export)
    with TestClient(create_app(runtime)) as client:
        response = client.get(
            "/api/v2/documents/" + job.document_id + "/bundle?include_original=true",
            headers={"Authorization": "Bearer editor"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_asset"
        assert "修复" in response.json()["error"]["message"]
        assert "private-corrupt-image-marker" not in response.text
        assert str(tmp_path) not in response.text
        assert "IDAT" not in response.text and "checksum" not in response.text
        assert response.headers["content-type"] == "application/json"
        assert "content-disposition" not in response.headers
    assert not list(exports.iterdir())
    assert runtime.repo.get_document(actor, job.document_id).result_id == result.result_id
    assert storage.read(asset.asset_id) == raw
    assert storage.read(result.assets[0].asset_id) == image_bytes


def test_pg_upload_freezes_engine_and_reparse_keeps_choice(
    parse_runtime: tuple[Runtime, Principal, Settings],
) -> None:
    """真实HTTP与PG保持上传工具、幂等冲突、子进程输出和重解析身份一致。"""
    runtime, actor, settings = parse_runtime
    kb = runtime.repo.create_kb(actor, "解析工具验收")
    headers = {"Authorization": "Bearer editor", "Idempotency-Key": "engine-upload"}
    raw = b"# Policy\n\n| Item | Days |\n| --- | --- |\n| A | 7 |"
    url = f"/api/v2/knowledge-bases/{kb.kb_id}/documents?filename=policy.md"
    with TestClient(create_app(runtime)) as client:
        capability = client.get("/api/v2/capabilities", headers=headers).json()
        assert [item["id"] for item in capability["parse_engines"]] == ["builtin", "docling"]
        response = client.post(
            url + "&parser_engine=docling&ocr_enabled=false", content=raw, headers=headers
        )
        assert response.status_code == 202, response.text
        job = Job.model_validate_json(response.text)
        assert job.payload["parser_engine"] == "docling"
        assert job.payload["ocr_enabled"] is False
        changed_ocr = client.post(
            url + "&parser_engine=docling&ocr_enabled=true", content=raw, headers=headers
        )
        assert changed_ocr.status_code == 409
        unsupported = client.post(
            url + "&parser_engine=builtin&ocr_enabled=true",
            content=raw,
            headers={**headers, "Idempotency-Key": "builtin-ocr"},
        )
        assert unsupported.status_code == 415
        same = client.post(
            url + "&parser_engine=docling&ocr_enabled=false", content=raw, headers=headers
        )
        assert same.json()["job_id"] == job.job_id
        conflict = client.post(url + "&parser_engine=builtin", content=raw, headers=headers)
        assert conflict.status_code == 409
        unknown = client.post(
            url + "&parser_engine=unknown",
            content=raw,
            headers={**headers, "Idempotency-Key": "unknown-tool"},
        )
        assert unknown.status_code == 422
    result = finish_parse(runtime, actor, settings, job)
    assert result.engine.name == "docling" and result.document.tables
    assert job.document_id is not None
    current = runtime.repo.get_document(actor, job.document_id)
    rerun = runtime.repo.submit_reparse(actor, job.document_id, current.revision, "engine-reparse")
    assert rerun.payload["parser_engine"] == "docling"
    assert rerun.payload["ocr_enabled"] is False
    assert result.options.ocr_enabled is False
    assert finish_parse(runtime, actor, settings, rerun).result_id == result.result_id


def test_pg_import_preview_and_atomic_confirmation(
    parse_runtime: tuple[Runtime, Principal, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """经真实HTTP与PG验证预览归属、整批回滚、幂等及正式任务采用同一结果。"""
    runtime, actor, settings = parse_runtime

    @contextmanager
    def local_runtime(_settings: Settings, *, batch: bool) -> Generator[Runtime]:
        """后台入口共用测试资产存储，任务租约与完成仍在真实PG中执行。"""
        assert batch
        yield runtime

    monkeypatch.setattr(worker, "open_runtime", local_runtime)
    kb = runtime.repo.create_kb(actor, "导入清洗验收")
    runtime.repo.set_fields(
        actor,
        kb.kb_id,
        kb.revision,
        [MetadataField(name="department", label="部门", type="text", required=True)],
    )
    path = f"/api/v2/knowledge-bases/{kb.kb_id}/imports"
    auth = {"Authorization": "Bearer editor"}
    peer = {"Authorization": "Bearer peer", "Idempotency-Key": "private-preview"}
    image_stream = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(image_stream, format="PNG")
    encoded = base64.b64encode(image_stream.getvalue()).decode()
    raw = f'<p>Refund in seven days</p><img src="data:image/png;base64,{encoded}">'.encode()
    inputs = [("refund.html", raw), ("warranty.txt", b"Warranty for one year")]
    previews: list[Job] = []
    output_ids: list[str] = []
    with TestClient(create_app(runtime)) as client:
        for name, content in inputs:
            staged = client.post(
                path + "/staged", params={"filename": name}, content=content, headers=auth
            )
            assert staged.status_code == 201, staged.text
            body = {
                **staged.json(),
                "rules": {"engine": "builtin", "exclude_images": True, "ocr_enabled": False},
            }
            assert client.post(path + "/previews", json=body, headers=peer).status_code == 404
            headers = {**auth, "Idempotency-Key": "preview-" + name}
            accepted = client.post(path + "/previews", json=body, headers=headers)
            assert accepted.status_code == 202, accepted.text
            job = Job.model_validate_json(accepted.text)
            previews.append(job)
            repeated = client.post(path + "/previews", json=body, headers=headers)
            assert repeated.json()["job_id"] == job.job_id
            assert client.get(path + "/previews/" + job.job_id, headers=peer).status_code == 404
            assert client.get("/api/v2/jobs/" + job.job_id, headers=peer).status_code == 404
            worker.execute_job(settings, job.job_id)
            completed = runtime.repo.get_job(actor, job.job_id)
            assert completed.status == "succeeded", completed.error_code
            result_id = (completed.result or {}).get("result_id")
            assert isinstance(result_id, str)
            output_ids.append(result_id)
            rendered = client.get(path + "/previews/" + job.job_id, headers=auth)
            assert rendered.json()["status"] == "succeeded", rendered.text
            if name == "refund.html":
                assert rendered.json()["before"] != rendered.json()["text"]
                assert "Refund in seven days" in rendered.json()["text"]
        assert not runtime.repo.list_documents(actor, kb.kb_id)
        items: list[dict[str, JsonValue]] = [
            {"preview_id": previews[0].job_id, "metadata": {"department": "Support"}},
            {"preview_id": previews[1].job_id, "metadata": {}},
        ]
        rejected = client.post(path + "/confirm", json={"items": items}, headers=auth)
        assert rejected.status_code == 422, rejected.text
        assert not runtime.repo.list_documents(actor, kb.kb_id)
        items[1]["metadata"] = {"department": "Service"}
        confirmed = client.post(path + "/confirm", json={"items": items}, headers=auth)
        assert confirmed.status_code == 202, confirmed.text
        jobs = TypeAdapter(list[Job]).validate_json(confirmed.content)
        repeated = client.post(path + "/confirm", json={"items": items}, headers=auth)
        assert [item["job_id"] for item in repeated.json()] == [job.job_id for job in jobs]
        items[1]["metadata"] = {"department": "Changed"}
        assert (
            client.post(path + "/confirm", json={"items": items}, headers=auth).status_code == 409
        )

    for job, result_id, department in zip(jobs, output_ids, ["Support", "Service"], strict=True):
        assert job.document_id is not None
        worker.execute_job(settings, job.job_id)
        document = runtime.repo.get_document(actor, job.document_id)
        assert document.result_id == result_id and document.metadata == {"department": department}
        if department == "Service":
            assert document.selected_result_id == result_id
        assert document.indexed_result_id is None
        reparse = runtime.repo.submit_reparse(
            actor, job.document_id, document.revision, "reparse-" + job.job_id
        )
        assert reparse.payload["import_rules"] == job.payload["import_rules"]
        assert job.payload["ocr_enabled"] is False
        assert reparse.payload["ocr_enabled"] is False


@pytest.mark.parametrize("mode", ["parse", "rag", "combined"])
def test_execute_job_preserves_preview_processing_runtime(
    parse_runtime: tuple[Runtime, Principal, Settings], monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """HTTP冻结参数后通过真实execute_job领取/解析/切分/提交；装配边界按实际能力选择。"""
    from dataclasses import replace

    from llama_index.core.embeddings import MockEmbedding
    from opensearchpy import OpenSearch

    from inkrag.search import SearchBackend

    runtime, actor, settings = parse_runtime
    backend = SearchBackend(
        OpenSearch(["http://127.0.0.1:1"]),
        MockEmbedding(embed_dim=4),
        model_revision="job-preview-test",
        dimensions=4,
        replicas=0,
        max_tokens=128,
    )
    configured = settings.model_copy(update={"capabilities": mode})
    rich_runtime = replace(runtime, search=backend, mode="combined")
    api_runtime = runtime if mode == "parse" else rich_runtime
    requested_modes: list[str] = []

    @contextmanager
    def local_runtime(actual: Settings, *, batch: bool) -> Generator[Runtime]:
        """仅替换外部连接装配，保留真实PG、隔离解析与核心分块；按传入配置决定检索能力。"""
        assert batch
        requested_modes.append(actual.capabilities)
        yield runtime if actual.capabilities == "parse" else rich_runtime

    monkeypatch.setattr(worker, "open_runtime", local_runtime)
    kb = runtime.repo.create_kb(actor, "完整预览入口")
    path = f"/api/v2/knowledge-bases/{kb.kb_id}/imports"
    auth = {"Authorization": "Bearer editor"}
    rules: dict[str, JsonValue] = {"engine": "builtin"}
    if mode != "parse":
        rules["processing"] = {"chunk_size": 64, "chunk_overlap": 8, "include_title": True}
    with TestClient(create_app(api_runtime)) as client:
        staged = client.post(
            path + "/staged?filename=preview.txt",
            content=("Warranty evidence remains traceable. " * 90).encode(),
            headers=auth,
        )
        assert staged.status_code == 201
        accepted = client.post(
            path + "/previews",
            json={"stage_id": staged.json()["stage_id"], "rules": rules},
            headers={**auth, "Idempotency-Key": uuid4().hex},
        )
        assert accepted.status_code == 202
        job = Job.model_validate_json(accepted.content)
        worker.execute_job(configured, job.job_id)
        completed = runtime.repo.get_job(actor, job.job_id)
        assert completed.status == "succeeded", completed.error_code
        assert requested_modes == [mode]
        result = completed.result or {}
        if mode == "parse":
            assert result["chunk_count"] is None and result["effective_processing"] is None
        else:
            assert isinstance(result["chunk_count"], int) and result["chunk_count"] > 1
            assert result["max_tokens"] == 128
            effective = result["effective_processing"]
            assert isinstance(effective, dict) and effective["chunk_size"] == 64
        confirmation: dict[str, JsonValue] = {"items": [{"preview_id": job.job_id, "metadata": {}}]}
        confirmed = client.post(
            path + "/confirm",
            json=confirmation,
            headers=auth,
        )
        assert confirmed.status_code == 202
        parse_job = TypeAdapter(list[Job]).validate_json(confirmed.content)[0]
        worker.execute_job(configured, parse_job.job_id)
        assert runtime.repo.get_job(actor, parse_job.job_id).status == "succeeded"
        assert requested_modes == [mode, "parse"]
    backend.client.close()
