"""OpenSearch生产索引适配：候选增量构建、后端冻结和范围内混合检索。"""

import hashlib
import json
import math
import re
from collections.abc import Callable
from typing import Literal

from inkparse import ParseResult
from llama_index.core import Document
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.schema import NodeWithScore, TextNode
from opensearchpy import OpenSearch, TransportError
from pydantic import JsonValue, TypeAdapter

from .documents import (
    NODE_PIPELINE_REVISION,
    NativeText,
    prepare_result_nodes,
    processing_revision,
)
from .lexical import LEGACY_LEXICAL, LexicalProfile
from .models import Error, ParseOrigin, ProcessingConfig
from .opensearch_keyword import (
    keyword_fields,
    keyword_mapping,
    keyword_query,
    opensearch_analyzer,
    terms_field_exists,
)
from .retrieval import check_vector, retrieval_mode, validate_query
from .search_diagnostics import native_explanation
from .search_models import FieldType, GenerationMetadata, Scalar, SearchFilter, SearchManifest
from .search_queries import (
    RRF_PIPELINE,
    compile_filters,
    rrf_pipeline,
    scope_filters,
    validate_attributes,
    validate_schema,
    vector_query,
)


def object_value(value: JsonValue | object) -> dict[str, JsonValue]:
    """把SDK响应限制为普通JSON对象，缺失结构不能被解释成空成功。"""
    try:
        return TypeAdapter(dict[str, JsonValue]).validate_python(value, strict=True)
    except ValueError:
        raise Error("invalid_search_response", "检索后端返回了无效结构") from None


def write_block_error(value: JsonValue, depth: int = 0) -> bool:
    """只识别原生写屏障错误及其嵌套原因，普通权限403不能伪装为冻结冲突。"""
    if depth > 10:
        return False
    if isinstance(value, dict):
        return value.get("type") == "cluster_block_exception" or any(
            write_block_error(child, depth + 1) for child in value.values()
        )
    if isinstance(value, list):
        return any(write_block_error(child, depth + 1) for child in value)
    return False


class SearchBackend:
    """借入官方客户端及原生嵌入模型，不持有PG发布指针或选择备用后端。"""

    def __init__(
        self,
        client: OpenSearch,
        embedding: BaseEmbedding,
        *,
        model_revision: str,
        dimensions: int,
        replicas: int = 1,
        tokenizer: Callable[[str], list[int]] | None = None,
        max_tokens: int = 510,
        lexical_profile: LexicalProfile = LEGACY_LEXICAL,
    ) -> None:
        """显式绑定完整模型身份和维度；副本数由部署配置，不在故障时降为零。"""
        if (
            not model_revision.strip()
            or not 1 <= dimensions <= 65536
            or not 0 <= replicas <= 5
            or not 32 <= max_tokens <= 8192
        ):
            raise ValueError("模型身份、维度或副本配置无效")
        self.client, self.embedding = client, embedding
        self.model_revision, self.dimensions, self.replicas = model_revision, dimensions, replicas
        self.tokenizer, self.max_tokens = tokenizer, max_tokens
        self.lexical_profile: LexicalProfile = lexical_profile

    def _request(
        self,
        method: str,
        path: str,
        body: object = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, JsonValue]:
        """直接调用官方传输并验证响应，外部异常只公开安全类别。"""
        try:
            payload = TypeAdapter(JsonValue).validate_python(body, strict=True)
            result = self.client.transport.perform_request(
                method, path, params=params, body=payload
            )
        except TransportError as exc:
            code = "search_access_denied" if exc.status_code in {401, 403} else "search_unavailable"
            if exc.status_code == 403:
                detail = TypeAdapter(JsonValue).validate_python(exc.info, strict=True)
                if write_block_error(detail):
                    code = "index_frozen"
            raise Error(code, "检索后端请求失败，请检查依赖或候选写入状态") from None
        return object_value(result)

    @staticmethod
    def generation_name(tenant_id: str, knowledge_base_id: str, build_id: str) -> str:
        """使用范围和构建摘要形成固定物理名，用户索引路径不能进入请求。"""
        if any(
            not item.strip() or len(item) > 256 for item in (tenant_id, knowledge_base_id, build_id)
        ):
            raise Error("invalid_scope", "索引范围或构建身份无效")
        scope = hashlib.sha256(json.dumps([tenant_id, knowledge_base_id]).encode()).hexdigest()[:32]
        build = hashlib.sha256(build_id.encode()).hexdigest()[:32]
        return f"inkrag-{scope}-{build}"

    def _binding(
        self, tenant_id: str, knowledge_base_id: str, generation: str
    ) -> GenerationMetadata:
        """校验物理名和后端清单都属于可信范围，拒绝跨KB或模型混用。"""
        prefix = self.generation_name(tenant_id, knowledge_base_id, "validation").rsplit("-", 1)[0]
        suffix = generation.removeprefix(prefix + "-")
        if (
            not generation.startswith(prefix + "-")
            or len(suffix) != 32
            or any(c not in "0123456789abcdef" for c in suffix)
        ):
            raise Error("invalid_scope", "索引不属于当前知识库")
        response = self._request("GET", f"/{generation}/_mapping")
        mapping = object_value(object_value(response.get(generation)).get("mappings"))
        meta = GenerationMetadata.model_validate(mapping.get("_meta"))
        if (meta.tenant_id, meta.knowledge_base_id, meta.model_revision, meta.dimensions) != (
            tenant_id,
            knowledge_base_id,
            self.model_revision,
            self.dimensions,
        ):
            raise Error("index_contract_conflict", "索引范围或模型契约不一致")
        return meta

    def health(self) -> dict[str, JsonValue]:
        """只读集群健康；具体HA拓扑由部署验收，不从200响应推断。"""
        return self._request("GET", "/_cluster/health")

    def fields_json(
        self, tenant_id: str, knowledge_base_id: str, generation: str
    ) -> dict[str, JsonValue]:
        """返回固定发布版本的字段契约，编辑态KB定义不能冒充已发布可筛选字段。"""
        binding = self._binding(tenant_id, knowledge_base_id, generation)
        self._require_frozen(generation)
        return {name: kind for name, kind in binding.field_schema.items()}

    def create_build(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        build_id: str,
        previous_generation: str | None = None,
        field_schema: dict[str, FieldType] | None = None,
    ) -> str:
        """创建候选；复用旧generation时原生reindex向量，不重新嵌入旧资料。

        调用方须使用独立build/attempt身份并持PG租约。复制失败保留不可发布候选；
        copy_complete只在完整复制后设置，重试可幂等恢复，不自动发布alias。
        """
        schema = field_schema or {}
        validate_schema(schema)
        generation = self.generation_name(tenant_id, knowledge_base_id, build_id)
        meta = GenerationMetadata(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            build_id=build_id,
            model_revision=self.model_revision,
            dimensions=self.dimensions,
            field_schema=schema,
            node_pipeline_revision=NODE_PIPELINE_REVISION,
            lexical_profile=self.lexical_profile,
            previous_generation=previous_generation,
            copy_complete=previous_generation is None,
        )
        if previous_generation is not None:
            previous = self._binding(tenant_id, knowledge_base_id, previous_generation)
            if previous.node_pipeline_revision != NODE_PIPELINE_REVISION:
                raise Error("index_contract_conflict", "切分或来源规则已更新，需从冻结原文重建")
            if previous.lexical_profile != meta.lexical_profile:
                raise Error("index_contract_conflict", "分词规则已更新，需生成完整的新关键词索引")
            if any(schema.get(name) != kind for name, kind in previous.field_schema.items()):
                raise Error("field_schema_conflict", "复用索引不能移除或改变已有字段类型")
            self._require_frozen(previous_generation)
        try:
            self._request("PUT", f"/{generation}", self._mapping(meta))
        except Error:
            existing = self._binding(tenant_id, knowledge_base_id, generation)
            if existing.model_copy(update={"copy_complete": meta.copy_complete}) != meta:
                raise Error("build_conflict", "已有候选与请求的构建契约不一致") from None
            meta = existing
        if not meta.copy_complete:
            response = self._request(
                "POST",
                "/_reindex",
                {
                    "source": {"index": previous_generation},
                    "dest": {"index": generation},
                    "conflicts": "abort",
                },
                {"refresh": "true", "wait_for_completion": "true"},
            )
            if response.get("timed_out") is not False or response.get("failures") != []:
                raise Error("index_copy_failed", "候选索引复制不完整，不允许发布")
            meta = meta.model_copy(update={"copy_complete": True})
            self._request("PUT", f"/{generation}/_mapping", {"_meta": meta.model_dump(mode="json")})
        self._ensure_pipeline()
        return generation

    def reusable_generation(self, tenant_id: str, knowledge_base_id: str, generation: str) -> bool:
        """旧发布可以继续查询，规则升级后的构建重新切分冻结原文。"""
        binding = self._binding(tenant_id, knowledge_base_id, generation)
        return (
            binding.node_pipeline_revision == NODE_PIPELINE_REVISION
            and binding.lexical_profile == self.lexical_profile
        )

    def _mapping(self, meta: GenerationMetadata) -> dict[str, JsonValue]:
        """固定索引字段和模型映射，metadata只开放产品声明的标量字段。"""
        types = {"text": "keyword", "number": "double", "boolean": "boolean", "date": "date"}
        attributes: dict[str, JsonValue] = {
            name: {"type": types[kind]} for name, kind in meta.field_schema.items()
        }
        return {
            "settings": {
                "index": {"knn": True, "number_of_shards": 1, "number_of_replicas": self.replicas}
            },
            "mappings": {
                "dynamic": "strict",
                "_meta": meta.model_dump(mode="json"),
                "properties": {
                    "tenant_id": {"type": "keyword"},
                    "knowledge_base_id": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "result_id": {"type": "keyword"},
                    **keyword_mapping(),
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": self.dimensions,
                        "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"},
                    },
                    "attributes": {"type": "object", "dynamic": "strict", "properties": attributes},
                    "node": {"type": "object", "enabled": False},
                },
            },
        }

    def _ensure_pipeline(self) -> None:
        """使用OpenSearch官方RRF处理器，避免本地另写融合算法。"""
        self._request(
            "PUT",
            f"/_search/pipeline/{RRF_PIPELINE}",
            {"description": "InkRAG reciprocal rank fusion", **rrf_pipeline()},
        )

    def upsert_result(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        generation: str,
        document_id: str,
        result: ParseResult,
        metadata: dict[str, Scalar] | None = None,
        processing: ProcessingConfig | None = None,
    ) -> int:
        """仅更新候选中本资料节点；部分失败明确报错，旧发布保持不可变。"""
        binding = self._binding(tenant_id, knowledge_base_id, generation)
        if not binding.copy_complete:
            raise Error("index_copy_incomplete", "候选复制尚未完成")
        if binding.lexical_profile != self.lexical_profile:
            raise Error("index_contract_conflict", "候选分词规则与当前 Worker 配置不一致")
        attributes = validate_attributes(metadata or {}, binding.field_schema)
        parsed = ParseResult.model_validate_json(result.model_dump_json())
        nodes = result_nodes(
            tenant_id,
            knowledge_base_id,
            document_id,
            parsed,
            processing or ProcessingConfig(),
            tokenizer=self.tokenizer,
            max_tokens=self.max_tokens,
            tokenizer_revision=self.model_revision if self.tokenizer is not None else None,
        )
        for node in nodes:
            node.metadata["metadata"] = attributes
            node.excluded_embed_metadata_keys = list(node.metadata)
            node.excluded_llm_metadata_keys = list(node.metadata)
        self.remove_document(tenant_id, knowledge_base_id, generation, document_id)
        analyzer = opensearch_analyzer(binding.lexical_profile)
        for start in range(0, len(nodes), 32):
            batch = nodes[start : start + 32]
            vectors = self.embedding.get_text_embedding_batch([n.get_content() for n in batch])
            if len(vectors) != len(batch):
                raise Error("invalid_embedding", "嵌入模型返回数量与节点不一致")
            lines: list[str] = []
            for node, vector in zip(batch, vectors, strict=True):
                check_vector(vector, self.dimensions)
                text = node.get_content()
                lines.append(json.dumps({"index": {"_index": generation, "_id": node.node_id}}))
                lines.append(
                    json.dumps(
                        {
                            "tenant_id": tenant_id,
                            "knowledge_base_id": knowledge_base_id,
                            "document_id": document_id,
                            "result_id": parsed.result_id,
                            "text": text,
                            **keyword_fields(analyzer.analyze(text)),
                            "embedding": vector,
                            "attributes": attributes,
                            "node": node.model_dump(mode="json", exclude={"embedding"}),
                        },
                        ensure_ascii=False,
                    )
                )
            response = self._request(
                "POST", "/_bulk", "\n".join(lines) + "\n", {"refresh": "wait_for"}
            )
            items = response.get("items")
            if (
                response.get("errors") is not False
                or not isinstance(items, list)
                or len(items) != len(batch)
            ):
                raise Error("index_write_failed", "索引批量写入未全部成功，不允许发布")
            for item in items:
                status = object_value(object_value(item).get("index")).get("status")
                if not isinstance(status, int) or not 200 <= status < 300:
                    raise Error("index_write_failed", "索引批量写入存在失败项")
        return len(nodes)

    def remove_document(
        self, tenant_id: str, knowledge_base_id: str, generation: str, document_id: str
    ) -> None:
        """在候选中删除资料，后端write block会拒绝任何对已冻结generation的修改。"""
        self._binding(tenant_id, knowledge_base_id, generation)
        clauses: list[JsonValue] = [
            *scope_filters(tenant_id, knowledge_base_id),
            {"term": {"document_id": document_id}},
        ]
        response = self._request(
            "POST",
            f"/{generation}/_delete_by_query",
            {"query": {"bool": {"filter": clauses}}},
            {"refresh": "true", "conflicts": "abort"},
        )
        if response.get("timed_out") is not False or response.get("failures") != []:
            raise Error("index_write_failed", "候选资料替换未全部完成")

    def _require_frozen(self, generation: str) -> None:
        """确认索引后端实际阻止写入，不能只信PG中的ready字符串。"""
        response = self._request("GET", f"/{generation}/_settings")
        settings = object_value(object_value(response.get(generation)).get("settings"))
        index = object_value(settings.get("index"))
        blocks = object_value(index.get("blocks"))
        if blocks.get("write") not in {True, "true"}:
            raise Error("index_not_frozen", "索引尚未封锁写入，不能作为发布版本")

    def freeze(self, tenant_id: str, knowledge_base_id: str, generation: str) -> SearchManifest:
        """实际封锁并排空后端写入，再refresh核对节点/资料清单；不更改PG发布。"""
        binding = self._binding(tenant_id, knowledge_base_id, generation)
        if not binding.copy_complete:
            raise Error("index_copy_incomplete", "候选复制未完成")
        block = self._request("PUT", f"/{generation}/_block/write")
        indices = block.get("indices")
        if (
            block.get("acknowledged") is not True
            or block.get("shards_acknowledged") is not True
            or not isinstance(indices, list)
            or not indices
            or any(object_value(i).get("blocked") is not True for i in indices)
        ):
            raise Error("index_freeze_failed", "索引写入屏障未全部确认")
        refresh = self._request("POST", f"/{generation}/_refresh")
        if object_value(refresh.get("_shards")).get("failed") != 0:
            raise Error("index_freeze_failed", "索引可见性刷新失败")
        self._require_frozen(generation)
        response = self._request(
            "POST",
            f"/{generation}/_count",
            {"query": {"bool": {"filter": scope_filters(tenant_id, knowledge_base_id)}}},
        )
        if object_value(response.get("_shards")).get("failed") != 0:
            raise Error("index_freeze_failed", "索引节点计数存在失败分片")
        total = response.get("count")
        if isinstance(total, bool) or not isinstance(total, int):
            raise Error("invalid_search_response", "索引节点计数无效")
        terms_exist = terms_field_exists(binding.lexical_profile)
        if terms_exist is not None:
            self._check_lexical_coverage(tenant_id, knowledge_base_id, generation, terms_exist)
        count = self._document_count(tenant_id, knowledge_base_id, generation)
        return SearchManifest(
            generation=generation,
            node_count=total,
            document_count=count,
            model_revision=self.model_revision,
            dimensions=self.dimensions,
        )

    def _check_lexical_coverage(
        self, tenant_id: str, knowledge_base_id: str, generation: str, terms_exist: JsonValue
    ) -> None:
        """锁写后检查预切词项覆盖，空字符串代表无正文词项。"""
        response = self._request(
            "POST",
            f"/{generation}/_count",
            {
                "query": {
                    "bool": {
                        "filter": scope_filters(tenant_id, knowledge_base_id),
                        "must_not": [terms_exist],
                    }
                }
            },
        )
        if response.get("count") != 0 or object_value(response.get("_shards")).get("failed") != 0:
            raise Error("index_lexical_incomplete", "候选索引的关键词字段未完整写入")

    def _document_count(self, tenant_id: str, knowledge_base_id: str, generation: str) -> int:
        """分页composite聚合精确计数，不能用近似cardinality充当发布清单。"""
        count = 0
        after: dict[str, JsonValue] | None = None
        while True:
            composite: dict[str, JsonValue] = {
                "size": 1000,
                "sources": [{"document_id": {"terms": {"field": "document_id"}}}],
            }
            if after is not None:
                composite["after"] = after
            response = self._request(
                "POST",
                f"/{generation}/_search",
                {
                    "size": 0,
                    "query": {"bool": {"filter": scope_filters(tenant_id, knowledge_base_id)}},
                    "aggs": {"documents": {"composite": composite}},
                },
                {"allow_partial_search_results": "false"},
            )
            self._check_complete(response)
            documents = object_value(object_value(response.get("aggregations")).get("documents"))
            buckets = documents.get("buckets")
            if not isinstance(buckets, list):
                raise Error("invalid_search_response", "索引资料清单无效")
            count += len(buckets)
            next_key = documents.get("after_key")
            if not buckets or next_key is None:
                return count
            next_after = object_value(next_key)
            if next_after == after:
                raise Error("invalid_search_response", "索引资料清单游标没有推进")
            after = next_after

    @staticmethod
    def _check_complete(response: dict[str, JsonValue]) -> None:
        """拒绝超时或部分分片成功，不把部分命中冒充完整查询。"""
        if (
            response.get("timed_out") is not False
            or object_value(response.get("_shards")).get("failed") != 0
        ):
            raise Error("partial_search_result", "检索后端未完整执行请求")

    def query(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        generation: str,
        query: str,
        *,
        top_k: int = 5,
        mode: Literal["hybrid", "vector", "keyword"] = "hybrid",
        filters: list[SearchFilter] | None = None,
        excluded_document_ids: list[str] | None = None,
    ) -> list[NodeWithScore]:
        """对固定generation执行范围内Top-K，两个通道先过滤，失败绝不返回假空结果。"""
        hits, _diagnostic = self._query(
            tenant_id,
            knowledge_base_id,
            generation,
            query,
            top_k=top_k,
            mode=mode,
            filters=filters,
            excluded_document_ids=excluded_document_ids,
        )
        return hits

    def _vector_query(self, query: str, top_k: int, scope: list[JsonValue]) -> dict[str, JsonValue]:
        """对原始问题生成并校验向量，随后编译受限的 OpenSearch 查询。"""
        vector = self.embedding.get_query_embedding(query)
        check_vector(vector, self.dimensions)
        return vector_query(vector, top_k, scope)

    def _query(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        generation: str,
        query: str,
        *,
        top_k: int,
        mode: Literal["hybrid", "vector", "keyword"],
        filters: list[SearchFilter] | None,
        excluded_document_ids: list[str] | None,
        diagnostics: bool = False,
    ) -> tuple[list[NodeWithScore], dict[str, JsonValue] | None]:
        """共享一次搜索响应；详细解释只在显式诊断时启用且有严格输入与落盘预算。"""
        prepared = validate_query(query, top_k, mode)
        query, top_k = prepared.text, prepared.top_k
        if diagnostics and (top_k > 20 or len(query) > 1000):
            raise Error("invalid_query", "详细诊断最多返回20条，问题最多1000字，请缩小后重试")
        binding = self._binding(tenant_id, knowledge_base_id, generation)
        self._require_frozen(generation)
        clauses = compile_filters(tenant_id, knowledge_base_id, filters or [], binding.field_schema)
        excluded = excluded_document_ids or []
        if len(excluded) > 100_000 or any(
            not re.fullmatch(r"[0-9a-f]{32}", identity) for identity in excluded
        ):
            raise Error("invalid_scope", "撤销范围无效或超过当前安全预算")
        # 撤销条件来自可信应用层，同时进入关键词与向量候选阶段。
        if excluded:
            clauses.append({"bool": {"must_not": [{"terms": {"document_id": list(excluded)}}]}})
        params: dict[str, str] = {"allow_partial_search_results": "false"}
        search_query: dict[str, JsonValue]
        if prepared.mode == "vector":
            executed_mode = "vector"
            search_query = self._vector_query(query, top_k, clauses)
        else:
            keywords = prepared.keywords(opensearch_analyzer(binding.lexical_profile))
            executed_mode = retrieval_mode(prepared, keywords)
            search_query = keyword_query(keywords, clauses)
            if executed_mode == "hybrid":
                search_query = {
                    "hybrid": {"queries": [search_query, self._vector_query(query, top_k, clauses)]}
                }
                params["search_pipeline"] = RRF_PIPELINE
        body: dict[str, JsonValue] = {
            "size": top_k,
            "query": search_query,
            "_source": ["node", "tenant_id", "knowledge_base_id"],
        }
        if diagnostics:
            body["explain"] = True
            if executed_mode == "hybrid":
                # 原生内联流水线与正常RRF参数完全相同，只增加同次响应解释。
                params.pop("search_pipeline", None)
                body["search_pipeline"] = rrf_pipeline(explain=True)
        response = self._request(
            "POST",
            f"/{generation}/_search",
            body,
            params,
        )
        self._check_complete(response)
        hits = object_value(response.get("hits")).get("hits")
        if not isinstance(hits, list):
            raise Error("invalid_search_response", "检索后端缺少命中列表")
        results: list[NodeWithScore] = []
        explanations: list[JsonValue] = []
        for item in hits:
            hit = object_value(item)
            source = object_value(hit.get("_source"))
            if (
                source.get("tenant_id") != tenant_id
                or source.get("knowledge_base_id") != knowledge_base_id
            ):
                raise Error("scope_violation", "检索后端返回范围外结果")
            node = TextNode.model_validate(source.get("node"))
            if (
                node.metadata.get("tenant_id") != tenant_id
                or node.metadata.get("knowledge_base_id") != knowledge_base_id
                or node.metadata.get("document_id") in excluded
            ):
                raise Error("scope_violation", "检索节点不属于当前可信范围")
            score = hit.get("_score")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise Error("invalid_search_response", "检索分数无效")
            results.append(NodeWithScore(node=node, score=float(score)))
            if diagnostics:
                origin = object_value(node.metadata.get("parse_origin"))
                explanations.append(
                    {
                        "rank": len(results),
                        "document_id": node.metadata.get("document_id"),
                        "result_id": origin.get("result_id"),
                        "element_ref": origin.get("element_ref"),
                        "score": float(score),
                        **native_explanation(
                            hit.get("_explanation"), float(score), executed_mode, len(results)
                        ),
                    }
                )
        diagnostic: dict[str, JsonValue] | None = None
        if diagnostics:
            diagnostic = {
                "kind": "opensearch_explain",
                "same_response": True,
                "mode": executed_mode,
                "returned_hit_count": len(results),
                "hits": explanations,
                "pre_filter_candidate_count": None,
                "channel_candidate_totals": None,
                "scope": "authorized_final_hits_only",
            }
        return results, diagnostic

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
        """为可选HTTP宿主映射证据，不要求parse-only宿主导入原生RAG节点。"""
        parsed = [SearchFilter.model_validate(value) for value in filters or []]
        hits, diagnostic = self._query(
            tenant_id,
            knowledge_base_id,
            generation,
            query,
            top_k=top_k,
            mode=mode,
            filters=parsed,
            excluded_document_ids=excluded_document_ids,
            diagnostics=diagnostics,
        )
        response: dict[str, JsonValue] = {
            "generation": generation,
            "hits": [
                {
                    "text": hit.get_content(),
                    "score": hit.score,
                    "metadata": TypeAdapter(dict[str, JsonValue]).validate_python(hit.metadata),
                }
                for hit in hits
            ],
        }
        if diagnostic is not None:
            response["diagnostics"] = diagnostic
        return response


def result_nodes(
    tenant_id: str,
    knowledge_base_id: str,
    document_id: str,
    result: ParseResult,
    processing: ProcessingConfig,
    *,
    tokenizer: Callable[[str], list[int]] | None = None,
    max_tokens: int | None = None,
    tokenizer_revision: str | None = None,
) -> list[TextNode]:
    """复用原生结构切分，保留正文、表格和已审校图片说明的来源定位。"""
    revision = processing_revision(
        processing, tokenizer_revision=tokenizer_revision, max_tokens=max_tokens
    )

    def document_for(_ordinal: int, item: NativeText, origin: ParseOrigin) -> Document:
        """为共享来源补充平台作用域。"""
        return Document(
            text=item.text,
            metadata={
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "document_id": document_id,
                "format": result.source.format,
                "page": item.page,
                "section": item.section,
                "processing_revision": revision,
                "source": {"name": result.source.name},
                "parse_origin": origin.model_dump(mode="json"),
            },
        )

    chunks = prepare_result_nodes(
        result, processing, document_for, tokenizer=tokenizer, max_tokens=max_tokens
    )
    result_nodes: list[TextNode] = []
    for ordinal, chunk in enumerate(chunks):
        identity = hashlib.sha256(
            json.dumps(
                [
                    tenant_id,
                    knowledge_base_id,
                    document_id,
                    result.result_id,
                    revision,
                    ordinal,
                    chunk.get_content(),
                ],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        node = TextNode(id_=identity, text=chunk.get_content(), metadata=chunk.metadata)
        node.excluded_embed_metadata_keys = list(node.metadata)
        node.excluded_llm_metadata_keys = list(node.metadata)
        result_nodes.append(node)
    return result_nodes
