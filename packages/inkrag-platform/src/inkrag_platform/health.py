"""按进程职责探测活性，不把HTTP端口、队列响应或循环心跳当作任务完成证据。"""

import argparse
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .settings import Settings

RELAY_HEARTBEAT = Path("/tmp/inkrag-relay.health")


def touch_relay_heartbeat(path: Path = RELAY_HEARTBEAT) -> None:
    """仅完成一次正常控制循环后更新时间；标记是临时活性信息，不是业务事实。"""
    path.touch(exist_ok=True)


def relay_healthy(path: Path = RELAY_HEARTBEAT, *, now: float | None = None) -> bool:
    """确认常规心跳文件不超过30秒，缺失、未来时间或不可读都视为未就绪。"""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        age = (time.time() if now is None else now) - path.stat().st_mtime
        return 0 <= age <= 30
    except OSError:
        return False


def worker_healthy(
    settings: "Settings", *, hostname: str | None = None, timeout: float = 3
) -> bool:
    """官方Celery定向ping本容器worker，防止其它容器的pong掩盖本进程故障。"""
    from .broker import create_celery

    app = create_celery(settings)
    target = "celery@" + (socket.gethostname() if hostname is None else hostname)
    try:
        replies = app.control.ping(destination=[target], timeout=timeout)
        return any(reply.get(target, {}).get("ok") == "pong" for reply in replies)
    except Exception:
        return False
    finally:
        app.close()


def main() -> int:
    """容器独立健康命令；API沿用HTTP探测，迁移任务不运行长驻探测。"""
    parser = argparse.ArgumentParser(description="检查InkRAG后台进程活性")
    parser.add_argument("role", choices=["worker", "relay"])
    arguments = parser.parse_args()
    try:
        if arguments.role == "relay":
            return 0 if relay_healthy() else 1
        from .settings import load_settings

        return 0 if worker_healthy(load_settings()) else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
