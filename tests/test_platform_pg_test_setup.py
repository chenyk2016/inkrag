"""测试入口必须拒绝正式/系统库，错误信息不得泄露连接凭据。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("database", ["inkrag", "postgres", "keycloak", "template0", "template1"])
def test_pg_helper_rejects_live_and_system_databases_without_printing_secrets(
    database: str,
) -> None:
    """拒绝发生在网络访问前，即使没有Docker也能检查安全边界。"""
    secret = "DO_NOT_PRINT_PG_TEST_PASSWORD"
    environment = {
        **os.environ,
        "INKRAG_TEST_DATABASE_DSN": f"postgresql+psycopg://inkrag_app:{secret}@127.0.0.1:1/{database}",
    }
    process = subprocess.run(
        [sys.executable, "scripts/check-platform-pg.py", "--collect-only"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    assert process.returncode == 2 and "拒绝" in process.stdout
    assert secret not in process.stdout


def test_pg_helper_rejects_migration_target_mismatch_without_connection() -> None:
    """运行与迁移身份只能指向同一独立库，避免用户配置错误把迁移打向另一个库。"""
    secret = "DO_NOT_PRINT_PG_MIGRATION_PASSWORD"
    environment = {
        **os.environ,
        "INKRAG_TEST_DATABASE_DSN": f"postgresql+psycopg://inkrag_app:{secret}@127.0.0.1:1/inkrag_test",
        "INKRAG_TEST_MIGRATION_DSN": f"postgresql+psycopg://inkrag_migrator:{secret}@127.0.0.1:1/other_test",
    }
    process = subprocess.run(
        [sys.executable, "scripts/check-platform-pg.py", "--collect-only"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    assert process.returncode == 2 and "同一个独立数据库" in process.stdout
    assert secret not in process.stdout
