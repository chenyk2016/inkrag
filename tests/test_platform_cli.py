"""验证迁移入口只借入数据库迁移凭据，生产拒绝不安全连接。"""

import sys

import pytest
from inkrag_platform import cli, database, settings


@pytest.mark.parametrize("mode", ["development", "production"])
def test_migrate_uses_only_its_own_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    """迁移直接使用专用连接，业务配置即使无效也不读取。"""
    events: list[str] = []
    dsn = "postgresql+psycopg://migration:fixture@db.test/inkrag?sslmode=verify-full"

    class MigrationDatabase:
        """只替代数据库网络边界，记录迁移和关闭操作。"""

        def __init__(self, url: str) -> None:
            """迁移只能取得专用连接。"""
            assert url == dsn
            events.append("open")

        def migrate(self) -> None:
            """记录正常迁移入口被执行。"""
            events.append("migrate")

        def close(self) -> None:
            """记录借入的数据库连接已释放。"""
            events.append("close")

    def reject_business_configuration() -> None:
        """业务配置在迁移进程中禁止读取。"""
        pytest.fail("迁移读取了业务配置")

    monkeypatch.setattr(settings, "load_settings", reject_business_configuration)
    monkeypatch.setattr(database, "Database", MigrationDatabase)
    monkeypatch.setattr(sys, "argv", ["inkrag", "migrate"])
    monkeypatch.setenv("INKRAG_MIGRATION_URL", dsn)
    monkeypatch.setenv("INKRAG_MODE", mode)
    monkeypatch.setenv("INKRAG_DATABASE_URL", "unused-business-setting")
    assert cli.main() == 0
    assert events == ["open", "migrate", "close"]
    assert dsn not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("mode", "dsn", "error"),
    [
        ("invalid", "", "INKRAG_MODE"),
        ("development", "", "INKRAG_MIGRATION_URL"),
        ("development", "sqlite:///db", "INKRAG_MIGRATION_URL"),
        ("production", "postgresql+psycopg://db.test/inkrag", "sslmode=verify-full"),
        (
            "production",
            "postgresql+psycopg://db.test/inkrag?sslmode=verify-full&sslmode=",
            "sslmode=verify-full",
        ),
    ],
)
def test_migrate_rejects_unsafe_configuration_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    dsn: str,
    error: str,
) -> None:
    """配置不合法时不建立连接，也不会输出连接内容。"""

    def reject_connection(_dsn: str) -> None:
        """失败配置到达数据库构造器即为越界。"""
        pytest.fail("无效迁移配置尝试连接数据库")

    monkeypatch.setattr(database, "Database", reject_connection)
    monkeypatch.setattr(sys, "argv", ["inkrag", "migrate"])
    monkeypatch.setenv("INKRAG_MODE", mode)
    monkeypatch.setenv("INKRAG_MIGRATION_URL", dsn)
    assert cli.main() == 1
    message = capsys.readouterr().err
    assert error in message
    if dsn:
        assert dsn not in message
