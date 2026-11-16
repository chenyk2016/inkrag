"""显式本地完整栈验收：真实OAuth/HTTP/PG/Rabbit/S3/隔离解析/TEI/OpenSearch。"""

import os
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
import pytest
from inkrag_platform.models import Document, Evaluation, IssuedKey, Job, KnowledgeBase, Release
from inkrag_platform.operations import DocumentView
from pydantic import BaseModel, ConfigDict, JsonValue, SecretStr, TypeAdapter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("INKRAG_STACK_INTEGRATION") != "1", reason="需显式启用本地完整栈"
    ),
]


class LocalAccess(BaseModel):
    """测试只使用本地初始化文件，密码禁止进入repr或证据报告。"""

    model_config = ConfigDict(strict=True, extra="forbid")
    url: str
    username: str
    password: SecretStr


def local_access() -> LocalAccess:
    """读取本次明确选中的本地测试栈，校验目标仍是环回地址。"""
    state = Path(os.environ.get("INKRAG_STACK_STATE_DIR", ".inkrag"))
    access = LocalAccess.model_validate_json((state / "access.json").read_bytes())
    assert urlsplit(access.url).hostname in {"127.0.0.1", "localhost"}, "仅允许显式本地测试栈"
    return access


class ReleaseResponse(BaseModel):
    """发布响应保留服务端权威身份。"""

    release: Release


class KeyResponse(BaseModel):
    """仅签发响应可读取一次凭证；测试不将其打印或保存。"""

    key: IssuedKey


class EvaluationList(BaseModel):
    """只接受已完成评测记录，不生成演示指标。"""

    items: list[Evaluation]


def body(response: httpx.Response) -> dict[str, JsonValue]:
    """收窄未知JSON，避免测试以结构不完整响应继续运行。"""
    return TypeAdapter(dict[str, JsonValue]).validate_json(response.content)


def request(
    api: httpx.Client,
    method: str,
    path: str,
    data: dict[str, JsonValue] | None = None,
    *,
    expected: int = 200,
) -> httpx.Response:
    """只记录安全协议事实，失败消息不回显令牌、正文和存储配置。"""
    response = api.request(method, path, json=data, headers={"Idempotency-Key": uuid4().hex})
    if response.status_code != expected:
        pytest.fail(f"{method} {path} HTTP {response.status_code}, expected {expected}")
    return response


def wait_job(api: httpx.Client, job: Job, seconds: int = 240) -> Job:
    """轮询真实PG任务，有限截止时间；后台失败不会被当成零结果。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        current = Job.model_validate_json(request(api, "GET", f"/api/v2/jobs/{job.job_id}").content)
        if current.status == "succeeded":
            return current
        if current.status in {"failed", "cancelled"}:
            pytest.fail(f"{job.kind} task {current.status}: {current.error_code}")
        time.sleep(0.5)
    pytest.fail(f"{job.kind} task exceeded {seconds}s")


def build_and_publish(api: httpx.Client, kb_id: str, current_release_id: str | None) -> Release:
    """后台构建成功仍需独立CAS发布，调用者无法指定物理索引。"""
    job = Job.model_validate_json(
        request(api, "POST", f"/api/v2/knowledge-bases/{kb_id}/builds", expected=202).content
    )
    completed = wait_job(api, job)
    assert completed.result is not None
    build_id = completed.result.get("build_id")
    assert isinstance(build_id, str)
    preview = body(request(api, "GET", f"/api/v2/builds/{build_id}/preview"))
    assert preview.get("publishable") is True
    assert preview.get("expected_release_id") == current_release_id
    return ReleaseResponse.model_validate_json(
        request(
            api,
            "POST",
            f"/api/v2/builds/{build_id}/publish",
            {"expected_release_id": current_release_id},
        ).content
    ).release


def test_complete_platform_user_journey() -> None:
    """覆盖异步解析/版本/发布隔离/三通道/过滤/凭证/评测/删除后恢复。"""
    access = local_access()
    with httpx.Client(base_url=access.url, timeout=60, trust_env=False) as api:
        request(api, "GET", "/health/ready")
        config = body(request(api, "GET", "/api/v2/session/config"))
        issuer, client_id = config.get("issuer"), config.get("client_id")
        assert isinstance(issuer, str) and isinstance(client_id, str)
        with httpx.Client(timeout=30, trust_env=False) as identity:
            response = identity.post(
                issuer + "/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": client_id,
                    "username": access.username,
                    "password": access.password.get_secret_value(),
                },
            )
            assert response.status_code == 200
            token = body(response).get("access_token")
            assert isinstance(token, str)
        api.headers["Authorization"] = "Bearer " + token
        kb = KnowledgeBase.model_validate_json(
            request(
                api,
                "POST",
                "/api/v2/knowledge-bases",
                {"name": "完整栈验收-" + uuid4().hex[:8]},
                expected=201,
            ).content
        )
        kb_path = f"/api/v2/knowledge-bases/{kb.kb_id}"
        kb = KnowledgeBase.model_validate_json(
            request(
                api,
                "PUT",
                kb_path + "/fields",
                {
                    "expected_revision": kb.revision,
                    "fields": [
                        {
                            "name": "department",
                            "label": "部门",
                            "type": "text",
                            "required": True,
                            "options": [],
                        }
                    ],
                },
            ).content
        )
        original = (
            "# 恒星设备说明\n\n恒星设备的保修期限为二十四个月。\n\n设备型号K900，故障时联系售后。\n\n验收批次："
            + uuid4().hex
        )
        uploaded = api.post(
            kb_path + "/documents?filename=manual.md",
            content=original.encode(),
            headers={
                "Content-Type": "application/octet-stream",
                "Idempotency-Key": uuid4().hex,
                "X-Ink-Metadata": quote('{"department":"财务"}', safe=""),
            },
        )
        assert uploaded.status_code == 202
        parsed_job = wait_job(api, Job.model_validate_json(uploaded.content))
        assert parsed_job.document_id
        doc_path = f"/api/v2/documents/{parsed_job.document_id}"
        view = DocumentView.model_validate_json(request(api, "GET", doc_path).content)
        assert view.result is not None and view.result.source.asset_id
        initial_result_id = view.result.result_id
        source_asset_id = view.result.source.asset_id
        asset_path = f"/api/v2/assets/{source_asset_id}"
        assert request(api, "GET", asset_path).content == original.encode()
        request(
            api, "POST", "/api/v2/retrieve", {"kb_id": kb.kb_id, "query": "保修期限"}, expected=409
        )
        request(
            api,
            "PUT",
            doc_path + "/selection",
            {"expected_document_revision": view.document.revision, "result_id": initial_result_id},
        )
        release = build_and_publish(api, kb.kb_id, None)
        request(
            api,
            "POST",
            f"/api/v2/builds/{release.build_id}/publish",
            {"expected_release_id": None},
            expected=409,
        )
        for mode in ("keyword", "vector", "hybrid"):
            found = body(
                request(
                    api,
                    "POST",
                    "/api/v2/retrieve",
                    {
                        "kb_id": kb.kb_id,
                        "query": "恒星设备保修期限",
                        "mode": mode,
                        "top_k": 5,
                        "filters": [{"field": "department", "operator": "eq", "value": "财务"}],
                    },
                )
            )
            hits = found.get("hits")
            assert isinstance(hits, list) and hits
            assert found.get("release_id") == release.release_id
        request(
            api,
            "POST",
            "/api/v2/retrieve",
            {
                "kb_id": kb.kb_id,
                "query": "保修期限",
                "mode": "keyword",
                "filters": [{"field": "missing_field", "operator": "eq", "value": "财务"}],
            },
            expected=422,
        )
        request(
            api,
            "POST",
            "/api/v2/retrieve",
            {
                "kb_id": kb.kb_id,
                "query": "保修期限",
                "mode": "keyword",
                "filters": [{"field": "department", "operator": "raw_dsl", "value": "财务"}],
            },
            expected=422,
        )
        empty = body(
            request(
                api,
                "POST",
                "/api/v2/retrieve",
                {
                    "kb_id": kb.kb_id,
                    "query": "保修期限",
                    "mode": "keyword",
                    "filters": [{"field": "department", "operator": "eq", "value": "研发"}],
                },
            )
        )
        assert empty.get("hits") == []
        element = next(text for text in view.result.document.texts if "二十四" in text.text)
        revised = DocumentView.model_validate_json(
            request(
                api,
                "PATCH",
                doc_path,
                {
                    "expected_result_id": initial_result_id,
                    "changes": [
                        {
                            "element_ref": element.self_ref,
                            "text": "恒星设备的保修期限调整为四十八个月。",
                        }
                    ],
                },
            ).content
        )
        assert revised.result is not None and revised.result.result_id != initial_result_id
        old = body(
            request(
                api,
                "POST",
                "/api/v2/retrieve",
                {"kb_id": kb.kb_id, "query": "保修期限", "mode": "keyword"},
            )
        )
        assert old.get("release_id") == release.release_id
        old_hits = old.get("hits")
        assert isinstance(old_hits, list) and any(
            isinstance(hit, dict)
            and isinstance(hit.get("text"), str)
            and "二十四" in str(hit["text"])
            for hit in old_hits
        )
        assert request(api, "GET", asset_path).content == original.encode()
        stale: dict[str, JsonValue] = {
            "expected_result_id": initial_result_id,
            "changes": [{"element_ref": element.self_ref, "text": "过期写入"}],
        }
        request(api, "PATCH", doc_path, stale, expected=409)
        ingest = Job.model_validate_json(
            request(
                api,
                "POST",
                doc_path + "/ingest",
                {
                    "expected_result_id": revised.result.result_id,
                    "expected_document_revision": revised.document.revision,
                },
                expected=202,
            ).content
        )
        done = wait_job(api, ingest)
        assert done.result is not None and isinstance(done.result.get("build_id"), str)
        second = ReleaseResponse.model_validate_json(
            request(
                api,
                "POST",
                f"/api/v2/builds/{done.result['build_id']}/publish",
                {"expected_release_id": release.release_id},
            ).content
        ).release
        issued = KeyResponse.model_validate_json(
            request(
                api, "POST", kb_path + "/callers", {"name": "完整栈只读调用方"}, expected=201
            ).content
        ).key
        with httpx.Client(
            base_url=access.url,
            headers={"Authorization": "Bearer " + issued.token},
            timeout=60,
            trust_env=False,
        ) as caller:
            request(
                caller,
                "POST",
                "/api/v2/retrieve",
                {"kb_id": kb.kb_id, "query": "保修期限", "mode": "keyword"},
            )
            request(caller, "POST", "/api/v2/knowledge-bases", {"name": "禁止创建"}, expected=403)
            request(api, "DELETE", f"/api/v2/callers/{issued.key_id}", expected=204)
            request(caller, "GET", kb_path + "/documents", expected=401)
        request(
            api,
            "POST",
            kb_path + "/samples",
            {"query": "恒星设备保修期限", "expected_document_ids": [parsed_job.document_id]},
            expected=201,
        )
        evaluation = Job.model_validate_json(
            request(
                api,
                "POST",
                kb_path + "/evaluations",
                {
                    "release_id": second.release_id,
                    "name": "真实检索回归",
                    "top_k": 5,
                    "mode": "keyword",
                },
                expected=202,
            ).content
        )
        wait_job(api, evaluation)
        evaluations = EvaluationList.model_validate_json(
            request(api, "GET", kb_path + "/evaluations").content
        )
        assert evaluations.items and evaluations.items[0].metrics.get("recall") == 1.0
        request(
            api,
            "POST",
            kb_path + "/samples",
            {
                "query": "NoMatchingTerm" + uuid4().hex,
                "expected_document_ids": [parsed_job.document_id],
            },
            expected=201,
        )
        mixed = Job.model_validate_json(
            request(
                api,
                "POST",
                kb_path + "/evaluations",
                {
                    "release_id": second.release_id,
                    "name": "保留零命中样本的指标回归",
                    "top_k": 1,
                    "mode": "keyword",
                },
                expected=202,
            ).content
        )
        wait_job(api, mixed)
        mixed_metrics = (
            EvaluationList.model_validate_json(
                request(api, "GET", kb_path + "/evaluations").content
            )
            .items[0]
            .metrics
        )
        assert mixed_metrics["sample_count"] == 2
        assert all(mixed_metrics[key] == 0.5 for key in ("recall", "precision", "mrr"))
        current = DocumentView.model_validate_json(request(api, "GET", doc_path).content).document
        removed = Document.model_validate_json(
            request(api, "DELETE", f"{doc_path}?expected_revision={current.revision}").content
        )
        assert removed.deleted
        request(api, "GET", asset_path, expected=404)
        after_delete = body(
            request(
                api,
                "POST",
                "/api/v2/retrieve",
                {"kb_id": kb.kb_id, "query": "保修期限", "mode": "keyword"},
            )
        )
        assert after_delete.get("release_id") == second.release_id
        assert after_delete.get("hits") == []
        request(
            api,
            "POST",
            f"/api/v2/releases/{release.release_id}/rollback",
            {"expected_release_id": second.release_id},
            expected=409,
        )
        recovered = build_and_publish(api, kb.kb_id, second.release_id)
        found = body(
            request(
                api,
                "POST",
                "/api/v2/retrieve",
                {"kb_id": kb.kb_id, "query": "保修期限", "mode": "keyword"},
            )
        )
        assert found.get("release_id") == recovered.release_id and found.get("hits") == []


def test_declared_formats_across_real_isolated_worker(tmp_path: Path) -> None:
    """七种补充格式经过真实隔离worker和资产，再共同构建检索，不冒充复杂样本质量评估。"""
    import base64
    import io

    from docx import Document as WordDocument
    from inkrag_platform.operations import EvaluationHit
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    access = local_access()
    sentinel = "formatacceptance" + uuid4().hex[:8]
    bitmap = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(bitmap, format="PNG")
    picture = bitmap.getvalue()
    encoded = base64.b64encode(picture).decode()
    inputs: dict[str, bytes] = {
        "manual.txt": sentinel.encode(),
        "data.json": ('{"policy":"' + sentinel + '","enabled":true,"empty":null}').encode(),
        "data.xml": ("<policy><description>" + sentinel + "</description></policy>").encode(),
        "data.csv": ("policy,term\n" + sentinel + ",24\n").encode(),
        "manual.html": (
            "<html><body><h1>"
            + sentinel
            + '</h1><table><tr><th>流程</th></tr><tr><td>独立解析</td></tr></table><img src="data:image/png;base64,'
            + encoded
            + '"></body></html>'
        ).encode(),
    }
    word = WordDocument()
    word.add_heading(sentinel, 1)
    word.add_paragraph("独立文档解析贯穿验收。")
    word.add_picture(io.BytesIO(picture))
    word_path = tmp_path / "manual.docx"
    word.save(str(word_path))
    inputs[word_path.name] = word_path.read_bytes()
    slides = Presentation()
    slide = slides.slides.add_slide(slides.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1)).text_frame.text = sentinel
    slide.shapes.add_picture(io.BytesIO(picture), Inches(1), Inches(2), width=Inches(1))
    slides_path = tmp_path / "slides.pptx"
    slides.save(str(slides_path))
    inputs[slides_path.name] = slides_path.read_bytes()
    with httpx.Client(base_url=access.url, timeout=60, trust_env=False) as api:
        config = body(request(api, "GET", "/api/v2/session/config"))
        issuer, client_id = config.get("issuer"), config.get("client_id")
        assert isinstance(issuer, str) and isinstance(client_id, str)
        with httpx.Client(timeout=30, trust_env=False) as identity:
            response = identity.post(
                issuer + "/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": client_id,
                    "username": access.username,
                    "password": access.password.get_secret_value(),
                },
            )
            assert response.status_code == 200
            token = body(response).get("access_token")
            assert isinstance(token, str)
        api.headers["Authorization"] = "Bearer " + token
        kb = KnowledgeBase.model_validate_json(
            request(
                api,
                "POST",
                "/api/v2/knowledge-bases",
                {"name": "多格式隔离验收-" + uuid4().hex[:8]},
                expected=201,
            ).content
        )
        jobs: list[tuple[str, Job]] = []
        for filename, raw in inputs.items():
            response = api.post(
                f"/api/v2/knowledge-bases/{kb.kb_id}/documents?filename={filename}",
                content=raw,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Idempotency-Key": uuid4().hex,
                },
            )
            assert response.status_code == 202
            jobs.append((filename, Job.model_validate_json(response.content)))
        expected_ids: set[str] = set()
        for filename, job in jobs:
            complete = wait_job(api, job)
            assert complete.document_id
            expected_ids.add(complete.document_id)
            view = DocumentView.model_validate_json(
                request(api, "GET", f"/api/v2/documents/{complete.document_id}").content
            )
            assert view.result and view.result.source.asset_id
            request(
                api,
                "PUT",
                f"/api/v2/documents/{complete.document_id}/selection",
                {
                    "expected_document_revision": view.document.revision,
                    "result_id": view.result.result_id,
                },
            )
            format_name = "text" if filename.endswith(".txt") else filename.rsplit(".", 1)[1]
            assert view.result.source.format == format_name
            assert (
                request(api, "GET", f"/api/v2/assets/{view.result.source.asset_id}").content
                == inputs[filename]
            )
            if format_name in {"html", "docx", "pptx"}:
                assert view.result.assets
                assert (
                    request(api, "GET", f"/api/v2/assets/{view.result.assets[0].asset_id}").content
                    == picture
                )
            if format_name == "csv":
                assert view.result.document.tables and not view.result.document.texts
        build_and_publish(api, kb.kb_id, None)
        found = body(
            request(
                api,
                "POST",
                "/api/v2/retrieve",
                {"kb_id": kb.kb_id, "query": sentinel, "top_k": 100, "mode": "keyword"},
            )
        )
        hits = TypeAdapter(list[EvaluationHit]).validate_python(found.get("hits"))
        actual = {
            identity for hit in hits if isinstance(identity := hit.metadata.get("document_id"), str)
        }
        assert actual == expected_ids
