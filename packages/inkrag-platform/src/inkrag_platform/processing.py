"""检索处理能力桥接；仅RAG宿主延迟加载核心配置，独立解析不依赖模型包。"""

from inkparse import ParseResult
from pydantic import JsonValue

from .models import DomainError
from .operations import Runtime


def processing_capabilities(runtime: Runtime) -> dict[str, JsonValue]:
    """暴露实际模型预算与核心默认值，不把未接入能力包装为可选功能。"""
    available = runtime.search is not None
    defaults: dict[str, JsonValue] | None = None
    maximum: int | None = None
    reason: str | None = "当前宿主未装配检索模型，仅提供文档解析"
    if available:
        from inkrag.models import ProcessingConfig
        from inkrag.search import SearchBackend

        if isinstance(runtime.search, SearchBackend):
            maximum = runtime.search.max_tokens
            if maximum < 32:
                available = False
                reason = "当前模型token上限低于分块器要求的最小32，无法提供检索分块"
            else:
                core_defaults = ProcessingConfig()
                size = min(core_defaults.chunk_size, maximum)
                defaults = ProcessingConfig.model_validate(
                    {
                        **core_defaults.model_dump(),
                        "chunk_size": size,
                        "chunk_overlap": min(core_defaults.chunk_overlap, size // 8),
                    }
                ).model_dump(mode="json")
                reason = None
        else:
            available = False
    return {
        "available": available,
        "reason": reason,
        "max_tokens": maximum,
        "defaults": defaults,
        "chunking": ["auto", "sentence", "markdown"],
        "qa_pairs": {"available": False, "reason": "尚未接入问答对生成模型"},
        "supplemental_index": {"available": False, "reason": "尚未接入补充索引生成与检索"},
        "image_semantics": {"available": False, "reason": "尚未接入图片语义理解与图片向量索引"},
    }


def normalize_processing(
    runtime: Runtime, value: dict[str, JsonValue] | None
) -> dict[str, JsonValue] | None:
    """用核心唯一规则校验并冻结默认；不允许处理预算超过当前嵌入模型上限。"""
    capability = processing_capabilities(runtime)
    if capability["available"] is not True:
        if value is not None:
            raise DomainError("capability_unavailable", "当前宿主不支持检索分块参数")
        return None
    from inkrag.models import ProcessingConfig

    defaults = capability["defaults"]
    if not isinstance(defaults, dict):
        raise DomainError("capability_unavailable", "当前宿主没有可用的检索默认参数")
    merged = {**defaults, **(value or {})}
    size, overlap = merged.get("chunk_size"), defaults.get("chunk_overlap")
    if "chunk_overlap" not in (value or {}) and isinstance(size, int) and isinstance(overlap, int):
        merged["chunk_overlap"] = min(overlap, size // 8)
    config = ProcessingConfig.model_validate(merged)
    maximum = capability["max_tokens"]
    if isinstance(maximum, int) and config.chunk_size > maximum:
        raise DomainError("invalid_request", f"分块长度不得超过当前模型的 {maximum} token 上限")
    return config.model_dump(mode="json")


def preview_chunks(
    runtime: Runtime,
    tenant_id: str,
    kb_id: str,
    identity: str,
    result: ParseResult,
    value: dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    """与正式索引调用同一切分器及tokenizer，只返回前20块但总数来自完整真实切分。"""
    normalized = normalize_processing(runtime, value)
    if normalized is None:
        return {
            "chunk_count": None,
            "chunks": [],
            "effective_processing": None,
            "chunks_truncated": False,
            "max_tokens": None,
        }
    from llama_index.core.utils import get_tokenizer

    from inkrag.models import Error, ProcessingConfig
    from inkrag.search import SearchBackend, result_nodes

    if not isinstance(runtime.search, SearchBackend):
        raise DomainError("capability_unavailable", "当前检索适配器无法提供真实分块预览")
    search = runtime.search
    try:
        nodes = result_nodes(
            tenant_id,
            kb_id,
            identity,
            result,
            ProcessingConfig.model_validate(normalized),
            tokenizer=search.tokenizer,
            max_tokens=search.max_tokens,
            tokenizer_revision=search.model_revision if search.tokenizer else None,
        )
    except Error as exc:
        if exc.code != "empty_document":
            raise
        nodes = []
    tokenizer = search.tokenizer or get_tokenizer()
    chunks: list[JsonValue] = []
    for node in nodes[:20]:
        origin = node.metadata.get("parse_origin", {})
        chunks.append(
            {
                "text": node.get_content(),
                "token_count": len(tokenizer(node.get_content())),
                "element_ref": origin.get("element_ref"),
                "page": node.metadata.get("page"),
            }
        )
    return {
        "chunk_count": len(nodes),
        "chunks": chunks,
        "effective_processing": normalized,
        "chunks_truncated": len(nodes) > 20,
        "max_tokens": search.max_tokens,
    }
