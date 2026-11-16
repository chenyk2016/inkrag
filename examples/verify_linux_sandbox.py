"""用最终镜像复测Linux隔离；仅传非秘密标记，证据只保存布尔值。"""

import argparse
import json
import subprocess
from pathlib import Path

from pydantic import TypeAdapter

REQUIRED_CHECKS = {
    "pid1_environment_denied",
    "parent_environment_denied",
    "self_proc_denied",
    "other_scratch_denied",
    "symlink_escape_denied",
    "network_denied",
    "io_uring_setup_denied",
    "fork_denied",
    "parent_signal_denied",
    "scratch_read_write",
    "child_environment_clean",
    "restricted_child_completed",
    "parent_has_only_test_marker",
    "formal_text_parse",
    "text_original_saved",
    "formal_docx_parse",
    "docx_native_table",
    "docx_original_saved",
    "parse_host_without_rag",
    "container_probe_completed",
}

CONTAINER_PROBE = r'''
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from docx import Document
from inkstore.local import LocalAssetStore
from inkrag_platform.sandbox import protect_process, run_parse
from inkrag_platform.settings import Settings
from pydantic import SecretStr

checks = {}
try:
    protect_process()
    with TemporaryDirectory(prefix="inkrag-verify-") as directory:
        root = Path(directory)
        current = root / "current"
        current.mkdir()
        other = root / "other"
        other.write_text("nonsecret-probe")
        (current / "escape").symlink_to(other)
        child_code = r"""
import ctypes
import errno
import json
import os
import socket
import sys
from pathlib import Path
from inkrag_platform.executor import restrict
root = Path(sys.argv[1])
restrict(512, 10, True, root)
checks = {}
# Linux UAPI在这两个已验证架构上使用425；未知架构明确失败，不能将ENOSYS当作隔离通过。
io_uring_setup = {"aarch64": 425, "x86_64": 425}.get(os.uname().machine)
if io_uring_setup is None:
    raise RuntimeError("隔离探测不支持此架构")
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
ctypes.set_errno(0)
result = libc.syscall(ctypes.c_long(io_uring_setup), ctypes.c_uint(0), ctypes.c_void_p())
checks["io_uring_setup_denied"] = result == -1 and ctypes.get_errno() == errno.EPERM
for name, path in [("pid1_environment_denied", "/proc/1/environ"),
    ("parent_environment_denied", "/proc/" + str(os.getppid()) + "/environ"),
    ("self_proc_denied", "/proc/self/environ"), ("other_scratch_denied", sys.argv[2]),
    ("symlink_escape_denied", root / "escape")]:
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
    checks["network_denied"] = True
else:
    connection.close()
    checks["network_denied"] = False
try:
    child = os.fork()
except PermissionError:
    checks["fork_denied"] = True
else:
    if child == 0:
        os._exit(0)
    os.waitpid(child, 0)
    checks["fork_denied"] = False
try:
    os.kill(os.getppid(), 0)
except PermissionError:
    checks["parent_signal_denied"] = True
else:
    checks["parent_signal_denied"] = False
(root / "ok").write_text("ok")
checks["scratch_read_write"] = (root / "ok").read_text() == "ok"
checks["child_environment_clean"] = "TEST_MARKER" not in os.environ and not any(
    key.startswith(("AWS_", "INKRAG_DATABASE", "INKRAG_RABBIT", "INKRAG_OIDC")) for key in os.environ)
print(json.dumps(checks))
"""
        environment = {"PATH": os.defpath, "HOME": str(current), "TMPDIR": str(current),
            "LANG": "C.UTF-8", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
            "PYTHONDONTWRITEBYTECODE": "1"}
        child = subprocess.run([sys.executable, "-I", "-c", child_code, str(current), str(other)],
            env=environment, capture_output=True, text=True, check=False, timeout=30)
        checks["restricted_child_completed"] = child.returncode == 0
        if child.returncode == 0:
            parsed = json.loads(child.stdout)
            if isinstance(parsed, dict) and all(type(value) is bool for value in parsed.values()):
                checks.update(parsed)
        checks["parent_has_only_test_marker"] = os.environ.get("TEST_MARKER") == "nonsecret-marker" and not any(
            key.startswith(("AWS_", "INKRAG_DATABASE", "INKRAG_RABBIT", "INKRAG_OIDC")) for key in os.environ)
        storage = LocalAssetStore.open(root / "output")
        settings = Settings(capabilities="parse", sandbox_required=True,
            database_url=SecretStr("postgresql+psycopg://nonsecret:nonsecret@db/test"),
            rabbit_url=SecretStr("amqp://nonsecret:nonsecret@rabbit/test"),
            s3_endpoint="http://storage", oidc_issuer="http://identity")
        text = "正式隔离正文"
        result = run_parse(text.encode(), "manual.txt", storage, settings)
        checks["formal_text_parse"] = result.document.texts[0].text == text
        checks["text_original_saved"] = storage.read(result.source.asset_id) == text.encode()
        document = Document()
        document.add_paragraph("正式Office隔离正文")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "A100", "12个月"
        output = io.BytesIO()
        document.save(output)
        result = run_parse(output.getvalue(), "manual.docx", storage, settings)
        checks["formal_docx_parse"] = result.document.texts[0].text == "正式Office隔离正文"
        checks["docx_native_table"] = len(result.document.tables) == 1
        checks["docx_original_saved"] = storage.read(result.source.asset_id) == output.getvalue()
        checks["parse_host_without_rag"] = not any(
            name.startswith(("llama_index", "inkrag.", "opensearchpy")) for name in sys.modules)
    checks["container_probe_completed"] = True
except Exception:
    checks["container_probe_completed"] = False
print(json.dumps(checks, sort_keys=True))
'''


def verify(image: str) -> dict[str, bool]:
    """容器不挂宿主目录、不继承应用凭据，只在只读镜像与受限tmpfs内执行探测。"""
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--read-only",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "128",
        "--memory",
        "768m",
        "--tmpfs",
        "/tmp:rw,size=128m,mode=1777",
        "--user",
        "10001:10001",
        "--env",
        "TEST_MARKER=nonsecret-marker",
        "-i",
        "--entrypoint",
        "python",
        image,
        "-I",
        "-",
    ]
    completed = subprocess.run(
        command, input=CONTAINER_PROBE, text=True, capture_output=True, timeout=180, check=False
    )
    if completed.returncode != 0:
        return {"container_probe_completed": False}
    try:
        result = TypeAdapter(dict[str, bool]).validate_json(completed.stdout, strict=True)
        result["complete_checklist"] = not (REQUIRED_CHECKS - result.keys())
        return result
    except ValueError:
        return {"container_probe_completed": False}


def main() -> int:
    """显式选择镜像和证据路径，原子保存布尔结果；任一失败返回非零。"""
    parser = argparse.ArgumentParser(description="验证最终Linux镜像的受限解析，不访问真实凭据")
    parser.add_argument("--image", default="inkrag-platform:0.1.0a1")
    parser.add_argument("--output", type=Path, default=Path("docs/evidence/platform-sandbox.json"))
    arguments = parser.parse_args()
    image, output = str(arguments.image), Path(arguments.output)
    result = verify(image)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".new")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
    print(json.dumps(result, sort_keys=True))
    return 0 if result and all(result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
