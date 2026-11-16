"""RabbitMQ只传任务身份；持久状态、恢复与投递意图始终留在PostgreSQL。"""

import logging
import ssl
from collections.abc import Callable
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit
from uuid import uuid4

from celery import Celery
from kombu import Connection, Exchange, Producer, Queue

from .health import RELAY_HEARTBEAT, touch_relay_heartbeat
from .models import DomainError
from .repository import Repository
from .settings import Settings

TASK_NAME = "inkrag.execute.v1"
QUEUE_NAME = "inkrag.jobs.v1"
EXCHANGE = Exchange("inkrag.jobs.v1", type="direct", durable=True)
DEAD_EXCHANGE = Exchange("inkrag.dead.v1", type="direct", durable=True)
DEAD_QUEUE = Queue(
    "inkrag.dead.v1",
    exchange=DEAD_EXCHANGE,
    routing_key="inkrag.dead.v1",
    durable=True,
    queue_arguments={"x-queue-type": "quorum"},
)
QUEUE = Queue(
    QUEUE_NAME,
    exchange=EXCHANGE,
    routing_key=QUEUE_NAME,
    durable=True,
    queue_arguments={
        "x-queue-type": "quorum",
        "x-delivery-limit": 20,
        "x-dead-letter-exchange": "inkrag.dead.v1",
        "x-dead-letter-routing-key": "inkrag.dead.v1",
        "x-dead-letter-strategy": "at-least-once",
        "x-overflow": "reject-publish",
    },
)
logger = logging.getLogger(__name__)


def _tls(settings: Settings) -> dict[str, object] | bool:
    """AMQPS同时验证证书链与目标主机；py-amqp不传server_hostname会跳主机校验。"""
    address = settings.rabbit_url.get_secret_value()
    if not address.startswith("amqps://"):
        return False
    hostname = urlsplit(address).hostname
    if hostname is None:
        raise ValueError("RabbitMQ地址缺少主机名")
    return {
        "cert_reqs": ssl.CERT_REQUIRED,
        "server_hostname": hostname,
        "tls_minimum_version": ssl.TLSVersion.TLSv1_2,
    }


def create_celery(settings: Settings) -> Celery:
    """显式绑定真实RabbitMQ和官方Celery协议，不使用结果后端保存业务状态。"""
    app = Celery("inkrag", broker=settings.rabbit_url.get_secret_value())
    app.conf.update(
        task_queues=[QUEUE],
        task_default_queue=QUEUE_NAME,
        task_default_exchange=EXCHANGE.name,
        task_default_exchange_type="direct",
        task_default_routing_key=QUEUE_NAME,
        task_create_missing_queues=False,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_protocol=2,
        task_ignore_result=True,
        task_store_errors_even_if_ignored=False,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_acks_on_failure_or_timeout=False,
        worker_prefetch_multiplier=1,
        worker_detect_quorum_queues=True,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        broker_transport_options={"confirm_publish": True},
        broker_use_ssl=_tls(settings),
        broker_connection_timeout=5,
        broker_connection_retry_on_startup=True,
        broker_heartbeat=30,
        task_soft_time_limit=settings.job_seconds,
        task_time_limit=settings.job_seconds + 30,
        enable_utc=True,
    )
    return app


class BrokerPublisher:
    """有界连接复用官方确认发布；无路由basic.return和nack均不能成为已送达。"""

    def __init__(self, settings: Settings) -> None:
        """构造不连接网络，首次有持久投递意图时才建立AMQP连接。"""
        self.settings = settings
        self.app = create_celery(settings)
        self.connection: Connection | None = None
        self.producer: Producer | None = None

    def _connect(self) -> Producer:
        """新连接启用publisher confirm，重连仍由PG outbox控制重投。"""
        if self.producer is None:
            connection = Connection(
                self.settings.rabbit_url.get_secret_value(),
                connect_timeout=5,
                heartbeat=30,
                transport_options={"confirm_publish": True},
                ssl=_tls(self.settings),
            )
            self.connection = connection
            connection.connect()
            self.producer = Producer(connection, exchange=EXCHANGE, serializer="json")
        return self.producer

    def publish(self, job_id: str) -> None:
        """官方Celery任务消息只携带job_id，确认且真实路由成功后才返回。

        py-amqp在等待confirm时处理basic.return，无返回处理器时抛出异常；
        因此mandatory未路由消息不会因随后收到ack而被误记为投递成功。
        """
        try:
            message = self.app.amqp.create_task_message(
                uuid4().hex, TASK_NAME, args=(job_id,), kwargs={}, ignore_result=True
            )
            self._connect().publish(
                message.body,
                exchange=EXCHANGE,
                routing_key=QUEUE_NAME,
                headers=message.headers,
                serializer="json",
                delivery_mode=2,
                mandatory=True,
                retry=False,
                declare=[DEAD_QUEUE, QUEUE],
                timeout=10,
                confirm_timeout=10,
                **message.properties,
            )
        except Exception:
            self.close()
            raise DomainError(
                "broker_unavailable", "消息路由或持久确认失败，保留投递意图"
            ) from None

    def heartbeat(self) -> None:
        """空闲Relay也维护AMQP心跳；失联只清连接，不改投递事实。"""
        try:
            if self.connection is not None:
                self.connection.heartbeat_check()
        except Exception:
            self.close()

    def close(self) -> None:
        """释放借入之外的AMQP连接，不删除持久队列或outbox。"""
        connection, self.connection, self.producer = self.connection, None, None
        if connection is not None:
            try:
                connection.release()
            except Exception:
                logger.warning("broker_close_failed")


def relay_once(
    repo: Repository,
    publish: Callable[[str], None],
    limit: int = 20,
    *,
    job_id: str | None = None,
) -> int:
    """短事务领取后在事务外发布，再按领取次数条件确认；重复投递由Worker幂等吸收。"""
    events = repo.claim_outbox(limit=limit, job_id=job_id)
    delivered = 0
    for event in events:
        try:
            publish(event.job_id)
        except Exception:
            repo.mark_outbox(event.event_id, event.attempts, delivered=False)
            logger.warning("outbox_publish_failed event_id=%s", event.event_id)
        else:
            repo.mark_outbox(event.event_id, event.attempts, delivered=True)
            delivered += 1
    return delivered


def run_relay(
    settings: Settings, *, stop: Event | None = None, heartbeat_path: Path = RELAY_HEARTBEAT
) -> None:
    """独立服务持续恢复过期租约与outbox，故障时有界退避，不丢已接受任务。"""
    signal = stop if stop is not None else Event()
    repo = Repository(settings.database_url.get_secret_value())
    publisher = BrokerPublisher(settings)
    try:
        repo.check()
        while not signal.is_set():
            try:
                repo.recover_jobs()
                delivered = relay_once(repo, publisher.publish)
                publisher.heartbeat()
                touch_relay_heartbeat(heartbeat_path)
                signal.wait(0.1 if delivered else 1)
            except DomainError as exc:
                logger.error("relay_failed code=%s", exc.code)
                signal.wait(2)
    finally:
        publisher.close()
        repo.close()
