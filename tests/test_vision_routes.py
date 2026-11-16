"""真实 PG 和 HTTP 验收图意草稿、审校权限与并发版本边界。"""

import os
from base64 import b64encode
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from inkrag_platform.api import create_app
from inkrag_platform.local_model import LocalChatModel
from inkrag_platform.models import DomainError, Principal
from inkrag_platform.operations import DocumentView, Runtime
from inkrag_platform.parse_routes import StructureRevisionRequest, save_structure
from inkrag_platform.settings import Settings
from inkrag_platform.vision import VisionDraft, VisionSuggestion
from pydantic import JsonValue
from test_parsing_routes import finish_parse
from test_parsing_routes import parse_runtime as parse_runtime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("INKRAG_TEST_DATABASE_DSN"), reason="需显式独立PG测试库"),
]


def test_pg_vision_draft_has_exact_source_and_requires_review(
    parse_runtime: tuple[Runtime, Principal, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实际读取受控原图，草稿无写入，审校通过已有版本和资料授权提交。"""
    runtime, actor, settings = parse_runtime
    runtime = replace(runtime, vision_model=LocalChatModel("http://localhost:12347/v1", "fixture"))
    image = (
        Path(__file__).resolve().parents[1] / "assets/samples/vision/warehouse-flow.png"
    ).read_bytes()
    image_url = "data:image/png;base64," + b64encode(image).decode("ascii")
    raw = f'<h1>Warehouse intake</h1><img src="{image_url}">'.encode()
    kb = runtime.repo.create_kb(actor, "本地图意验收")
    source = runtime.assets_for(actor).put_bytes(raw)
    job = runtime.repo.submit_parse(
        actor, kb.kb_id, "warehouse.html", source.asset_id, "vision-fixture"
    )
    original = finish_parse(runtime, actor, settings, job)
    assert job.document_id is not None
    document_id = job.document_id
    assert len(original.assets) == 1
    binding = original.assets[0]
    payload = {"result_id": original.result_id, "element_ref": binding.element_ref}
    path = "/api/v2/documents/" + document_id
    headers = {"Authorization": "Bearer editor"}
    calls = 0
    output = "proposed"

    def complete(
        _model: LocalChatModel,
        messages: list[dict[str, JsonValue]],
        schema: dict[str, JsonValue],
        *,
        max_tokens: int,
    ) -> str:
        """只替换本机模型推理，校验实际发送的受控原图、结构和预算。"""
        nonlocal calls
        calls += 1
        content = messages[1]["content"]
        assert isinstance(content, list)
        assert {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}} in content
        assert schema["type"] == "object"
        assert max_tokens > 0
        if output == "invalid":
            return '{"description":"少了必填内容"}'
        if output == "busy":
            raise DomainError("local_model_busy", "本机模型繁忙")
        if output == "uncertain":
            return VisionDraft(
                status="uncertain",
                description="图像无法读清",
                relationships=[],
                uncertainties=["连接线模糊"],
            ).model_dump_json()
        if output == "concurrent":
            save_structure(
                runtime,
                actor,
                document_id,
                StructureRevisionRequest.model_validate(
                    {
                        "expected_result_id": original.result_id,
                        "operations": [
                            {
                                "kind": "picture_description",
                                "element_ref": binding.element_ref,
                                "text": "并发审核",
                            }
                        ],
                    }
                ),
            )
        return VisionDraft(
            status="proposed",
            description="仓储收货流程。",
            relationships=["封条不完整时转检查，之后隔离。"],
            uncertainties=[],
        ).model_dump_json()

    monkeypatch.setattr(LocalChatModel, "complete", complete)
    with TestClient(create_app(replace(runtime, vision_model=None))) as unconfigured_client:
        unavailable = unconfigured_client.post(path + "/vision", json=payload, headers=headers)
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "vision_unavailable"

    with TestClient(create_app(runtime)) as client:
        for token, expected in (("", 401), ("viewer", 403), ("alien", 404)):
            reject_headers = {"Authorization": "Bearer " + token} if token else {}
            rejected = client.post(path + "/vision", json=payload, headers=reject_headers)
            assert rejected.status_code == expected, rejected.text
        assert (
            client.post(
                path + "/vision",
                json={**payload, "asset_id": source.asset_id},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                path + "/vision",
                json={**payload, "element_ref": "#/pictures/999"},
                headers=headers,
            ).status_code
            == 404
        )
        assert calls == 0

        before = runtime.repo.get_document(actor, document_id)
        accepted = client.post(path + "/vision", json=payload, headers=headers)
        assert accepted.status_code == 200, accepted.text
        assert accepted.headers["cache-control"] == "no-store"
        suggestion = VisionSuggestion.model_validate_json(accepted.content)
        assert suggestion.source.model_dump() == {
            "document_id": document_id,
            "result_id": original.result_id,
            "element_ref": binding.element_ref,
            "asset_id": binding.asset_id,
            "page": binding.page,
        }
        assert suggestion.suggested_text == "仓储收货流程。\n封条不完整时转检查，之后隔离。"
        downloaded = client.get("/api/v2/assets/" + suggestion.source.asset_id, headers=headers)
        assert downloaded.status_code == 200 and downloaded.content == image
        assert runtime.repo.get_document(actor, document_id) == before

        output = "uncertain"
        uncertain = client.post(path + "/vision", json=payload, headers=headers)
        assert uncertain.status_code == 200, uncertain.text
        assert VisionSuggestion.model_validate_json(uncertain.content).suggested_text is None
        output = "invalid"
        invalid = client.post(path + "/vision", json=payload, headers=headers)
        assert (
            invalid.status_code == 502 and invalid.json()["error"]["code"] == "vision_model_invalid"
        )
        output = "busy"
        busy = client.post(path + "/vision", json=payload, headers=headers)
        assert busy.status_code == 429 and busy.json()["error"]["code"] == "local_model_busy"

        output = "concurrent"
        raced = client.post(path + "/vision", json=payload, headers=headers)
        assert raced.status_code == 409 and raced.json()["error"]["code"] == "version_conflict"
        stale = client.post(path + "/vision", json=payload, headers=headers)
        assert stale.status_code == 409
        assert calls == 5

        current = runtime.repo.get_document(actor, document_id)
        saved = client.patch(
            path + "/structure",
            json={
                "expected_result_id": current.result_id,
                "operations": [
                    {
                        "kind": "picture_description",
                        "element_ref": binding.element_ref,
                        "text": suggestion.suggested_text,
                    }
                ],
            },
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        view = DocumentView.model_validate_json(saved.content)
        assert view.result is not None and view.result.editor is not None
        assert (
            view.result.editor.picture_descriptions[binding.element_ref]
            == suggestion.suggested_text
        )
        assert view.result.result_id != original.result_id

        excluded = client.patch(
            path + "/structure",
            json={
                "expected_result_id": view.result.result_id,
                "operations": [
                    {"kind": "exclude", "element_ref": binding.element_ref, "excluded": True}
                ],
            },
            headers=headers,
        )
        assert excluded.status_code == 200, excluded.text
        excluded_view = DocumentView.model_validate_json(excluded.content)
        rejected = client.post(
            path + "/vision",
            json={**payload, "result_id": excluded_view.document.result_id},
            headers=headers,
        )
        assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "vision_excluded"
        assert calls == 5
