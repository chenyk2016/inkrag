"""把硅基流动托管的文本向量模型接到原生嵌入接口。"""

import asyncio
import math
import os
from typing import override

import httpx
from llama_index.core.embeddings import BaseEmbedding
from pydantic import BaseModel, JsonValue, PrivateAttr, SecretStr

from .models import Error

MODEL = "BAAI/bge-m3"
DIMENSIONS = 1024


class _Vector(BaseModel):
    """读取一个向量及其对应的输入位置。"""

    index: int
    embedding: list[float]


class _EmbeddingResponse(BaseModel):
    """校验嵌入接口返回的向量结构。"""

    data: list[_Vector]


class SiliconFlowEmbedding(BaseEmbedding):
    """显式使用硅基流动；仅将待嵌入的片段和查询发送给模型服务。"""

    _api_key: SecretStr = PrivateAttr()
    _model: str = PrivateAttr()
    _dimensions: int | None = PrivateAttr()
    _timeout: float = PrivateAttr()

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = MODEL,
        dimensions: int | None = None,
        embed_batch_size: int = 16,
        timeout: float = 30,
    ) -> None:
        """选择模型、输出维度、批大小及超时，构造时不请求网络。"""
        key = api_key if api_key is not None else os.environ.get("SILICONFLOW_API_KEY", "")
        if not key.strip():
            raise Error("missing_embedding_key", "请配置 SILICONFLOW_API_KEY 或传入 api_key")
        model = model.strip()
        if not model:
            raise Error("invalid_model", "model 不能为空")
        if dimensions is not None and (
            type(dimensions) is not int
            or dimensions < 1
            or not model.removeprefix("Pro/").startswith("Qwen/Qwen3-")
        ):
            raise Error("invalid_model", "dimensions 仅用于支持指定维度的 Qwen3 模型")
        if type(embed_batch_size) is not int or embed_batch_size < 1:
            raise Error("invalid_model", "embed_batch_size 必须是正整数")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise Error("invalid_model", "timeout 必须是有限正数")
        model_name = f"siliconflow:{model}"
        if dimensions is not None:
            model_name += f":dimensions={dimensions}"
        super().__init__(model_name=model_name, embed_batch_size=embed_batch_size)
        self._api_key = SecretStr(key.strip())
        self._model, self._dimensions, self._timeout = model, dimensions, timeout

    @override
    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        """按输入位置返回向量，拒绝缺失、重复和维度不符的结果。"""
        payload: dict[str, JsonValue] = {
            "model": self._model,
            "input": [*texts],
            "encoding_format": "float",
        }
        if self._dimensions is not None:
            payload["dimensions"] = self._dimensions
        with httpx.Client(timeout=self._timeout, trust_env=False) as client:
            response = client.post(
                "https://api.siliconflow.cn/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                json=payload,
            )
            response.raise_for_status()
        rows = sorted(
            _EmbeddingResponse.model_validate_json(response.content).data, key=lambda row: row.index
        )
        expected = self._dimensions
        if expected is None and self._model.removeprefix("Pro/") == MODEL:
            expected = DIMENSIONS
        sizes = {len(row.embedding) for row in rows}
        if (
            [row.index for row in rows] != list(range(len(texts)))
            or len(sizes) != 1
            or 0 in sizes
            or (expected is not None and sizes != {expected})
        ):
            raise Error("invalid_embedding", "硅基流动返回的向量数量、位置或维度不符")
        return [row.embedding for row in rows]

    @override
    def _get_text_embedding(self, text: str) -> list[float]:
        """为单条资料片段生成向量。"""
        return self._get_text_embeddings([text])[0]

    @override
    def _get_query_embedding(self, query: str) -> list[float]:
        """查询和入库使用同一模型及向量约定。"""
        return self._get_text_embedding(query)

    @override
    async def _aget_query_embedding(self, query: str) -> list[float]:
        """在线程中执行同步请求，避免阻塞异步调用方。"""
        return await asyncio.to_thread(self._get_query_embedding, query)
