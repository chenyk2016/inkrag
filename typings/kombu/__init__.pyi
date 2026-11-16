"""官方Kombu已使用连接、拓扑与确认发布的类型面。"""

class Exchange:
    """官方持久交换机定义。"""

    name: str
    def __init__(self, name: str, *, type: str, durable: bool) -> None: ...

class Queue:
    """显式quorum任务队列定义。"""
    def __init__(
        self,
        name: str,
        *,
        exchange: Exchange,
        routing_key: str,
        durable: bool,
        queue_arguments: dict[str, object],
    ) -> None: ...

class Connection:
    """AMQP连接生命周期，确认模式仍由py-amqp官方实现。"""
    def __init__(
        self,
        hostname: str,
        *,
        connect_timeout: int,
        heartbeat: int,
        transport_options: dict[str, object],
        ssl: dict[str, object] | bool = ...,
    ) -> None: ...
    def connect(self) -> None: ...
    def release(self) -> None: ...
    def heartbeat_check(self) -> None: ...

class Producer:
    """发布失败或路由返回直接抛出官方异常，调用层保留outbox。"""
    def __init__(self, channel: Connection, *, exchange: Exchange, serializer: str) -> None: ...
    def publish(
        self,
        body: object,
        *,
        exchange: Exchange,
        routing_key: str,
        headers: dict[str, object],
        serializer: str,
        delivery_mode: int,
        mandatory: bool,
        retry: bool,
        declare: list[Queue],
        timeout: int,
        confirm_timeout: int,
        **properties: str,
    ) -> None: ...
