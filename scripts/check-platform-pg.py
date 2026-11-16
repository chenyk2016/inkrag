"""在独立PostgreSQL测试库运行回归；初始化与测试运行权限分离，绝不打印DSN。"""

import os
import subprocess
import sys
from pathlib import Path

from inkrag_platform.database import Database
from inkrag_platform.models import DomainError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError


class TestConfigurationError(Exception):
    """只包含可公开的配置说明，不附带URL或数据库异常原文。"""


def test_url(value: str) -> URL:
    """显式测试连接不能指向正式库或数据库系统库。"""
    try:
        url = make_url(value)
    except (ValueError, SQLAlchemyError):
        raise TestConfigurationError("测试连接格式无效，未输出原始连接信息") from None
    if url.get_backend_name() != "postgresql" or not url.database:
        raise TestConfigurationError("测试连接必须指向独立PostgreSQL数据库")
    if url.database.casefold() in {"inkrag", "postgres", "keycloak", "template0", "template1"}:
        raise TestConfigurationError("拒绝将正式inkrag库或系统库作为测试数据库")
    return url


def check_runtime(url: URL) -> None:
    """只使用受限运行身份核对迁移与权限，不以管理员身份执行测试。"""
    database = Database(url.render_as_string(hide_password=False))
    try:
        database.check()
    finally:
        database.close()


def migrate(url: URL) -> None:
    """迁移连接不传给pytest子进程。"""
    database = Database(url.render_as_string(hide_password=False))
    try:
        database.migrate()
    finally:
        database.close()


def prepare_explicit(value: str) -> URL:
    """CI可提供预初始化测试连接，或额外提供同库的迁移连接。"""
    runtime = test_url(value)
    migration_value = os.environ.get("INKRAG_TEST_MIGRATION_DSN")
    if migration_value:
        migration = test_url(migration_value)
        if (runtime.host, runtime.port or 5432, runtime.database) != (
            migration.host,
            migration.port or 5432,
            migration.database,
        ):
            raise TestConfigurationError("迁移与测试运行连接必须指向同一个独立数据库")
        migrate(migration)
    check_runtime(runtime)
    print("PG回归使用显式独立测试库与受限运行身份；DSN未输出", flush=True)
    return runtime


def prepare_local(root: Path) -> URL:
    """受控初始化inkrag_test；不清空既有库，不连接正式业务outbox。"""
    values = dict(
        line.split("=", 1)
        for line in (root / ".inkrag/compose.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    port = int(values["PG_PORT"])
    runtime = URL.create(
        "postgresql+psycopg",
        username="inkrag_app",
        password=values["PG_APP_PASSWORD"],
        host="127.0.0.1",
        port=port,
        database="inkrag_test",
    )
    migration = URL.create(
        "postgresql+psycopg",
        username="inkrag_migrator",
        password=values["PG_MIGRATION_PASSWORD"],
        host="127.0.0.1",
        port=port,
        database="inkrag_test",
    )
    admin = URL.create(
        "postgresql+psycopg",
        username="bootstrap",
        password=values["PG_BOOTSTRAP_PASSWORD"],
        host="127.0.0.1",
        port=port,
        database="postgres",
    )
    engine = create_engine(admin, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(20622634763432788)"))
            try:
                owner = connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='inkrag_test'"
                    )
                )
                if owner is None:
                    connection.execute(text("CREATE DATABASE inkrag_test OWNER inkrag_migrator"))
                elif owner != "inkrag_migrator":
                    raise TestConfigurationError("既有inkrag_test不属于受控迁移角色，拒绝自动修改")
                connection.execute(text("REVOKE ALL ON DATABASE inkrag_test FROM PUBLIC"))
                connection.execute(
                    text("GRANT CONNECT ON DATABASE inkrag_test TO inkrag_app,inkrag_migrator")
                )
                migrate(migration)
                check_runtime(runtime)
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(20622634763432788)"))
    finally:
        engine.dispose()
    print("PG回归使用独立inkrag_test；运行身份inkrag_app，迁移/admin连接未传入pytest", flush=True)
    return runtime


def main() -> int:
    """配置只在当前进程与必要子进程环境存在，业务服务永远不访问测试库。"""
    root = Path(__file__).resolve().parents[1]
    try:
        explicit = os.environ.get("INKRAG_TEST_DATABASE_DSN")
        url = prepare_explicit(explicit) if explicit else prepare_local(root)
    except TestConfigurationError as error:
        print("PG测试环境拒绝：" + str(error), file=sys.stderr)
        return 2
    except DomainError as error:
        print("PG测试环境未就绪：" + error.code, file=sys.stderr)
        return 2
    except (SQLAlchemyError, OSError, KeyError, ValueError):
        print("PG测试初始化失败，请核对本地数据库及受控配置；连接详情未输出", file=sys.stderr)
        return 2
    environment = {
        **os.environ,
        "INKRAG_TEST_DATABASE_DSN": url.render_as_string(hide_password=False),
    }
    for name in (
        "INKRAG_TEST_MIGRATION_DSN",
        "INKRAG_MIGRATION_URL",
        "PG_BOOTSTRAP_PASSWORD",
        "PG_MIGRATION_PASSWORD",
    ):
        environment.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *sys.argv[1:]], cwd=root, env=environment, check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
