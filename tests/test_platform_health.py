"""后台角色使用自己的活性证据；过期心跳和其它worker的响应不能掩盖故障。"""

import os
import time
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest
from inkrag_platform.broker import run_relay
from inkrag_platform.health import relay_healthy, touch_relay_heartbeat, worker_healthy
from inkrag_platform.settings import Settings
from pydantic import SecretStr


def test_relay_heartbeat_missing_fresh_stale_and_future(tmp_path: Path) -> None:
    """真实文件时间反例覆盖缺失、成功循环、失联和异常未来时间。"""
    marker = tmp_path / "relay.health"
    assert not relay_healthy(marker)
    touch_relay_heartbeat(marker)
    now = time.time()
    assert relay_healthy(marker, now=now)
    os.utime(marker, (now - 31, now - 31))
    assert not relay_healthy(marker, now=now)
    os.utime(marker, (now + 60, now + 60))
    assert not relay_healthy(marker, now=now)
    marker.unlink()
    marker.mkdir()
    assert not relay_healthy(marker, now=now)


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("INKRAG_TEST_RABBIT_URL")
    or not os.environ.get("INKRAG_TEST_WORKER_HOSTNAME"),
    reason="需要真实Rabbit和明确目标的运行中Celery worker",
)
def test_real_worker_ping_is_scoped_to_target_node() -> None:
    """真实Rabbit只接受指定worker的pong；同一broker里的其它活节点不能代替它。"""
    settings = Settings(
        capabilities="parse",
        database_url=SecretStr("postgresql+psycopg://unused"),
        rabbit_url=SecretStr(os.environ["INKRAG_TEST_RABBIT_URL"]),
        s3_endpoint="http://unused",
        oidc_issuer="http://unused",
    )
    assert worker_healthy(settings, hostname=os.environ["INKRAG_TEST_WORKER_HOSTNAME"])
    assert not worker_healthy(settings, hostname="missing-" + uuid4().hex, timeout=1)


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("INKRAG_TEST_DATABASE_DSN") or not os.environ.get("INKRAG_TEST_RABBIT_URL"),
    reason="需要明确的测试PG与真实Rabbit，不能给现有业务数据库注入恢复循环",
)
def test_real_relay_loop_updates_heartbeat_and_stopped_marker_expires(tmp_path: Path) -> None:
    """真实恢复/投递控制循环刷新心跳，停止后过期标记不能保持健康。"""
    settings = Settings(
        capabilities="parse",
        database_url=SecretStr(os.environ["INKRAG_TEST_DATABASE_DSN"]),
        rabbit_url=SecretStr(os.environ["INKRAG_TEST_RABBIT_URL"]),
        s3_endpoint="http://unused",
        oidc_issuer="http://unused",
    )
    marker = tmp_path / "running-relay.health"
    stop = Event()
    process = Thread(
        target=run_relay,
        args=(settings,),
        kwargs={"stop": stop, "heartbeat_path": marker},
        daemon=True,
    )
    process.start()
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and process.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert relay_healthy(marker)
    finally:
        stop.set()
        process.join(timeout=15)
    assert not process.is_alive()
    stale = time.time() - 31
    os.utime(marker, (stale, stale))
    assert not relay_healthy(marker)
