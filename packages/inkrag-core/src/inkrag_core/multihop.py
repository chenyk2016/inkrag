"""让调用方选择怎样提出下一问，由同一入口限制检索和上下文。"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from threading import get_ident

from .contracts import Hit


@dataclass(frozen=True, slots=True)
class Hop:
    """一条实际执行的子查询与返回给策略的有界证据。"""

    query: str
    hits: tuple[Hit, ...]


type HopSearch = Callable[[str], Hop | None]
type MultiHopStrategy = Callable[[str, HopSearch], None]


@dataclass(frozen=True, slots=True)
class MultiHopLimits:
    """限制检索次数、证据候选数和每条问题长度。"""

    max_queries: int = 4
    max_evidence: int = 12
    max_query_chars: int = 4096

    def __post_init__(self) -> None:
        """把查询和上下文限制在进程内可控的区间。"""
        limits = ((self.max_queries, 16), (self.max_evidence, 100), (self.max_query_chars, 4096))
        if any(type(value) is not int or not 1 <= value <= ceiling for value, ceiling in limits):
            raise ValueError("多跳检索预算超出允许范围")


_DEFAULT_LIMITS = MultiHopLimits()


@dataclass(frozen=True, slots=True)
class MultiHopResult:
    """保存跨跳去重后的原文、实际各跳和是否因预算截断。"""

    hits: tuple[Hit, ...]
    hops: tuple[Hop, ...]
    truncated: bool


def decomposition(planner: Callable[[str], Iterable[str]]) -> MultiHopStrategy:
    """把预先拆题的函数转成策略，逐条执行拆出的问题。"""

    def plan(question: str, search: HopSearch) -> None:
        """拆题函数每次只执行一次，遇到重复查询或预算耗尽即停止。"""
        queries = planner(question)
        if isinstance(queries, str):
            raise ValueError("拆题策略应返回问题序列")
        for query in queries:
            if search(query) is None:
                break

    return plan


def iterative(next_query: Callable[[str, tuple[Hop, ...]], str | None]) -> MultiHopStrategy:
    """先查原问题，再用已有证据提出下一问，返回 None 表示结束。"""

    def plan(question: str, search: HopSearch) -> None:
        """每一跳只能读取此前实际返回且在预算内的原文。"""
        hops: list[Hop] = []
        query: str | None = question
        while query is not None:
            hop = search(query)
            if hop is None:
                break
            hops.append(hop)
            query = next_query(question, tuple(hops))

    return plan


def run_multihop(
    query: str,
    retrieve: Callable[[str], Sequence[Hit]],
    strategy: MultiHopStrategy,
    *,
    limits: MultiHopLimits = _DEFAULT_LIMITS,
) -> MultiHopResult:
    """运行调用方选择的策略，通过注入的固定范围函数取得原文。

    每次检索沿用注入函数绑定的索引、范围与路由。策略和检索错误均传给调用者。
    最终结果按各跳的相同名次交替取证，保留命中本身的分数和来源。
    """
    initial = _checked_query(query, limits.max_query_chars)
    hops: list[Hop] = []
    seen_queries: set[str] = set()
    evidence: dict[str, Hit] = {}
    truncated = False
    active = True
    caller_thread = get_ident()

    def search(proposed: str) -> Hop | None:
        """预算耗尽或重走已执行的问题时返回 None。"""
        nonlocal truncated
        if not active:
            raise RuntimeError("多跳查询已经结束")
        if get_ident() != caller_thread:
            raise RuntimeError("多跳策略需要按顺序同步查询")
        current = _checked_query(proposed, limits.max_query_chars)
        if current in seen_queries:
            return None
        if len(seen_queries) >= limits.max_queries:
            truncated = True
            return None
        seen_queries.add(current)
        incoming = retrieve(current)
        if len(incoming) > limits.max_evidence:
            truncated = True
        selected: dict[str, Hit] = {}
        for hit in incoming[: limits.max_evidence]:
            identity = hit.chunk.id
            previous = evidence.get(identity)
            if previous is not None and previous.chunk != hit.chunk:
                raise ValueError("多跳检索发现同一证据身份对应不同原文")
            if previous is None and len(evidence) == limits.max_evidence:
                truncated = True
                continue
            evidence.setdefault(identity, hit)
            selected.setdefault(identity, hit)
        hop = Hop(current, tuple(selected.values()))
        hops.append(hop)
        return hop

    try:
        strategy(initial, search)
    finally:
        active = False
    merged: dict[str, Hit] = {}
    for rank in range(max((len(hop.hits) for hop in hops), default=0)):
        for hop in hops:
            if rank < len(hop.hits):
                hit = hop.hits[rank]
                merged.setdefault(hit.chunk.id, hit)
    return MultiHopResult(tuple(merged.values()), tuple(hops), truncated)


def _checked_query(query: object, max_chars: int) -> str:
    """拒绝超预算、空白或类型异常的问题，避免提交给检索依赖。"""
    if not isinstance(query, str) or len(query) > max_chars or not query.strip() or "\0" in query:
        raise ValueError("多跳问题为空或超出长度预算")
    return query.strip()
