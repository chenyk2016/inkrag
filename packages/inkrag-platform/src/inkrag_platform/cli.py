"""零配置本地完整运行，外部环境通过明确变量与独立进程入口接入。"""

import argparse
import os
import signal
import sys
from pathlib import Path
from threading import Event
from types import FrameType
from urllib.parse import parse_qs, urlsplit


def _migrate() -> None:
    """独立迁移只读取迁移连接和运行模式，不加载业务进程凭据。"""
    mode = os.environ.get("INKRAG_MODE", "development")
    if mode not in {"development", "production"}:
        raise RuntimeError("INKRAG_MODE 必须是 development 或 production")
    dsn = os.environ.get("INKRAG_MIGRATION_URL", "")
    if not dsn.startswith("postgresql+psycopg://"):
        raise RuntimeError("INKRAG_MIGRATION_URL 必须是专用 PostgreSQL/psycopg 连接")
    if mode == "production" and parse_qs(urlsplit(dsn).query, keep_blank_values=True).get(
        "sslmode"
    ) != ["verify-full"]:
        raise RuntimeError("生产迁移连接要求 sslmode=verify-full")

    from .database import Database

    database = Database(dsn)
    try:
        database.migrate()
    finally:
        database.close()
    print("数据库版本迁移完成。", flush=True)


def main() -> int:
    """默认初始化本地Docker；API/worker启动不会隐式迁移或创建后端。"""
    parser = argparse.ArgumentParser(prog="inkrag", description="独立文档解析与检索平台")
    parser.add_argument(
        "command",
        nargs="?",
        default="up",
        choices=["up", "down", "status", "api", "worker", "relay", "migrate"],
    )
    parser.add_argument("--rebuild", action="store_true", help="重新构建当前源码应用镜像")
    arguments = parser.parse_args()
    try:
        if arguments.command in {"up", "down", "status"}:
            from .bootstrap import manage, up

            if arguments.command == "up":
                up(rebuild=arguments.rebuild)
            else:
                manage(arguments.command)
            return 0
        if arguments.command == "migrate":
            _migrate()
            return 0
        from .settings import load_settings

        settings = load_settings()
        if arguments.command == "api":
            import uvicorn

            from .api import create_app
            from .wiring import open_runtime

            with open_runtime(settings) as runtime:
                application = create_app(runtime, web_dir=Path(__file__).parent / "static")
                uvicorn.run(
                    application,
                    host=settings.bind,
                    port=settings.port,
                    # 默认访问日志包含查询串中的文件名；安全错误另以请求ID记录。
                    access_log=False,
                    proxy_headers=False,
                    limit_concurrency=32,
                    backlog=512,
                    timeout_keep_alive=5,
                    timeout_graceful_shutdown=30,
                )
        else:
            from .broker import run_relay
            from .sandbox import protect_process
            from .worker import run_worker

            protect_process()
            if arguments.command == "worker":
                run_worker(settings)
            else:
                stop = Event()

                def shutdown(_signum: int, _frame: FrameType | None) -> None:
                    """停止接受新的outbox领取，已领取投递完成后关闭连接。"""
                    stop.set()

                signal.signal(signal.SIGTERM, shutdown)
                signal.signal(signal.SIGINT, shutdown)
                run_relay(settings, stop=stop)
        return 0
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            "启动或运行失败（" + type(exc).__name__ + "）；请检查服务状态及配置。", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
