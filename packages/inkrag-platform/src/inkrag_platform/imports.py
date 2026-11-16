"""文件暂存、隔离预览和批量确认。预览产物在正式任务中按摘要复用。"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from time import time
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from inkparse import ParseError, ParseOptions, ParseResult, save_stored_result
from inkparse.adapters import ParserEngine
from inkparse.editing import ExcludeOperation, effective_document, revise_structure
from inkparse.io import render_markdown
from inkparse.models import ParsedPayload, fingerprint
from inkstore import AssetId, StorageError, StoredRecord
from pydantic import Field, JsonValue
from starlette.concurrency import run_in_threadpool

from .models import DomainError, Job, Principal
from .operations import Runtime, StrictModel, check_parser_input
from .parser_rules import validate_parser_rules
from .processing import normalize_processing, preview_chunks
from .sandbox import run_parse
from .settings import Settings

STAGE_TTL = 24 * 60 * 60
IMPORT_BYTES = 20 * 1024 * 1024
ImportFile = tuple[str, str, str, str, dict[str, JsonValue], dict[str, JsonValue], int]


class StagedFile(StrictModel):
    """绑定上传者、目标知识库和原件摘要，按过期时间限制后续使用。"""

    owner: str
    kb_id: str
    filename: str
    asset_id: AssetId
    expires_at: int


class ImportRules(StrictModel):
    """清洗规则只作用于解析后的有效正文，保留原件及稳定来源。"""

    engine: ParserEngine
    ocr_enabled: bool | None = None
    ocr_mode: Literal["auto", "full_page"] | None = None
    table_mode: Literal["fast", "accurate"] | None = None
    table_structure: bool | None = None
    processing: dict[str, JsonValue] | None = None
    exclude_headers: bool = False
    exclude_images: bool = False


class PreviewInput(StrictModel):
    """预览明确指定暂存原件和一份规则。"""

    stage_id: AssetId
    rules: ImportRules


class ImportItem(StrictModel):
    """确认引用一次已完成的预览，并附这份资料的属性。"""

    preview_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    metadata: dict[str, JsonValue]


class ImportBatch(StrictModel):
    """每批限制资料数，任一项失败则不登记本批资料。"""

    items: list[ImportItem] = Field(min_length=1, max_length=20)


def _authorized(runtime: Runtime, principal: Principal, kb_id: str) -> None:
    """检查当前库的可写范围，暂存和预览均需要编辑权限。"""
    runtime.repo.get_kb(principal, kb_id)
    if not {"admin", "editor"}.intersection(principal.roles):
        raise DomainError("forbidden", "当前账号没有导入权限")


def _stage_key(principal: Principal, kb_id: str, stage_id: str) -> str:
    """从可信租户、库与上传者派生记录地址，外部 ID 无法直接访问普通结果。"""
    identity = ["import-stage.v1", principal.tenant_id, kb_id, principal.subject, stage_id]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _stage(runtime: Runtime, principal: Principal, kb_id: str, stage_id: str) -> StagedFile:
    """先确定上传者专属地址，再读取记录；缺失与无权使用采用同一错误。"""
    try:
        record = runtime.assets_for(principal).load_record(_stage_key(principal, kb_id, stage_id))
    except StorageError as exc:
        if exc.code in {"missing_record", "not_found"}:
            raise DomainError("not_found", "暂存文件不可用，请重新添加文件") from None
        raise
    stage = StagedFile.model_validate_json(record.payload)
    if (stage.owner, stage.kb_id, stage.asset_id) != (
        principal.subject,
        kb_id,
        record.source_asset_id,
    ):
        raise DomainError("not_found", "暂存文件不可用，请重新添加文件")
    return stage


def _save_stage(
    runtime: Runtime, principal: Principal, kb_id: str, filename: str, raw: bytes
) -> str:
    """保存原件和不可变暂存记录，正式资料在确认时登记。"""
    storage = runtime.assets_for(principal)
    asset = storage.put_bytes(raw)
    identity = uuid4().hex + uuid4().hex
    stage = StagedFile(
        owner=principal.subject,
        kb_id=kb_id,
        filename=filename,
        asset_id=asset.asset_id,
        expires_at=int(time()) + STAGE_TTL,
    )
    storage.save_record(
        StoredRecord(
            record_id=_stage_key(principal, kb_id, identity),
            source_asset_id=asset.asset_id,
            payload=stage.model_dump_json(),
        )
    )
    return identity


def clean_result(result: ParseResult, rules: ImportRules) -> ParseResult:
    """复用现有排除操作处理页眉页脚或图片，首次导入结果从版本一开始。"""
    refs = [
        item.self_ref
        for item in result.document.texts
        if rules.exclude_headers and item.label in {"page_header", "page_footer"}
    ]
    if rules.exclude_images:
        refs.extend(item.self_ref for item in result.document.pictures)
    if not refs:
        return result
    for start in range(0, len(refs), 1000):
        result = revise_structure(
            result,
            [
                ExcludeOperation(kind="exclude", element_ref=ref, excluded=True)
                for ref in refs[start : start + 1000]
            ],
        )
    values = result.model_dump(exclude={"result_id"})
    values.update(revision=1, previous_result_id=None)
    payload = ParsedPayload.model_validate(values)
    return ParseResult.model_validate({**payload.model_dump(), "result_id": fingerprint(payload)})


def execute_preview(runtime: Runtime, settings: Settings, job: Job) -> dict[str, JsonValue]:
    """使用与正式解析相同的隔离执行器，保存确认时会采用的完整清洗结果。"""
    owner = job.payload.get("owner")
    if not isinstance(owner, str):
        raise DomainError("invalid_job", "预览任务缺少上传者")
    principal = Principal(
        tenant_id=job.tenant_id, subject=owner, roles=["editor"], kb_ids=[job.kb_id]
    )
    request = PreviewInput.model_validate(
        {"stage_id": job.payload.get("stage_id"), "rules": job.payload.get("rules")}
    )
    stage = StagedFile.model_validate(job.payload.get("source"))
    if stage.expires_at <= time():
        raise ParseError("expired_import", "暂存文件已过期，请重新添加")
    storage = runtime.assets_for(principal)
    original = run_parse(
        storage.read(stage.asset_id),
        stage.filename,
        storage,
        settings,
        ParseOptions.model_validate(
            {
                "engine": request.rules.engine,
                "ocr_enabled": request.rules.ocr_enabled,
                **{
                    key: value
                    for key, value in request.rules.model_dump().items()
                    if key in {"ocr_mode", "table_mode", "table_structure"} and value is not None
                },
            }
        ),
    )
    result = clean_result(original, request.rules)
    document = effective_document(result)
    text = render_markdown(result, document)
    if not text.strip():
        raise ParseError("empty_import", "清洗后没有可用正文，请调整规则")
    save_stored_result(result, storage)
    before = render_markdown(original, effective_document(original)) if original != result else text
    blocks: list[JsonValue] = [
        {"kind": item.label.value, "text": item.text[:500]}
        for item in document.texts
        if result.editor is None or item.self_ref not in result.editor.excluded_refs
    ]
    return {
        **preview_chunks(
            runtime, job.tenant_id, job.kb_id, job.job_id, result, request.rules.processing
        ),
        "result_id": result.result_id,
        "text": text[:16000],
        "before": before[:16000],
        "blocks": blocks[:30],
        "truncated": len(text) > 16000 or len(before) > 16000,
        "warnings": [warning.model_dump(mode="json") for warning in result.warnings[:20]],
    }


def _preview_job(runtime: Runtime, principal: Principal, kb_id: str, job_id: str) -> Job:
    """只有发起人可以用预览读取内容或确认导入。"""
    job = runtime.repo.get_job(principal, job_id)
    if (
        job.kind != "import_preview"
        or job.kb_id != kb_id
        or job.payload.get("owner") != principal.subject
    ):
        raise DomainError("not_found", "预览任务不存在")
    return job


def _preview_content(job: Job) -> dict[str, JsonValue]:
    """预览只读有界任务摘要，轮询无需重新读取原件或资产。"""
    if job.status != "succeeded":
        return {"status": job.status, "error": job.error_diagnostic, "error_code": job.error_code}
    stage = StagedFile.model_validate(job.payload.get("source"))
    if stage.expires_at <= time():
        raise DomainError("invalid_request", "预览已过期，请重新添加文件")
    result = dict(job.result or {})
    result.pop("result_id", None)
    return {**result, "status": job.status}


def _confirm(runtime: Runtime, principal: Principal, kb_id: str, batch: ImportBatch) -> list[Job]:
    """校验所有暂存与预览归属，再原子接受整批解析结果登记任务。"""
    inputs: list[ImportFile] = []
    for item in batch.items:
        job = _preview_job(runtime, principal, kb_id, item.preview_id)
        stage_id = job.payload.get("stage_id")
        result_id = (job.result or {}).get("result_id")
        if (
            job.status != "succeeded"
            or not isinstance(stage_id, str)
            or not isinstance(result_id, str)
        ):
            raise DomainError("not_ready", "请先完成本批资料的预览")
        stage = StagedFile.model_validate(job.payload.get("source"))
        rules = ImportRules.model_validate(job.payload.get("rules"))
        inputs.append(
            (
                stage_id,
                stage.filename,
                stage.asset_id,
                result_id,
                item.metadata,
                rules.model_dump(mode="json"),
                stage.expires_at,
            )
        )
    if len({item[0] for item in inputs}) != len(inputs):
        raise DomainError("invalid_request", "同一暂存文件不能重复确认")
    return runtime.repo.confirm_import(principal, kb_id, inputs)


def build_import_router(
    runtime: Runtime,
    principal_dependency: Callable[..., Principal | Awaitable[Principal]],
) -> APIRouter:
    """注册暂存、排队预览和确认入口，长解析由现有Worker执行。"""
    router = APIRouter(prefix="/api/v2/knowledge-bases/{kb_id}/imports")

    @router.post("/staged", status_code=201)
    async def upload_stage(
        kb_id: str,
        request: Request,
        principal: Annotated[Principal, Depends(principal_dependency)],
        filename: Annotated[
            str, Query(min_length=1, max_length=255, pattern=r"^[^/\\\x00-\x1f]+$")
        ],
    ) -> dict[str, str]:
        """暂存受限文件，实际请求体大小仍由ASGI限制。"""
        await run_in_threadpool(_authorized, runtime, principal, kb_id)
        raw = await request.body()
        if not raw or len(raw) > IMPORT_BYTES or filename in {".", ".."}:
            raise HTTPException(422, "文件为空、超过20MiB或文件名无效")
        identity = await run_in_threadpool(_save_stage, runtime, principal, kb_id, filename, raw)
        return {"stage_id": identity}

    @router.post("/previews", status_code=202)
    async def create_preview(
        kb_id: str,
        value: PreviewInput,
        principal: Annotated[Principal, Depends(principal_dependency)],
        idempotency_key: Annotated[str, Header(min_length=8, max_length=128)],
    ) -> Job:
        """冻结规则与暂存身份后进入持久队列。"""
        await run_in_threadpool(_authorized, runtime, principal, kb_id)
        stage = await run_in_threadpool(_stage, runtime, principal, kb_id, value.stage_id)
        if stage.expires_at <= time():
            raise DomainError("invalid_request", "暂存文件已过期，请重新添加")
        await run_in_threadpool(
            check_parser_input, runtime, value.rules.engine, stage.filename, value.rules.ocr_enabled
        )
        normalized = normalize_processing(runtime, value.rules.processing)
        validate_parser_rules(
            value.rules.engine, stage.filename, value.rules.model_dump(mode="json")
        )
        rules = value.rules.model_dump(mode="json")
        rules["processing"] = normalized
        payload: dict[str, JsonValue] = {
            "stage_id": value.stage_id,
            "rules": rules,
            "owner": principal.subject,
            "source": stage.model_dump(mode="json"),
        }
        return await run_in_threadpool(
            runtime.repo.accept_job,
            principal,
            "import_preview",
            kb_id,
            None,
            payload,
            idempotency_key,
        )

    @router.get("/previews/{job_id}")
    async def preview(
        kb_id: str, job_id: str, principal: Annotated[Principal, Depends(principal_dependency)]
    ) -> dict[str, JsonValue]:
        """返回本人的预览状态及有效正文样例。"""
        await run_in_threadpool(_authorized, runtime, principal, kb_id)
        job = await run_in_threadpool(_preview_job, runtime, principal, kb_id, job_id)
        return _preview_content(job)

    @router.post("/confirm", status_code=202)
    async def confirm(
        kb_id: str,
        batch: ImportBatch,
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> list[Job]:
        """确认整批暂存原件与属性，沿用后台解析任务的事务提交。"""
        await run_in_threadpool(_authorized, runtime, principal, kb_id)
        return await run_in_threadpool(_confirm, runtime, principal, kb_id, batch)

    return router
