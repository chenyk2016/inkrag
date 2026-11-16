"""固定候选/发布的真实试查与逐题评测；可观测事实不依赖搜索别名。"""

import time
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter
from sqlalchemy import func, select

from .database import CandidateEvaluationRow, SampleRow
from .models import Build, DomainError, EvaluationSample, ExpectedSource, Job, Principal
from .operations import Runtime
from .repository import require_role
from .workspace import evaluation_value, record_trace
from .workspace_models import (
    CandidateEvaluation,
    ServiceConfig,
    TrialRequest,
)


def _effective(
    config: ServiceConfig, principal: Principal, request: TrialRequest, *, enforce_callers: bool
) -> tuple[Literal["hybrid", "vector", "keyword"], int, list[dict[str, JsonValue]]]:
    """调用者只能收窄业务条件，服务固定范围与可信租户范围始终保留。"""
    if (
        enforce_callers
        and config.allowed_callers
        and principal.subject not in config.allowed_callers
    ):
        raise DomainError("forbidden", "当前调用方没有此服务访问权限")
    if (request.model_fields_set - {"query", "diagnostics"}) - set(config.allowed_fields):
        raise DomainError("forbidden", "请求使用了服务未开放的字段")
    if config.allowed_filter_fields is not None and any(
        item.field not in config.allowed_filter_fields for item in (request.filters or [])
    ):
        raise DomainError("forbidden", "请求使用了服务未开放的筛选字段")
    filters = [item.model_dump(mode="json") for item in config.fixed_filters]
    filters.extend(item.model_dump(mode="json") for item in (request.filters or []))
    if len(filters) > 20:
        raise DomainError("invalid_filter", "固定范围和动态条件合计最多20项")
    return request.mode or config.mode, request.top_k or config.top_k, filters


def _candidate(runtime: Runtime, principal: Principal, build_id: str) -> Build:
    """未发布候选必须可读、就绪且授权投影仍有效。"""
    build = runtime.repo.get_build(principal, build_id)
    runtime.repo.get_kb(principal, build.kb_id)
    if build.status != "ready":
        raise DomainError("not_ready", "候选尚未构建完成")
    return build


def _sources(response: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    """严格验证来源身份，并返回无正文片段的细粒度来源投影。"""
    values = response.get("hits")
    if not isinstance(values, list):
        raise DomainError("invalid_search_result", "检索返回缺少命中列表")
    sources: list[dict[str, JsonValue]] = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
            raise DomainError("invalid_search_result", "检索命中缺少可核验的来源")
        metadata = value["metadata"]
        if not isinstance(metadata, dict):
            raise DomainError("invalid_search_result", "来源格式无效")
        identity = metadata.get("document_id")
        if not isinstance(identity, str) or not identity:
            raise DomainError("invalid_search_result", "检索命中缺少资料身份")
        origin = metadata.get("parse_origin")
        source: dict[str, JsonValue] = {"document_id": identity, "page": metadata.get("page")}
        if isinstance(origin, dict):
            source.update(
                {"result_id": origin.get("result_id"), "element_ref": origin.get("element_ref")}
            )
        sources.append(source)
    return sources


def _source_ids(response: dict[str, JsonValue]) -> list[str]:
    """文档级指标按第一次出现去重，保持检索实际排名。"""
    return list(
        dict.fromkeys(
            TypeAdapter(str).validate_python(source["document_id"], strict=True)
            for source in _sources(response)
        )
    )


def _matches(expected: ExpectedSource, actual: dict[str, JsonValue]) -> bool:
    """资料、结果、元素和页码是同时满足的人工条件，不能用同文档近似替代。"""
    return all(
        value is None or actual.get(key) == value
        for key, value in expected.model_dump(mode="json").items()
    )


def query_target(
    runtime: Runtime,
    principal: Principal,
    target_id: str,
    request: TrialRequest,
    *,
    candidate: bool = True,
) -> dict[str, JsonValue]:
    """执行固定物理版本；返回的trace_id必定可查且与实际命中来自同次请求。"""
    if runtime.search is None:
        raise DomainError("capability_unavailable", "当前宿主没有启用检索能力")
    if request.diagnostics and not candidate:
        raise DomainError("forbidden", "详细诊断仅供编辑者候选试查，线上服务不接受此选项")
    release_info: dict[str, JsonValue] = {}
    if candidate:
        require_role(principal, write=True)
        build = _candidate(runtime, principal, target_id)
        kind = "build"
        target = build.build_id
    else:
        release = runtime.repo.get_active_release(
            principal, target_id, allow_deleted_projection=True
        )
        build = runtime.repo.get_build(principal, release.build_id)
        kind = "release"
        target = release.release_id
        release_info = {"release_id": release.release_id, "release_version": release.version}
    mode, top_k, filters = _effective(
        ServiceConfig.model_validate(build.service_config),
        principal,
        request,
        enforce_callers=not candidate,
    )
    epoch, retrieval_epoch, excluded = runtime.repo.retrieval_projection(
        principal, build.kb_id, [document.document_id for document in build.manifest]
    )
    started = time.monotonic()
    effective: dict[str, JsonValue] = {
        "query": request.query,
        "mode": mode,
        "top_k": top_k,
        "filters": TypeAdapter(list[JsonValue]).validate_python(filters),
    }
    if request.diagnostics:
        effective["diagnostics"] = True
    try:
        response = runtime.search.query_json(
            principal.tenant_id,
            build.kb_id,
            build.index_name,
            request.query,
            top_k=top_k,
            mode=mode,
            filters=filters,
            excluded_document_ids=excluded,
            diagnostics=request.diagnostics,
        )
        if set(_source_ids(response)) & set(excluded):
            raise DomainError("scope_violation", "检索返回了已撤销的资料")
        kb = runtime.repo.get_kb(principal, build.kb_id)
        if kb.authz_epoch != epoch or kb.retrieval_epoch != retrieval_epoch:
            raise DomainError(
                "authorization_pending", "查询期间权限或资料启用状态已改变，请重新查询"
            )
        outcome: dict[str, JsonValue] = {
            "status": "succeeded",
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "document_ids": TypeAdapter(list[JsonValue]).validate_python(_source_ids(response)),
            "index_name": build.index_name,
        }
        if request.diagnostics:
            diagnostic = response.get("diagnostics")
            if not isinstance(diagnostic, dict):
                raise DomainError("capability_unavailable", "检索适配器未提供同次详细诊断")
            outcome["diagnostics"] = diagnostic
        trace = record_trace(
            runtime.repo,
            principal,
            build.kb_id,
            target,
            kind,
            effective,
            outcome,
        )
    except Exception as exc:
        record_trace(
            runtime.repo,
            principal,
            build.kb_id,
            target,
            kind,
            effective,
            {
                "status": "failed",
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "error_code": exc.code if isinstance(exc, DomainError) else "retrieval_failed",
                "index_name": build.index_name,
            },
        )
        raise
    return {
        **response,
        **release_info,
        "trace_id": trace.trace_id,
        "build_id": build.build_id,
        "target_kind": kind,
        "target_id": target,
        "snapshot_id": build.index_name,
        "effective_request": effective,
        "authorization_epoch": epoch,
    }


def evaluate_candidate(
    runtime: Runtime, job: Job, owner: str, heartbeat: Callable[[], None] | None = None
) -> CandidateEvaluation:
    """Worker在固定候选上逐题执行真实检索，最后用任务租约原子提交完整报告。"""
    requested_by = job.payload.get("requested_by")
    principal = Principal(
        tenant_id=job.tenant_id,
        subject=requested_by if isinstance(requested_by, str) else "worker:" + job.job_id,
        roles=["editor"],
        kb_ids=[job.kb_id],
    )
    with runtime.repo.transaction() as session:
        runtime.repo.leased_in_transaction(session, job.job_id, owner, job.lease_epoch)
    build_id = TypeAdapter(str).validate_python(job.payload.get("build_id"), strict=True)
    name = TypeAdapter(str).validate_python(job.payload.get("name"), strict=True)
    sample_ids = TypeAdapter(list[str]).validate_python(job.payload.get("sample_ids"), strict=True)
    top_k = TypeAdapter(int).validate_python(job.payload.get("top_k"), strict=True)
    if (
        not sample_ids
        or len(sample_ids) > 100
        or len(set(sample_ids)) != len(sample_ids)
        or not 1 <= top_k <= 100
    ):
        raise DomainError("invalid_request", "候选评测样本或Top-K无效")
    build = _candidate(runtime, principal, build_id)
    evaluation_epoch, retrieval_epoch, _ = runtime.repo.retrieval_projection(
        principal, build.kb_id, []
    )
    if build.kb_id != job.kb_id:
        raise DomainError("invalid_request", "候选和任务不属于同一知识库")
    with runtime.repo.transaction() as session:
        runtime.repo.scope_in_transaction(session, principal, job.kb_id)
        samples = list(
            session.scalars(
                select(SampleRow).where(
                    SampleRow.tenant_id == principal.tenant_id,
                    SampleRow.kb_id == job.kb_id,
                    SampleRow.id.in_(sample_ids),
                )
            )
        )
        if len(samples) != len(sample_ids):
            raise DomainError("not_found", "部分人工评测样本不存在")
        frozen = [
            EvaluationSample(
                sample_id=row.id,
                kb_id=row.kb_id,
                query=row.query,
                expected_document_ids=row.expected_document_ids,
                expected_sources=[
                    ExpectedSource.model_validate(value) for value in row.expected_sources
                ],
                no_answer=row.no_answer,
                annotation_revision=row.annotation_revision,
                supersedes_id=row.supersedes_id,
                created_at=row.created_at,
            )
            for row in samples
        ]
    details: list[dict[str, JsonValue]] = []
    recalls: list[float] = []
    precisions: list[float] = []
    mrrs: list[float] = []
    evidence_recalls: list[float] = []
    no_answer_checks: list[float] = []
    config = ServiceConfig.model_validate(build.service_config)
    actual_top_k = top_k if "top_k" in config.allowed_fields else config.top_k
    # 文档级指标继续使用原生LlamaIndex实现；条款与无答案是单列的人工标注检查。
    from llama_index.core.evaluation.retrieval.metrics import MRR, Precision, Recall

    recall_metric, precision_metric, rank_metric = Recall(), Precision(), MRR()
    for sample in frozen:
        if heartbeat:
            heartbeat()
        request = (
            TrialRequest(query=sample.query, top_k=top_k)
            if "top_k" in config.allowed_fields
            else TrialRequest(query=sample.query)
        )
        response = query_target(runtime, principal, build.build_id, request)
        actual = _source_ids(response)
        actual_sources = _sources(response)
        detail: dict[str, JsonValue] = {
            "sample_id": sample.sample_id,
            "annotation_revision": sample.annotation_revision,
            "query": sample.query,
            "no_answer": sample.no_answer,
            "expected_document_ids": TypeAdapter(list[JsonValue]).validate_python(
                sample.expected_document_ids
            ),
            "expected_sources": TypeAdapter(list[JsonValue]).validate_python(
                [value.model_dump(mode="json") for value in sample.expected_sources]
            ),
            "actual_document_ids": TypeAdapter(list[JsonValue]).validate_python(actual),
            "actual_sources": TypeAdapter(list[JsonValue]).validate_python(actual_sources),
            "trace_id": response["trace_id"],
            "recall": None,
            "precision": None,
            "mrr": None,
            "evidence_recall": None,
            "no_answer_passed": None,
        }
        if sample.no_answer:
            passed = not actual
            no_answer_checks.append(float(passed))
            detail["no_answer_passed"] = passed
        else:
            if not sample.expected_document_ids:
                raise DomainError("invalid_evaluation", "正例样本缺少人工相关资料标注")
            recall = (
                recall_metric.compute(
                    expected_ids=sample.expected_document_ids, retrieved_ids=actual
                ).score
                if actual
                else 0.0
            )
            precision = (
                precision_metric.compute(
                    expected_ids=sample.expected_document_ids, retrieved_ids=actual
                ).score
                if actual
                else 0.0
            )
            mrr = (
                rank_metric.compute(
                    expected_ids=sample.expected_document_ids, retrieved_ids=actual
                ).score
                if actual
                else 0.0
            )
            recalls.append(recall)
            precisions.append(precision)
            mrrs.append(mrr)
            detail.update(
                {
                    "recall": recall,
                    "precision": precision,
                    "mrr": mrr,
                    "missing_document_ids": TypeAdapter(list[JsonValue]).validate_python(
                        sorted(set(sample.expected_document_ids) - set(actual))
                    ),
                }
            )
            if sample.expected_sources:
                matched = [
                    source
                    for source in sample.expected_sources
                    if any(_matches(source, actual_source) for actual_source in actual_sources)
                ]
                missed = [source for source in sample.expected_sources if source not in matched]
                score = len(matched) / len(sample.expected_sources)
                evidence_recalls.append(score)
                detail.update(
                    {
                        "evidence_recall": score,
                        "matched_sources": TypeAdapter(list[JsonValue]).validate_python(
                            [value.model_dump(mode="json") for value in matched]
                        ),
                        "missing_sources": TypeAdapter(list[JsonValue]).validate_python(
                            [value.model_dump(mode="json") for value in missed]
                        ),
                    }
                )
        details.append(detail)
    metrics: dict[str, JsonValue] = {
        "recall": sum(recalls) / len(recalls) if recalls else None,
        "precision": sum(precisions) / len(precisions) if precisions else None,
        "mrr": sum(mrrs) / len(mrrs) if mrrs else None,
        "evidence_recall": sum(evidence_recalls) / len(evidence_recalls)
        if evidence_recalls
        else None,
        "no_answer_accuracy": sum(no_answer_checks) / len(no_answer_checks)
        if no_answer_checks
        else None,
        "sample_count": len(frozen),
        "positive_count": len(recalls),
        "evidence_labeled_count": len(evidence_recalls),
        "no_answer_count": len(no_answer_checks),
        "top_k": actual_top_k,
        "unit": "unique_document",
        "no_answer_rule": "zero_retrieved_evidence",
    }
    with runtime.repo.transaction() as session:
        kb = runtime.repo.scope_in_transaction(session, principal, job.kb_id, write=True, lock=True)
        if kb.authz_epoch != evaluation_epoch or kb.retrieval_epoch != retrieval_epoch:
            raise DomainError("authorization_pending", "评测期间权限或资料启用状态已改变")
        lease = runtime.repo.leased_in_transaction(session, job.job_id, owner, job.lease_epoch)
        row = CandidateEvaluationRow(
            id=uuid4().hex,
            tenant_id=principal.tenant_id,
            kb_id=job.kb_id,
            build_id=build.build_id,
            name=name,
            metrics=metrics,
            details=details,
        )
        session.add(row)
        lease.result = {"evaluation_id": row.id, "build_id": build.build_id}
        lease.status, lease.stage, lease.lease_owner, lease.lease_until = (
            "succeeded",
            "complete",
            None,
            None,
        )
        lease.updated_at = TypeAdapter(datetime).validate_python(
            session.scalar(select(func.clock_timestamp())), strict=True
        )
        runtime.repo.audit_in_transaction(
            session, principal, "candidate.evaluate", row.id, kb_id=row.kb_id
        )
        session.flush()
        return evaluation_value(row)
