"""已发布知识库的进程内查询入口，查询规则由宿主提供。"""

from collections.abc import Callable
from typing import Literal

from pydantic import JsonValue

from .models import Error

type PublishedQuery = Callable[[dict[str, JsonValue]], dict[str, JsonValue]]


class PublishedRetrieval:
    """在连接作用域内查询已发布知识库，返回与服务一致的证据结构。"""

    def __init__(self, execute: PublishedQuery) -> None:
        """借入宿主查询函数，连接由外部上下文持有。"""
        self._execute: PublishedQuery | None = execute

    def retrieve(
        self,
        query: str,
        *,
        mode: Literal["keyword", "vector", "hybrid"] | None = None,
        top_k: int | None = None,
        filters: list[dict[str, JsonValue]] | None = None,
    ) -> dict[str, JsonValue]:
        """保留未传参数的含义，由已发布服务校验查询并决定默认策略。"""
        execute = self._execute
        if execute is None:
            raise Error("closed", "已发布知识库连接已关闭")
        request: dict[str, JsonValue] = {"query": query}
        if mode is not None:
            request["mode"] = mode
        if top_k is not None:
            request["top_k"] = top_k
        if filters is not None:
            request["filters"] = [*filters]
        return execute(request)

    def close(self) -> None:
        """释放对宿主查询函数的引用，并拒绝连接作用域之外的调用。"""
        self._execute = None
