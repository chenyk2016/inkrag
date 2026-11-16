"""生产启动边界：明确配置、秘密隔离和零副作用的解析独立装配。"""

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from inkrag_platform.bootstrap import initialize_state, manage
from inkrag_platform.settings import Settings
from pydantic import SecretStr, TypeAdapter, ValidationError


def settings() -> Settings:
    """构造不建立连接的完整解析配置。"""
    return Settings(
        capabilities="parse",
        database_url=SecretStr("postgresql+psycopg://app:p@db/inkrag"),
        rabbit_url=SecretStr("amqp://user:p@rabbit/vhost"),
        s3_endpoint="http://s3:9000",
        oidc_issuer="http://oidc/realms/test",
    )


def test_production_rejects_unsafe_infrastructure() -> None:
    """生产不能误用SQLite、无TLS连接或关闭隔离，解析模式无需RAG依赖。"""
    base = settings()
    for values in [
        {"database_url": "sqlite:///state.db"},
        {"mode": "production"},
        {"capabilities": "combined"},
        {"opensearch_user": SecretStr("app")},
    ]:
        with pytest.raises(ValidationError):
            Settings.model_validate({**base.model_dump(), **values})
    secure = Settings.model_validate(
        {
            **base.model_dump(),
            "mode": "production",
            "database_url": SecretStr("postgresql+psycopg://app:p@db/inkrag?sslmode=verify-full"),
            "rabbit_url": SecretStr("amqps://user:p@rabbit/vhost"),
            "s3_endpoint": "https://s3.example",
            "oidc_issuer": "https://oidc.example/realms/test",
        }
    )
    with pytest.raises(ValidationError):
        Settings.model_validate({**secure.model_dump(), "sandbox_required": False})
    with pytest.raises(ValidationError):
        Settings.model_validate({**secure.model_dump(), "oidc_jwks_url": "http://insecure/jwks"})
    # Compose中存在未启用的开发RAG默认URL，不应迫使独立解析产品配置这些服务。
    isolated = Settings.model_validate(
        {
            **secure.model_dump(),
            "opensearch_url": "http://unused",
            "embedding_url": "http://unused",
            "batch_embedding_url": "http://unused",
            "search_replicas": 0,
        }
    )
    assert isolated.capabilities == "parse"
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                **secure.model_dump(),
                "migration_url": SecretStr("postgresql+psycopg://owner:p@db/inkrag"),
            }
        )
    assert "user:p" not in repr(secure)


def test_rag_profile_is_validated_before_runtime_opens() -> None:
    """共享的版本定义用于启动预检，未知版本在建立连接前被拒绝。"""
    values = {
        **settings().model_dump(),
        "capabilities": "rag",
        "opensearch_url": "http://search",
        "embedding_url": "http://embedding",
        "batch_embedding_url": "http://batch",
        "model_revision": "test",
    }
    accepted = Settings.model_validate({**values, "lexical_profile": "jieba-v1"})
    assert accepted.search_lexical_profile() == "jieba-v1"
    with pytest.raises(ValidationError, match="jieba-v1"):
        Settings.model_validate({**values, "lexical_profile": "jieba-unknown"})


def test_build_configuration_cannot_exceed_reader_limits() -> None:
    """达到安全上限仍能启动，超过查询可读范围的配置启动前拒绝。"""
    base = settings()
    assert base.max_build_documents == 5000
    assert base.max_manifest_bytes == 16 * 1024 * 1024
    for key, maximum in (
        ("max_build_documents", base.max_build_documents),
        ("max_manifest_bytes", base.max_manifest_bytes),
    ):
        Settings.model_validate({**base.model_dump(), key: maximum})
        with pytest.raises(ValidationError, match=key):
            Settings.model_validate({**base.model_dump(), key: maximum + 1})


def test_initialization_reuses_private_credentials(tmp_path: Path) -> None:
    """重启不得重生成密码、污染进程环境或改变已分配端口。"""
    before = dict(os.environ)
    first = initialize_state(tmp_path)
    second = initialize_state(tmp_path)
    assert first == second
    assert first["PG_APP_PASSWORD"] != first["PG_MIGRATION_PASSWORD"]
    assert len({first[key] for key in first if key.endswith("_PORT")}) == 6
    assert stat.S_IMODE((tmp_path / "compose.env").stat().st_mode) == 0o600
    assert dict(os.environ) == before


def test_bootstrap_isolates_sibling_states_and_recovers_old_partial_write(tmp_path: Path) -> None:
    """相邻测试栈不共享卷；旧版中断后可恢复原作用域与原凭据。"""
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config = initialize_state(first)
    initialize_state(second)
    adapter = TypeAdapter(dict[str, str | dict[str, str]])
    first_state = adapter.validate_json((first / "state.json").read_bytes())
    second_state = adapter.validate_json((second / "state.json").read_bytes())
    assert first_state["project"] != second_state["project"]
    assert initialize_state(first) == config
    (first / "state.json").unlink()
    assert initialize_state(first) == config
    recovered = adapter.validate_json((first / "state.json").read_bytes())
    legacy_id = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()[:10]
    assert recovered["project"] == "inkrag-" + legacy_id
    assert recovered["ports"] == first_state["ports"]


def test_manage_includes_app_profile_without_removing_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """查看和停机都包含 API、Worker、Relay 与模型；停机不清空持久卷。"""
    monkeypatch.setenv("INKRAG_STATE_DIR", str(tmp_path))
    initialize_state(tmp_path)
    calls: list[tuple[tuple[str, ...], bool]] = []

    def record(directory: Path, *args: str, capture: bool = False) -> str:
        """记录 Compose CLI 参数，真实容器由发行验收验证。"""
        assert directory == tmp_path.resolve()
        calls.append((args, capture))
        return ""

    monkeypatch.setattr("inkrag_platform.bootstrap.compose", record)
    manage("status")
    manage("down")
    assert calls == [
        (("--profile", "app", "ps", "--format", "table"), True),
        (("--profile", "app", "down"), False),
    ]


def test_parse_host_imports_without_rag_or_io(tmp_path: Path) -> None:
    """新的独立Python进程导入宿主不会加载RAG/模型或创建任何状态文件。"""
    code = """
import sys
import inkrag_platform.api
import inkrag_platform.worker
from inkrag_platform.settings import Settings
from pydantic import SecretStr

Settings(
    capabilities="parse",
    database_url=SecretStr("postgresql+psycopg://db/inkrag"),
    rabbit_url=SecretStr("amqp://rabbit"),
    s3_endpoint="http://storage",
    oidc_issuer="http://oidc",
)
assert "inkrag" not in sys.modules
assert not any(n.startswith(('inkrag.', 'llama_index', 'opensearchpy')) for n in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert list(tmp_path.iterdir()) == []
