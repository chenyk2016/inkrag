"""沿明确登记的关系查找原文，每条边都需要当前索引中的证据。"""

import hashlib
import json
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .contracts import Chunk, Hit, IndexReader


def _valid_label(value: object) -> bool:
    """接受能稳定保存和查找的短引用标识。"""
    return isinstance(value, str) and 0 < len(value) <= 512 and value.strip() == value


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """从一个引用名指向另一个引用名，chunk_id 标识支持这条关系的原文。"""

    source: str
    relation: str
    target: str
    chunk_id: str

    def __post_init__(self) -> None:
        """拒绝空值和超长标识。引用名大小写与原始登记保持一致。"""
        if not all(
            _valid_label(value)
            for value in (self.source, self.relation, self.target, self.chunk_id)
        ):
            raise ValueError("关系边的标识为空、过长或含首尾空白")


@dataclass(frozen=True, slots=True)
class GraphLimits:
    """限制最深路径、已访问节点、单点扩展和原始证据查询量。"""

    max_depth: int = 2
    max_nodes: int = 32
    max_fanout: int = 8
    max_edge_checks: int = 256

    def __post_init__(self) -> None:
        """可调预算有固定上限，目录过大时调用者可缩小种子和资料范围。"""
        limits = (
            (self.max_depth, 8),
            (self.max_nodes, 512),
            (self.max_fanout, 64),
            (self.max_edge_checks, 4096),
        )
        if any(type(value) is not int or not 1 <= value <= ceiling for value, ceiling in limits):
            raise ValueError("图查询预算超出允许范围")


_DEFAULT_LIMITS = GraphLimits()


@dataclass(frozen=True, slots=True)
class GraphPath:
    """由一个种子开始的路径，边和各边对应的原文按访问顺序对齐。"""

    seed: str
    edges: tuple[GraphEdge, ...] = ()
    evidence: tuple[Chunk, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphResult:
    """返回命中的原文、通往被访问节点的路径以及是否触发预算。"""

    hits: tuple[Hit, ...]
    paths: tuple[GraphPath, ...]
    truncated: bool


class ReferenceGraph:
    """以显式边构建有向邻接表，按最短路径提升与种子相近的证据。"""

    def __init__(self, edges: Iterable[GraphEdge]) -> None:
        """去重并排序，令同一份边集合具有稳定的遍历顺序及指纹。"""
        records = sorted(
            {(edge.source, edge.relation, edge.target, edge.chunk_id) for edge in edges}
        )
        self._fingerprint = hashlib.sha256(
            json.dumps(records, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        adjacency: dict[str, list[GraphEdge]] = {}
        seed_sources: dict[str, set[str]] = {}
        for source, relation, target, chunk_id in records:
            adjacency.setdefault(source, []).append(GraphEdge(source, relation, target, chunk_id))
            seed_sources.setdefault(chunk_id, set()).add(source)
        self._adjacency = {source: tuple(outgoing) for source, outgoing in adjacency.items()}
        self._seed_sources = {
            chunk_id: tuple(sorted(sources)) for chunk_id, sources in seed_sources.items()
        }

    @property
    def fingerprint(self) -> str:
        """返回显式边及其证据身份的稳定摘要，供构建或发布记录使用。"""
        return self._fingerprint

    def seeds_for(self, chunk_ids: Sequence[str], limit: int) -> tuple[str, ...]:
        """从已通过范围校验的命中证据找起点，按证据排名去重。"""
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError("图查询种子数量超出预算")
        if isinstance(chunk_ids, str) or len(chunk_ids) > 4096:
            raise ValueError("图查询命中的证据列表超出预算")
        seeds: dict[str, None] = {}
        for chunk_id in chunk_ids:
            for source in self._seed_sources.get(chunk_id, ()):
                seeds[source] = None
                if len(seeds) == limit:
                    return tuple(seeds)
        return tuple(seeds)

    def traverse(
        self,
        reader: IndexReader,
        seeds: Sequence[str],
        *,
        document_ids: frozenset[str] | None = None,
        limits: GraphLimits = _DEFAULT_LIMITS,
    ) -> GraphResult:
        """在已授权资料内广度优先查找第一条最短路径，缺失证据的边无法通行。

        reader 绑定当前索引版本。每个节点扩展前以受信过滤条件查证边的原文。
        此方法只读。存储异常和返回范围违规会抛出，预算截断留在返回结果中。
        """
        if isinstance(seeds, str) or len(seeds) > 512:
            raise ValueError("图查询种子应为最多 512 个引用名")
        if not all(_valid_label(seed) for seed in seeds):
            raise ValueError("图查询种子为空或超出标识预算")
        if document_ids == frozenset():
            return GraphResult((), (), False)
        starts = tuple(dict.fromkeys(seeds))
        truncated = len(starts) > limits.max_nodes
        visited = set(starts[: limits.max_nodes])
        queue = deque((seed, GraphPath(seed)) for seed in starts[: limits.max_nodes])
        paths: list[GraphPath] = []
        hits: dict[str, Hit] = {}
        checks_left = limits.max_edge_checks
        while queue:
            node, path = queue.popleft()
            outgoing = self._adjacency.get(node, ())
            truncated = truncated or len(outgoing) > checks_left
            candidates = outgoing[:checks_left]
            checks_left -= len(candidates)
            chunk_ids = tuple(dict.fromkeys(edge.chunk_id for edge in candidates))
            if not chunk_ids:
                continue
            proofs = _edge_proofs(reader, chunk_ids, document_ids)
            fanout = 0
            for edge in candidates:
                chunk = proofs.get(edge.chunk_id)
                if chunk is None or edge.target in visited:
                    continue
                if (
                    len(path.edges) >= limits.max_depth
                    or len(visited) >= limits.max_nodes
                    or fanout == limits.max_fanout
                ):
                    truncated = True
                    break
                visited.add(edge.target)
                fanout += 1
                next_path = GraphPath(path.seed, (*path.edges, edge), (*path.evidence, chunk))
                paths.append(next_path)
                hits.setdefault(chunk.id, Hit(chunk, 1 / len(next_path.edges)))
                queue.append((edge.target, next_path))
        return GraphResult(tuple(hits.values()), tuple(paths), truncated)


def _edge_proofs(
    reader: IndexReader, chunk_ids: tuple[str, ...], document_ids: frozenset[str] | None
) -> dict[str, Chunk]:
    """核对当前读取结果的证据身份和资料范围，违规即失败。"""
    requested = set(chunk_ids)
    proofs: dict[str, Chunk] = {}
    for chunk in reader.get(chunk_ids, document_ids=document_ids):
        if (
            chunk.id not in requested
            or chunk.id in proofs
            or (document_ids is not None and chunk.document_id not in document_ids)
        ):
            raise ValueError("图查询收到范围外或重复的原文证据")
        proofs[chunk.id] = chunk
    return proofs
