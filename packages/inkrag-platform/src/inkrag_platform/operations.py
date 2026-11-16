"""组合平台的短流程；任务只持久提交，长解析和索引由独立worker执行。"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Literal, Protocol

from inkparse import (
    ParseError,
    ParseResult,
    export_markdown,
    load_stored_result,
    revise_result,
    save_stored_result,
)
from inkparse.adapters import ParserEngine, check_engine_format
from inkparse.adapters.docling.models import models_ready
from inkstore import AssetStorage
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from .local_model import LocalChatModel
from .models import Document, DomainError, Evaluation, Job, Principal, ResultVersion
from .repository import Repository
from .workspace_models import ServiceConfig

if TYPE_CHECKING:
    from .vision import VisionModel


class TokenVerifier(Protocol):
    """只接受经过校验的令牌，身份不来自请求的tenant字段。"""

    def verify(self, token: str) -> Principal:
        """验证签名、发行方、受众和有效期后返回可信主体。"""
        ...


class SearchPort(Protocol):
    """避免独立解析宿主导入RAG引擎的检索边界，具体签名随适配器核对。"""

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
        """在可信作用域及固定发布generation查询，返回明确HTTP投影。"""
        ...

    def fields_json(
        self, tenant_id: str, knowledge_base_id: str, generation: str
    ) -> dict[str, JsonValue]:
        """返回固定发布generation的声明字段，不暴露保留范围字段。"""
        ...


@dataclass(frozen=True)
class Runtime:
    """显式持有借入依赖，不在构造时启动Docker、迁移或加载模型。"""

    repo: Repository
    assets_for: Callable[[Principal], AssetStorage]
    auth: TokenVerifier
    search: SearchPort | None
    oidc: dict[str, str]
    mode: Literal["parse", "rag", "combined"] = "combined"
    parse_models_path: str | None = None
    check_ready: Callable[[], None] | None = None
    model_identity: dict[str, JsonValue] | None = None
    local_model: LocalChatModel | None = None
    vision_model: "VisionModel | None" = None
    vision_provider: Literal["siliconflow", "local", "disabled"] = "disabled"


class StrictModel(BaseModel):
    """HTTP控制输入拒绝未知字段和隐式类型转换。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class TextChange(StrictModel):
    """对现有原生文本元素的有界修订。"""

    element_ref: str = Field(pattern=r"^#/texts/[0-9]+$")
    text: str = Field(max_length=1024 * 1024)


class RevisionRequest(StrictModel):
    """要求提交编辑所基于的版本，防止覆盖后续修订。"""

    expected_result_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: list[TextChange] = Field(min_length=1, max_length=1000)


class DocumentView(StrictModel):
    """解析任务未完成时result为空；目录身份和持久任务仍可查询。"""

    document: Document
    result: ParseResult | None
    version: ResultVersion | None = None


def document_view(
    runtime: Runtime, principal: Principal, document_id: str, result_id: str | None = None
) -> DocumentView:
    """核对资料和历史归属后读取不可变产物，不从输入读取任意存储路径。"""
    document = runtime.repo.get_document(principal, document_id)
    if document.result_id is None and result_id is None:
        return DocumentView(document=document, result=None)
    version = runtime.repo.get_version(principal, document_id, result_id)
    result = load_stored_result(runtime.assets_for(principal), version.result_id)
    return DocumentView(document=document, result=result, version=version)


def submit_upload(
    runtime: Runtime,
    principal: Principal,
    kb_id: str,
    path: Path,
    filename: str,
    idempotency_key: str,
    metadata: dict[str, JsonValue] | None = None,
    parser_engine: ParserEngine = "builtin",
    ocr_enabled: bool | None = None,
) -> Job:
    """上传字节保存成功后原子登记资料和parse任务，失败不返回已接受状态。

    20MiB预算由HTTP实际流和这里的冻结读取共同保证；当前受控代理上传，
    不假装已经实现预签名finalize。可能留下未引用内容，不能删除别人的去重资产。
    """
    runtime.repo.get_kb(principal, kb_id)
    if not {"admin", "editor"}.intersection(principal.roles):
        raise DomainError("forbidden", "当前账号没有修改权限")
    with path.open("rb") as stream:
        data = stream.read(20 * 1024 * 1024 + 1)
    if not data or len(data) > 20 * 1024 * 1024:
        raise DomainError("invalid_upload", "文件为空或超过20MiB")
    try:
        check_parser_input(runtime, parser_engine, filename, ocr_enabled)
    except ParseError as exc:
        raise DomainError(exc.code, str(exc)) from None
    asset = runtime.assets_for(principal).put_bytes(data)
    return runtime.repo.submit_parse(
        principal,
        kb_id,
        filename,
        asset.asset_id,
        idempotency_key,
        metadata,
        parser_engine,
        ocr_enabled,
    )


def check_parser_input(
    runtime: Runtime, engine: ParserEngine, filename: str, ocr_enabled: bool | None = None
) -> None:
    """校验工具真实格式与当前部署的离线工件。"""
    if engine == "builtin" and ocr_enabled is True:
        raise ParseError("unsupported_option", "内置解析工具不支持 OCR，请显式选择 Docling")
    format_name = check_engine_format(engine, filename)
    if (
        engine == "docling"
        and format_name in {"pdf", "image"}
        and not models_ready(runtime.parse_models_path)
    ):
        raise ParseError("engine_unavailable", "Docling 的 PDF 和图片解析模型尚未配置")


def save_revision(
    runtime: Runtime, principal: Principal, document_id: str, request: RevisionRequest
) -> DocumentView:
    """只创建文本修订，先持久结果再用PG条件提交，冲突不覆盖旧指针。"""
    if not {"admin", "editor"}.intersection(principal.roles):
        raise DomainError("forbidden", "当前账号没有修改权限")
    current = document_view(runtime, principal, document_id)
    if current.result is None:
        raise DomainError("not_ready", "资料尚未解析完成")
    if current.result.result_id != request.expected_result_id:
        raise DomainError("version_conflict", "当前版本已变化，请重新检查")
    if len({c.element_ref for c in request.changes}) != len(request.changes):
        raise DomainError("invalid_revision", "同一文本元素不能重复修改")
    result = revise_result(current.result, {c.element_ref: c.text for c in request.changes})
    save_stored_result(result, runtime.assets_for(principal))
    runtime.repo.save_revision(
        principal,
        document_id,
        request.expected_result_id,
        result.result_id,
        result.revision,
        result.previous_result_id,
        [a.asset_id for a in result.assets],
    )
    return document_view(runtime, principal, document_id, result.result_id)


def export_document(
    runtime: Runtime,
    principal: Principal,
    document_id: str,
    format_name: Literal["json", "markdown"],
    result_id: str | None,
) -> bytes:
    """按授权读取明确版本导出；临时Markdown文件总会清理。"""
    view = document_view(runtime, principal, document_id, result_id)
    if view.result is None:
        raise DomainError("not_ready", "资料尚未解析完成")
    if format_name == "json":
        return view.result.model_dump_json(indent=2).encode()
    with TemporaryDirectory(prefix="inkrag-export-") as directory:
        path = Path(directory) / "document.md"
        export_markdown(view.result, path)
        return path.read_bytes()


class EvaluationHit(StrictModel):
    """评测只使用检索实际返回的正文和来源，不把模型分数当质量标签。"""

    text: str
    score: float | None
    metadata: dict[str, JsonValue]


def evaluate_release(
    runtime: Runtime,
    principal: Principal,
    kb_id: str,
    release_id: str,
    name: str,
    top_k: int = 5,
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid",
    sample_ids: list[str] | None = None,
    lease: tuple[str, str, int] | None = None,
) -> Evaluation:
    """供独立worker执行固定发布的文档级评测，保存真实宏平均指标。

    每个问题的chunk按资料ID去重，precision分母为实际返回资料数，
    recall分母为人工标注资料数；MRR采用首个相关资料的倒数排名。
    发布切换或授权epoch变化明确失败，空标注集不产生零分假结论。
    """
    if runtime.search is None or not 1 <= top_k <= 100:
        raise DomainError("invalid_evaluation", "评测需要检索能力与合法top_k")
    release = runtime.repo.get_active_release(principal, kb_id)
    if release.release_id != release_id:
        raise DomainError("version_conflict", "评测所依据的发布版本已变化")
    build = runtime.repo.get_build(principal, release.build_id)
    policy = ServiceConfig.model_validate(build.service_config)
    effective_top_k = top_k if "top_k" in policy.allowed_fields else policy.top_k
    effective_mode = mode if "mode" in policy.allowed_fields else policy.mode
    fixed_filters = [item.model_dump(mode="json") for item in policy.fixed_filters]
    _, retrieval_epoch, excluded = runtime.repo.retrieval_projection(
        principal, kb_id, [item.document_id for item in build.manifest]
    )
    samples = runtime.repo.list_samples(principal, kb_id)
    if sample_ids is not None:
        requested = set(sample_ids)
        samples = [sample for sample in samples if sample.sample_id in requested]
        if len(samples) != len(requested):
            raise DomainError("invalid_evaluation", "评测样本已变化")
    if any(sample.no_answer or sample.expected_sources for sample in samples):
        raise DomainError(
            "invalid_evaluation", "条款与无答案标注请使用固定构建评测，以保留逐题来源和独立指标"
        )
    if not samples or len(samples) > 100:
        raise DomainError("invalid_evaluation", "评测需要1到100个真实标注样本")
    recalls: list[float] = []
    precisions: list[float] = []
    reciprocal_ranks: list[float] = []
    # 只在实际RAG评测时加载原生指标，独立解析宿主仍不依赖或导入LlamaIndex。
    from llama_index.core.evaluation.retrieval.metrics import MRR, Precision, Recall

    recall_metric, precision_metric, rank_metric = Recall(), Precision(), MRR()
    for sample in samples:
        response = runtime.search.query_json(
            principal.tenant_id,
            kb_id,
            release.index_name,
            sample.query,
            top_k=effective_top_k,
            mode=effective_mode,
            filters=fixed_filters,
            excluded_document_ids=excluded,
        )
        hits = TypeAdapter(list[EvaluationHit]).validate_python(response.get("hits"))
        identities: list[str] = []
        for hit in hits:
            identity = hit.metadata.get("document_id")
            if not isinstance(identity, str) or not identity:
                raise DomainError("invalid_search_result", "检索结果缺少可核验的资料身份")
            if identity in excluded:
                raise DomainError("scope_violation", "检索返回了已禁用或撤销的资料")
            if identity not in identities:
                identities.append(identity)
        # 原生指标拒绝空检索列表；业务合法的零命中统一记0，而非失败或丢掉该样本。
        recalls.append(
            recall_metric.compute(
                expected_ids=sample.expected_document_ids, retrieved_ids=identities
            ).score
            if identities
            else 0.0
        )
        precisions.append(
            precision_metric.compute(
                expected_ids=sample.expected_document_ids, retrieved_ids=identities
            ).score
            if identities
            else 0.0
        )
        reciprocal_ranks.append(
            rank_metric.compute(
                expected_ids=sample.expected_document_ids, retrieved_ids=identities
            ).score
            if identities
            else 0.0
        )
    if runtime.repo.get_kb(principal, kb_id).retrieval_epoch != retrieval_epoch:
        raise DomainError("version_conflict", "评测期间资料启用状态已变化")
    current = runtime.repo.get_active_release(principal, kb_id)
    if current.release_id != release_id or current.authz_epoch != release.authz_epoch:
        raise DomainError("version_conflict", "评测期间发布或授权已变化")
    metrics: dict[str, JsonValue] = {
        "sample_count": len(samples),
        "top_k": effective_top_k,
        "mode": effective_mode,
        "filters": [item.model_dump(mode="json") for item in policy.fixed_filters],
        "recall": sum(recalls) / len(recalls),
        "precision": sum(precisions) / len(precisions),
        "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
        "unit": "document",
        "sample_ids": [sample.sample_id for sample in samples],
    }
    if lease is not None:
        job_id, owner, epoch = lease
        return runtime.repo.record_job_evaluation(
            job_id, owner, epoch, release_id, name, metrics, retrieval_epoch
        )
    return runtime.repo.record_evaluation(
        principal, kb_id, release_id, name, metrics, retrieval_epoch
    )
