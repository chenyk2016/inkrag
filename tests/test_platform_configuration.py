"""验证外部配置不会被本地开发凭据静默替换；检查只记录布尔结果。"""

import json
import subprocess
from pathlib import Path

import pytest
from inkrag_platform import bootstrap


@pytest.mark.parametrize("external", [True, False])
def test_s3_environment_selects_only_its_configured_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external: bool
) -> None:
    """外部S3缺省键显式为空供SDK默认链处理；本地MinIO才允许Compose默认凭据。"""
    (tmp_path / "state.json").write_text(json.dumps({"project": "inkrag-configuration-test"}))
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    if external:
        monkeypatch.setenv("INKRAG_S3_ENDPOINT", "https://s3.example")
    else:
        monkeypatch.delenv("INKRAG_S3_ENDPOINT", raising=False)
    checks: list[bool] = []

    def run(
        command: list[str], *, env: dict[str, str], check: bool, text: bool, stdout: int | None
    ) -> subprocess.CompletedProcess[str]:
        """只截取配置选择结果，不保存或输出完整宿主环境。"""
        checks.append(
            (env.get("AWS_ACCESS_KEY_ID") == "" and env.get("AWS_SECRET_ACCESS_KEY") == "")
            if external
            else ("AWS_ACCESS_KEY_ID" not in env and "AWS_SECRET_ACCESS_KEY" not in env)
        )
        return subprocess.CompletedProcess(command, returncode=0, stdout="")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    bootstrap.compose(tmp_path, "ps", capture=True)
    assert checks == [True]
