"""原生BaseEmbedding的窄HTTP适配，连接独立TEI推理，在线/批处理可分池。"""

import asyncio
import hashlib
import math
from pathlib import Path
from typing import override

import httpx
from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
)
from tokenizers import Tokenizer

from .models import DomainError

MODEL_ID = "BAAI/bge-small-zh-v1.5"
MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
TOKENIZER_SHA256 = "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"
TOKENIZER_PATH = Path(__file__).parent / "resources" / "bge-small-zh-v1.5" / "tokenizer.json"


class ServiceBudget(BaseModel):
    """只解释TEI实际公开的模型身份和服务预算，未知遥测字段不进入业务含义。"""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)
    model_id: str
    model_sha: str
    model_dtype: str
    model_type: dict[str, JsonValue]
    max_input_length: int = Field(ge=4, le=8192)
    max_client_batch_size: int = Field(ge=1, le=1024)
    max_batch_tokens: int = Field(ge=1, le=1048576)


def load_tokenizer(revision: str) -> Tokenizer:
    """仅加载随包附带的固定工件，摘要与权重身份不匹配时拒绝，不在线下载或截断。"""
    if (
        revision != MODEL_REVISION
        or hashlib.sha256(TOKENIZER_PATH.read_bytes()).hexdigest() != TOKENIZER_SHA256
    ):
        raise ValueError("本地分词器与固定模型revision或工件摘要不匹配")
    tokenizer = Tokenizer.from_file(str(TOKENIZER_PATH))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


class RemoteEmbedding(BaseEmbedding):
    """模型身份绑定不可变权重与查询前缀，不按同维度猜测兼容。"""

    _client: httpx.Client = PrivateAttr()
    _dimensions: int = PrivateAttr()
    _query_prefix: str = PrivateAttr()
    _tokenizer: Tokenizer = PrivateAttr()
    _budget: ServiceBudget = PrivateAttr()

    def __init__(
        self,
        url: str,
        revision: str,
        *,
        query_prefix: str = "为这个句子生成表示以用于检索相关文章：",
    ) -> None:
        """启动时核对推理服务权重并探测真实维度；失败不使用替代向量。"""
        super().__init__(
            model_name=f"{MODEL_ID}@{revision}:cls:normalized:{query_prefix}:tokenizer-{TOKENIZER_SHA256}"
        )
        self._tokenizer = load_tokenizer(revision)
        self._client = httpx.Client(
            base_url=url.rstrip("/"), timeout=httpx.Timeout(60, connect=3), trust_env=False
        )
        self._query_prefix = query_prefix
        try:
            info = self._client.get("/info")
            info.raise_for_status()
            self._budget = ServiceBudget.model_validate_json(info.content)
            embedding_type = self._budget.model_type.get("embedding")
            if (
                self._budget.model_sha != revision
                or self._budget.model_id != MODEL_ID
                or self._budget.max_input_length != 512
                or not isinstance(embedding_type, dict)
                or embedding_type.get("pooling") != "cls"
            ):
                raise ValueError("嵌入服务模型身份、池化或长度契约不匹配")
            self.embed_batch_size = min(self._budget.max_client_batch_size, 128)
            probe = self._encode(["模型维度验证"], expected=None)
            self._dimensions = len(probe[0])
        except Exception:
            self._client.close()
            raise

    @property
    def dimensions(self) -> int:
        """返回由真实向量探测的固定维度。"""
        return self._dimensions

    @property
    def max_text_tokens(self) -> int:
        """从真实输入上限扣除原生特殊token预算，用于正文切分而非猜测字符数。"""
        return self._budget.max_input_length - self._tokenizer.num_special_tokens_to_add(False)

    @property
    def metadata(self) -> dict[str, JsonValue]:
        """返回可公开的完整模型工件身份与预算，不包含端点或连接凭据。"""
        return {
            "revision": self.model_name,
            "dimensions": self.dimensions,
            "model_id": self._budget.model_id,
            "weights_revision": self._budget.model_sha,
            "dtype": self._budget.model_dtype,
            "pooling": "cls",
            "normalized": True,
            "query_prefix": self._query_prefix,
            "tokenizer_sha256": TOKENIZER_SHA256,
            "max_input_tokens": self._budget.max_input_length,
            "max_client_batch_size": self._budget.max_client_batch_size,
            "max_batch_tokens": self._budget.max_batch_tokens,
        }

    def tokenize(self, text: str) -> list[int]:
        """提供与TEI同工件的无特殊token原生分词，供LlamaIndex切分器注入。"""
        return self._tokenizer.encode(text, add_special_tokens=False).ids

    def close(self) -> None:
        """释放本适配器创建的HTTP连接池。"""
        self._client.close()

    def _encode(self, texts: list[str], expected: int | None) -> list[list[float]]:
        """调用TEI有界批接口，验证数量、维度及数值，拒绝静默截断。"""
        if not texts or len(texts) > 128 or any(not t.strip() or len(t) > 32000 for t in texts):
            raise DomainError("invalid_embedding_input", "嵌入批次为空或超出输入预算")
        lengths = [len(self._tokenizer.encode(text, add_special_tokens=True).ids) for text in texts]
        if any(length > self._budget.max_input_length for length in lengths):
            raise DomainError(
                "embedding_input_too_long", "输入加模型特殊token后超过512；请缩短查询或调整切分配置"
            )
        results: list[list[float]] = []
        batch: list[str] = []
        longest = 0
        for text, length in zip(texts, lengths, strict=True):
            padded_cost = max(longest, length) * (len(batch) + 1)
            if batch and (
                len(batch) >= self._budget.max_client_batch_size
                or padded_cost > self._budget.max_batch_tokens
            ):
                results.extend(self._request_vectors(batch, expected))
                batch, longest = [], 0
            if length > self._budget.max_batch_tokens:
                raise DomainError("invalid_embedding_input", "单条输入超过推理服务批token预算")
            batch.append(text)
            longest = max(longest, length)
        if batch:
            results.extend(self._request_vectors(batch, expected))
        return results

    def _request_vectors(self, texts: list[str], expected: int | None) -> list[list[float]]:
        """每次请求同时遵守服务批条数和保守padding预算，响应必须完整且有限。"""
        response = self._client.post(
            "/embed", json={"inputs": texts, "normalize": True, "truncate": False}
        )
        response.raise_for_status()
        try:
            vectors = TypeAdapter(list[list[float]]).validate_json(response.content, strict=True)
        except ValidationError:
            raise DomainError("invalid_embedding_response", "嵌入服务返回了无效向量结构") from None
        if len(vectors) != len(texts) or any(
            not v
            or (expected is not None and len(v) != expected)
            or not all(math.isfinite(n) for n in v)
            or not any(v)
            for v in vectors
        ):
            raise DomainError(
                "invalid_embedding_response", "嵌入服务返回了数量、维度或数值无效的向量"
            )
        return vectors

    @override
    def _get_query_embedding(self, query: str) -> list[float]:
        """在线查询带固定BGE提示，与入库正文嵌入语义区分。"""
        return self._encode([self._query_prefix + query], self._dimensions)[0]

    @override
    async def _aget_query_embedding(self, query: str) -> list[float]:
        """异步调用方不在事件循环执行阻塞网络请求。"""
        return await asyncio.to_thread(self._get_query_embedding, query)

    @override
    def _get_text_embedding(self, text: str) -> list[float]:
        """单段文字使用批接口同一验证。"""
        return self._encode([text], self._dimensions)[0]

    @override
    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        """LlamaIndex批量嵌入直接交给独立推理服务批处理。"""
        return self._encode(texts, self._dimensions)
