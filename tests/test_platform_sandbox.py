"""隔离入口的可复测反例；安全探测只尝试打开路径，不读取任何真实秘密。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from inkparse import ParseError
from inkrag_platform.sandbox import run_parse
from inkrag_platform.settings import Settings
from inkstore.local import LocalAssetStore
from pydantic import SecretStr


def test_child_returns_safe_diagnostic_and_never_retries_bad_json(tmp_path: Path) -> None:
    """真实隔离子进程携带安全位置和恢复建议，正文与本机路径不出现在错误中。"""
    settings = Settings(
        capabilities="parse",
        sandbox_required=sys.platform == "linux",
        database_url=SecretStr("postgresql+psycopg://test:test@unused/test"),
        rabbit_url=SecretStr("amqp://test:test@unused/test"),
        s3_endpoint="http://unused",
        oidc_issuer="http://unused",
    )
    storage = LocalAssetStore.open(tmp_path / "output")
    with pytest.raises(ParseError) as caught:
        run_parse(b'{"private-secret":1,\n}', "bad.json", storage, settings)
    diagnostic = caught.value.diagnostic()
    assert diagnostic.code == "invalid_json"
    assert diagnostic.location == "第2行，第1列"
    assert diagnostic.suggestion and not diagnostic.retryable
    assert "private-secret" not in diagnostic.model_dump_json()
    assert str(tmp_path) not in diagnostic.model_dump_json()


def test_development_parse_uses_bounded_child_on_macos(tmp_path: Path) -> None:
    """macOS显式开发允许无Linux沙箱解析，不因不支持RLIMIT_AS而全量失败。"""
    if sys.platform != "darwin":
        pytest.skip("此反例针对macOS开发资源限制")
    settings = Settings(
        capabilities="parse",
        sandbox_required=False,
        database_url=SecretStr("postgresql+psycopg://test:test@db/test"),
        rabbit_url=SecretStr("amqp://test:test@rabbit/test"),
        s3_endpoint="http://storage",
        oidc_issuer="http://identity",
    )
    storage = LocalAssetStore.open(tmp_path / "output")
    result = run_parse("隔离入口正文".encode(), "manual.txt", storage, settings)
    assert result.document.texts[0].text == "隔离入口正文"
    assert result.source.asset_id is not None
    assert storage.read(result.source.asset_id) == "隔离入口正文".encode()


@pytest.mark.integration
def test_linux_filesystem_network_process_and_environment_isolation(tmp_path: Path) -> None:
    """真实Linux新进程验证禁止越界打开、符号链接、网络、fork和父进程信号。"""
    if sys.platform != "linux" or os.environ.get("INKRAG_SANDBOX_INTEGRATION") != "1":
        pytest.skip("需显式启用Linux Landlock/seccomp集成环境")
    current = tmp_path / "current"
    current.mkdir()
    other = tmp_path / "other"
    other.write_text("nonsecret-probe")
    (current / "escape").symlink_to(other)
    code = """
import json, os, socket, sys
from pathlib import Path
from inkrag_platform.executor import restrict
root = Path(sys.argv[1])
restrict(512, 10, True, root)
checks = {}
for name, path in [("init_environment", "/proc/1/environ"),
                   ("other_scratch", sys.argv[2]), ("symlink_escape", root / "escape")]:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except PermissionError:
        checks[name] = True
    else:
        os.close(descriptor)
        checks[name] = False
try:
    connection = socket.socket()
except PermissionError:
    checks["network"] = True
else:
    connection.close()
    checks["network"] = False
try:
    child = os.fork()
except PermissionError:
    checks["fork"] = True
else:
    if child == 0:
        os._exit(0)
    os.waitpid(child, 0)
    checks["fork"] = False
try:
    os.kill(os.getppid(), 0)
except PermissionError:
    checks["parent_signal"] = True
else:
    checks["parent_signal"] = False
(root / "ok").write_text("ok")
checks["scratch_read_write"] = (root / "ok").read_text() == "ok"
checks["secret_environment_absent"] = "INKRAG_SANDBOX_PROBE" not in os.environ
print(json.dumps(checks))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code, str(current), str(other)],
        env={
            "PATH": os.defpath,
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    checks: object = json.loads(completed.stdout)
    assert checks == {
        "init_environment": True,
        "other_scratch": True,
        "symlink_escape": True,
        "network": True,
        "fork": True,
        "parent_signal": True,
        "scratch_read_write": True,
        "secret_environment_absent": True,
    }


def test_untrusted_error_symlink_cannot_be_read_by_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅替换子进程边界模拟逃逸输出，父进程不得沿符号链接读取沙箱外文件。"""
    import inkrag_platform.sandbox as boundary

    outside = tmp_path / "nonsecret-error-probe"
    outside.write_text("outside_private_marker")

    def malicious_output(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[bytes]:
        """创建恶意子进程产物，不读取用户目录或任何真实秘密。"""
        (Path(command[4]) / "error").symlink_to(outside)
        return subprocess.CompletedProcess[bytes](command, 1)

    monkeypatch.setattr(boundary.subprocess, "run", malicious_output)
    settings = Settings(
        capabilities="parse",
        sandbox_required=False,
        database_url=SecretStr("postgresql+psycopg://test:test@unused/test"),
        rabbit_url=SecretStr("amqp://test:test@unused/test"),
        s3_endpoint="http://unused",
        oidc_issuer="http://unused",
    )
    storage = LocalAssetStore.open(tmp_path / "output")
    with pytest.raises(ParseError) as caught:
        run_parse(b"Valid plain source", "source.txt", storage, settings)
    assert caught.value.code == "sandbox_failed"
    assert "outside_private_marker" not in str(caught.value)
    assert outside.read_text() == "outside_private_marker"
