"""真实PG/S3/Rabbit显式集成；宿主开发子进程不冒充Linux隔离验收。"""

import hashlib
import os
import sys
from uuid import uuid4

import pytest
from inkparse import load_stored_result
from inkrag_platform.broker import EXCHANGE, BrokerPublisher, relay_once
from inkrag_platform.models import DomainError, Principal
from inkrag_platform.settings import Settings
from inkrag_platform.wiring import open_runtime
from inkrag_platform.worker import BuildInputStorage, execute_job
from inkstore import StoredRecord
from kombu import Connection, Producer
from pydantic import SecretStr

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all(
            os.environ.get(key)
            for key in (
                "INKRAG_TEST_DATABASE_DSN",
                "INKRAG_TEST_RABBIT_URL",
                "INKRAG_TEST_S3_ENDPOINT",
            )
        ),
        reason="需要显式PG/Rabbit/S3测试后端与SDK凭据",
    ),
]


@pytest.fixture
def settings() -> Settings:
    """依赖只有显式环境配置，不启动Docker或覆盖调用方服务。"""
    return Settings(
        capabilities="parse",
        database_url=SecretStr(os.environ["INKRAG_TEST_DATABASE_DSN"]),
        rabbit_url=SecretStr(os.environ["INKRAG_TEST_RABBIT_URL"]),
        s3_endpoint=os.environ["INKRAG_TEST_S3_ENDPOINT"],
        oidc_issuer="http://unused.local",
        sandbox_required=sys.platform == "linux",
    )


@pytest.mark.skipif(sys.platform != "linux", reason="严格隔离子进程贯穿需要Linux执行面")
def test_real_parse_persistence_and_duplicate(settings: Settings) -> None:
    """真实资产和解析子进程完成后提交PG，重复任务不重复推进版本或attempt。"""
    principal = Principal(tenant_id="test-worker-" + uuid4().hex, subject="test", roles=["admin"])
    with open_runtime(settings) as runtime:
        repo = runtime.repo
        kb = repo.create_kb(principal, "解析贯穿")
        storage = runtime.assets_for(principal)
        asset = storage.put_bytes("# 生产解析\n\n原件和结果的引用必须稳定。".encode())
        job = repo.submit_parse(principal, kb.kb_id, "worker.md", asset.asset_id, "parse")
        execute_job(settings, job.job_id)
        done = repo.get_job(principal, job.job_id)
        assert done.status == "succeeded" and done.attempt == 1
        assert job.document_id
        version = repo.get_version(principal, job.document_id)
        parsed = load_stored_result(storage, version.result_id)
        assert parsed.source.asset_id == asset.asset_id
        assert repo.has_asset(principal, asset.asset_id)
        execute_job(settings, job.job_id)
        duplicate = repo.get_job(principal, job.job_id)
        assert duplicate.result == done.result and duplicate.attempt == 1


def test_real_outbox_confirm_retry_and_unroutable(settings: Settings) -> None:
    """确认失败保留意图，官方mandatory未路由不能被broker ack误判成功。"""
    principal = Principal(tenant_id="test-relay-" + uuid4().hex, subject="test", roles=["admin"])
    with open_runtime(settings) as runtime:
        repo = runtime.repo
        kb = repo.create_kb(principal, "消息贯穿")
        asset = runtime.assets_for(principal).put_bytes(b"RabbitMQ confirmation test")
        job = repo.submit_parse(principal, kb.kb_id, "relay.txt", asset.asset_id, "publish")
        # 保留真实任务身份，但不让测试投递产生额外业务执行。
        repo.cancel_job(principal, job.job_id)

        def failed_publish(job_id: str) -> None:
            """注入确认未知，模拟网络断连；不改变其它任务事件。"""
            raise DomainError("broker_unavailable", "test failure")

        assert relay_once(repo, failed_publish, job_id=job.job_id) == 0
        publisher = BrokerPublisher(settings)
        try:
            assert relay_once(repo, publisher.publish, job_id=job.job_id) == 1
            assert repo.claim_outbox(job_id=job.job_id) == []
        finally:
            publisher.close()
        connection = Connection(
            settings.rabbit_url.get_secret_value(),
            connect_timeout=5,
            heartbeat=30,
            transport_options={"confirm_publish": True},
        )
        try:
            connection.connect()
            producer = Producer(connection, exchange=EXCHANGE, serializer="json")
            with pytest.raises(Exception) as returned:
                producer.publish(
                    {"job_id": job.job_id},
                    exchange=EXCHANGE,
                    routing_key="no-route-" + uuid4().hex,
                    headers={},
                    serializer="json",
                    delivery_mode=2,
                    mandatory=True,
                    retry=False,
                    declare=[],
                    timeout=5,
                    confirm_timeout=5,
                )
            assert returned.type.__name__ == "ChannelError"
        finally:
            connection.release()


def test_actual_s3_record_input_budget_stops_cumulative_work(settings: Settings) -> None:
    """增量只累计实际读取的解析记录，第二份超额在解析模型解码之前明确停止。"""
    principal = Principal(
        tenant_id="test-input-budget-" + uuid4().hex, subject="test", roles=["admin"]
    )
    with open_runtime(settings) as runtime:
        storage = runtime.assets_for(principal)
        asset = storage.put_bytes(b"source")
        identities: list[str] = []
        for index in range(2):
            payload = '{"text":"' + str(index) + "x" * 700 + '"}'
            identity = hashlib.sha256(payload.encode()).hexdigest()
            storage.save_record(
                StoredRecord(record_id=identity, source_asset_id=asset.asset_id, payload=payload)
            )
            identities.append(identity)
        budget = BuildInputStorage(storage, 1024)
        assert budget.load_record(identities[0]).record_id == identities[0]
        assert 700 < budget.bytes_read < 1024
        with pytest.raises(DomainError) as rejected:
            budget.load_record(identities[1])
        assert rejected.value.code == "build_budget_exceeded"
