"""公开的证据、索引和模型接口，可在任意 Python 应用里实现。"""

import math
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Source:
    """原件名称以及可由调用方打开的元素、页或解析版本。"""

    name: str
    element_ref: str
    page: int | None = None
    result_id: str | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Chunk:
    """索引和检索使用的原文片段。相同原文和来源生成稳定 ID。"""

    id: str
    document_id: str
    text: str
    source: Source


@dataclass(frozen=True, slots=True)
class IndexProfile:
    """索引绑定的模型语义、向量维度和分词配置，应随不兼容变更更新。"""

    embedding_id: str
    dimensions: int
    analyzer_id: str

    def __post_init__(self) -> None:
        """拒绝不能持久化或无法比较的配置。"""
        if type(self.dimensions) is not int or not 1 <= self.dimensions <= 4096:
            raise ValueError("向量维度必须在 1 至 4096 之间")
        if any(
            not value.strip() or len(value) > 255 for value in (self.embedding_id, self.analyzer_id)
        ):
            raise ValueError("模型和分词器应各提供一个非空且稳定的标识")


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    """一次原子写入提交的片段、词项与向量。词项允许重复以保留词频。"""

    chunk: Chunk
    terms: tuple[str, ...]
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Hit:
    """一路候选或融合后的结果。分数只在各自的排名算法内解释。"""

    chunk: Chunk
    score: float


class Embedding(Protocol):
    """输入一批文本，按相同顺序给出定长向量。ID 应包含模型版本和提示策略。"""

    @property
    def fingerprint(self) -> str:
        """返回绑定索引的模型身份。"""
        ...

    @property
    def dimensions(self) -> int:
        """返回固定的向量维度。"""
        ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """按输入顺序生成一一对应的向量；失败抛出异常。"""
        ...


class LexicalAnalyzer(Protocol):
    """为 BM25 提供词项，让同一套分词策略用于入库和查询。"""

    @property
    def fingerprint(self) -> str:
        """返回包括词典和算法版本的分词身份。"""
        ...

    def analyze(self, text: str) -> tuple[str, ...]:
        """按固定规则产生非空词项序列，重复词代表词频。"""
        ...


class IndexReader(Protocol):
    """在同一个只读快照上响应所有通道。范围过滤应先于排序。"""

    def keyword(
        self, terms: Sequence[str], limit: int, document_ids: frozenset[str] | None = None
    ) -> list[Hit]:
        """用指定词项检索并按最相关优先排序。"""
        ...

    def vector(
        self, query: Sequence[float], limit: int, document_ids: frozenset[str] | None = None
    ) -> list[Hit]:
        """在相同范围内按向量检索。"""
        ...

    def get(
        self, chunk_ids: Sequence[str], document_ids: frozenset[str] | None = None
    ) -> list[Chunk]:
        """按 ID 读取仍存在、仍在范围内的片段。"""
        ...


class Index(Protocol):
    """原子替换资料，两路索引与原文片段作为一个提交单元。"""

    @property
    def profile(self) -> IndexProfile:
        """返回当前集合的持久配置。"""
        ...

    def replace(self, document_id: str, rows: Sequence[IndexedChunk]) -> None:
        """原子替换整个资料。空序列表示删除。"""
        ...

    def read(self) -> AbstractContextManager[IndexReader]:
        """打开一致的只读快照并在退出时释放资源。"""
        ...


def validate_vector(vector: Sequence[float], dimensions: int) -> tuple[float, ...]:
    """校验可安全存储和比较的有限非零向量。"""
    if len(vector) != dimensions or any(
        isinstance(value, bool) or not math.isfinite(value) or abs(value) > 1e18 for value in vector
    ):
        raise ValueError("向量维度不符，或包含超出范围的分量")
    if math.hypot(*vector) < 1e-18:
        raise ValueError("余弦检索需要非零向量")
    return tuple(float(value) for value in vector)
