"""官方Celery实际使用的类型面，运行时始终调用已安装SDK。"""

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T", bound=Callable[[str], None])

class Configuration:
    """窄化配置入口，参数由Celery官方配置校验。"""
    def update(self, **kwargs: object) -> None: ...

class TaskMessage:
    """Celery协议2消息的官方namedtuple视图。"""

    headers: dict[str, object]
    properties: dict[str, str]
    body: object

class AMQP:
    """复用Celery内置消息编码而非自定义任务JSON。"""
    def create_task_message(
        self,
        task_id: str,
        name: str,
        *,
        args: tuple[str],
        kwargs: dict[str, object],
        ignore_result: bool,
    ) -> TaskMessage: ...

class Control:
    """官方远程控制ping，目标节点必须显式指定。"""
    def ping(
        self, *, destination: list[str], timeout: float
    ) -> list[dict[str, dict[str, str]]]: ...

class Celery:
    """应用与worker入口；不把Celery结果当作业务事实。"""

    conf: Configuration
    amqp: AMQP
    control: Control
    def __init__(self, main: str, *, broker: str) -> None: ...
    def task(self, *, name: str, ignore_result: bool) -> Callable[[T], T]: ...
    def worker_main(self, argv: list[str]) -> None: ...
    def close(self) -> None: ...
