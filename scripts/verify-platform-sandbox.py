"""用不可变Linux镜像验收真实隔离、含图Office解析及离线交付，不访问用户资料。"""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

MODULE_PATHS = {
    "inkparse.parser": "packages/inkparse/src/inkparse/parser.py",
    "inkparse.adapters.docling.reader": "packages/inkparse/src/inkparse/adapters/docling/reader.py",
    "inkparse.adapters.builtin.reader": "packages/inkparse/src/inkparse/adapters/builtin/reader.py",
    "inkparse.models": "packages/inkparse/src/inkparse/models.py",
    "inkparse.adapters.builtin.html": "packages/inkparse/src/inkparse/adapters/builtin/html.py",
    "inkparse.adapters.builtin.office": "packages/inkparse/src/inkparse/adapters/builtin/office.py",
    "inkparse.adapters.builtin.structured": "packages/inkparse/src/inkparse/adapters/builtin/structured.py",
    "inkparse.editing": "packages/inkparse/src/inkparse/editing.py",
    "inkparse.bundle": "packages/inkparse/src/inkparse/bundle.py",
    "inkparse.io": "packages/inkparse/src/inkparse/io.py",
    "inkrag_platform.executor": "packages/inkrag-platform/src/inkrag_platform/executor.py",
    "inkrag_platform.sandbox": "packages/inkrag-platform/src/inkrag_platform/sandbox.py",
}

DELIVERY_CHECKS = {
    "linux_runtime",
    "outer_container_socket_available",
    "outer_container_fork_available",
    "bad_json_rejected",
    "safe_json_location",
    "bad_json_not_retryable",
    "safe_json_no_content_leak",
    "docx_strict_parse",
    "docx_native_table",
    "docx_image_bytes_saved",
    "docx_original_bytes_saved",
    "docx_source_digest",
    "docx_record_roundtrip",
    "zip_relative_image",
    "zip_manifest_checksums",
    "zip_original_bytes",
    "zip_original_digest",
    "zip_json_roundtrip",
    "zip_safe_paths",
    "zip_original_optional",
    "zip_corrupt_crc_rejected",
    "zip_corrupt_crc_safe_diagnostic",
    "zip_corrupt_crc_preserves_previous",
    "zip_corrupt_crc_temporary_cleaned",
    "zip_corrupt_crc_bytes_preserved",
    "zip_failure_atomic",
    "zip_failure_temporary_cleaned",
    "parse_import_without_rag",
    "container_credentials_absent",
    "delivery_probe_completed",
}

DELIVERY_PROBE = r"""
import base64
import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from docx import Document
from PIL import Image
from inkparse import ParseError, ParseResult, load_stored_result, save_bundle
from inkstore import StorageError
from inkstore.local import LocalAssetStore
from inkrag_platform.sandbox import run_parse
from inkrag_platform.settings import Settings
from py_landlock import Landlock
from pydantic import SecretStr

report = {"checks": {}, "stage": "runtime", "module_sha256": {}}
checks = report["checks"]
try:
    checks["linux_runtime"] = sys.platform == "linux"
    report["python"] = sys.version.split()[0]
    report["architecture"] = os.uname().machine
    report["landlock_abi"] = Landlock(strict=True).abi_version
    for name in MODULE_NAMES:
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin is not None
        report["module_sha256"][name] = hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest()
    connection = socket.socket()
    connection.close()
    checks["outer_container_socket_available"] = True
    child = os.fork()
    if child == 0:
        os._exit(0)
    _, status = os.waitpid(child, 0)
    checks["outer_container_fork_available"] = status == 0
    checks["container_credentials_absent"] = not any(
        key.startswith(("AWS_", "MINIO_", "INKRAG_DATABASE", "INKRAG_RABBIT", "INKRAG_OIDC"))
        for key in os.environ
    )
    with TemporaryDirectory(prefix="inkrag-delivery-verify-") as directory:
        root = Path(directory)
        store_root = root / "assets"
        storage = LocalAssetStore.open(store_root)
        settings = Settings(capabilities="parse", sandbox_required=True,
            database_url=SecretStr("postgresql+psycopg://nonsecret:nonsecret@unused/test"),
            rabbit_url=SecretStr("amqp://nonsecret:nonsecret@unused/test"),
            s3_endpoint="http://unused", oidc_issuer="http://unused")
        report["parse_memory_mb"] = settings.parse_memory_mb
        report["parse_seconds"] = settings.parse_seconds
        report["stage"] = "strict_bad_json"
        try:
            run_parse(b'{"private-nonsecret-marker":1,\n}', "bad.json", storage, settings)
        except ParseError as failure:
            detail = failure.diagnostic()
            report["diagnostic"] = detail.model_dump(mode="json")
            checks["bad_json_rejected"] = detail.code == "invalid_json"
            checks["safe_json_location"] = detail.location == "第2行，第1列"
            checks["bad_json_not_retryable"] = not detail.retryable
            checks["safe_json_no_content_leak"] = (
                "private-nonsecret-marker" not in detail.model_dump_json()
                and directory not in detail.model_dump_json()
                and bool(detail.suggestion)
            )
        else:
            checks["bad_json_rejected"] = False
        report["stage"] = "strict_docx_with_image"
        image_stream = io.BytesIO()
        Image.new("RGB", (4, 3), "blue").save(image_stream, format="PNG")
        image_bytes = image_stream.getvalue()
        document = Document()
        document.add_heading("隔离验收含图文档", level=1)
        document.add_paragraph("真实原件、嵌入图片和表格在同一解析结果关联。")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "A100", "29"
        document.add_picture(io.BytesIO(image_bytes))
        source = io.BytesIO()
        document.save(source)
        source_bytes = source.getvalue()
        result = run_parse(source_bytes, "with-image.docx", storage, settings)
        checks["docx_strict_parse"] = result.source.format == "docx" and len(result.document.pictures) == 1
        checks["docx_native_table"] = (len(result.document.tables) == 1
            and result.document.tables[0].data.table_cells[1].text == "29")
        assert result.source.asset_id is not None
        checks["docx_original_bytes_saved"] = storage.read(result.source.asset_id) == source_bytes
        checks["docx_source_digest"] = result.source.sha256 == hashlib.sha256(source_bytes).hexdigest()
        checks["docx_image_bytes_saved"] = (len(result.assets) == 1
            and result.assets[0].element_ref == "#/pictures/0"
            and storage.read(result.assets[0].asset_id) == image_bytes)
        checks["docx_record_roundtrip"] = load_stored_result(storage, result.result_id).result_id == result.result_id
        report["source_sha256"] = result.source.sha256
        report["image_sha256"] = hashlib.sha256(image_bytes).hexdigest()
        report["stage"] = "portable_zip"
        packet = root / "delivery.zip"
        save_bundle(result, storage, packet, include_original=True)
        with zipfile.ZipFile(packet) as archive:
            names = archive.namelist()
            image_path = "assets/" + result.assets[0].asset_id + ".png"
            markdown = archive.read("document.md").decode()
            manifest = json.loads(archive.read("manifest.json"))
            checks["zip_relative_image"] = image_path in markdown and archive.read(image_path) == image_bytes
            checks["zip_manifest_checksums"] = all(
                hashlib.sha256(archive.read(name)).hexdigest() == entry["sha256"]
                and len(archive.read(name)) == entry["size_bytes"]
                for name, entry in manifest["files"].items()
            ) and bool(manifest["resources"])
            checks["zip_original_bytes"] = archive.read("original/source.docx") == source_bytes
            checks["zip_original_digest"] = (manifest["original"]["sha256"] == result.source.sha256
                and hashlib.sha256(archive.read("original/source.docx")).hexdigest() == result.source.sha256)
            checks["zip_json_roundtrip"] = ParseResult.model_validate_json(archive.read("document.json")).result_id == result.result_id
            checks["zip_safe_paths"] = all(not name.startswith("/") and ".." not in name.split("/") for name in names)
        optional = root / "without-original.zip"
        save_bundle(result, storage, optional)
        with zipfile.ZipFile(optional) as archive:
            checks["zip_original_optional"] = not any(name.startswith("original/") for name in archive.namelist())
        report["stage"] = "zip_corrupt_crc_preserves_previous"
        damaged = bytearray(image_bytes)
        offset = damaged.index(b"IDAT")
        length = int.from_bytes(damaged[offset - 4:offset], "big")
        damaged[offset + 4 + length] ^= 1
        damaged_bytes = bytes(damaged)
        encoded = base64.b64encode(damaged_bytes).decode()
        damaged_source = ('<p>private-corrupt-image-marker</p>'
            + '<img src="data:image/png;base64,' + encoded + '">').encode()
        damaged_result = run_parse(damaged_source, "damaged-image.html", storage, settings)
        previous = packet.read_bytes()
        names_before = sorted(path.name for path in root.iterdir())
        try:
            save_bundle(damaged_result, storage, packet, include_original=True)
        except ParseError as failure:
            detail = failure.diagnostic()
            report["crc_diagnostic"] = detail.model_dump(mode="json")
            checks["zip_corrupt_crc_rejected"] = detail.code == "invalid_asset"
            checks["zip_corrupt_crc_safe_diagnostic"] = (detail.location == "#/pictures/0"
                and not detail.retryable and bool(detail.suggestion) and "修复" in detail.message
                and "private-corrupt-image-marker" not in detail.model_dump_json()
                and directory not in detail.model_dump_json()
                and "IDAT" not in detail.model_dump_json() and "checksum" not in detail.model_dump_json())
            checks["zip_corrupt_crc_preserves_previous"] = packet.read_bytes() == previous
            checks["zip_corrupt_crc_temporary_cleaned"] = sorted(path.name for path in root.iterdir()) == names_before
        else:
            checks["zip_corrupt_crc_rejected"] = False
        assert damaged_result.source.asset_id is not None
        checks["zip_corrupt_crc_bytes_preserved"] = (storage.read(damaged_result.assets[0].asset_id) == damaged_bytes
            and storage.read(damaged_result.source.asset_id) == damaged_source)
        report["stage"] = "zip_failure_preserves_previous"
        before = packet.read_bytes()
        files_before = sorted(path.name for path in root.iterdir())
        (store_root / "objects" / result.assets[0].asset_id).unlink()
        try:
            save_bundle(result, storage, packet)
        except StorageError:
            checks["zip_failure_atomic"] = packet.read_bytes() == before
            checks["zip_failure_temporary_cleaned"] = sorted(path.name for path in root.iterdir()) == files_before
        else:
            checks["zip_failure_atomic"] = False
    checks["parse_import_without_rag"] = not any(
        name.startswith(("llama_index", "inkrag.", "opensearchpy")) for name in sys.modules)
    checks["delivery_probe_completed"] = True
    report["stage"] = "completed"
except Exception as failure:
    checks["delivery_probe_completed"] = False
    report["failure_type"] = type(failure).__name__
    if isinstance(failure, ParseError):
        report["failure_code"] = failure.code
print(json.dumps(report, sort_keys=True))
"""


def existing_isolation_probe(path: Path) -> tuple[str, set[str]]:
    """只提取既有验收中的静态字符串与必需检查项，不执行宿主上的容器探测。"""
    program = ""
    required: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "CONTAINER_PROBE" and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, str):
                program = value
        if target.id == "REQUIRED_CHECKS" and isinstance(node.value, ast.Set):
            for element in node.value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    required.add(element.value)
    if not program or not required:
        raise ValueError("既有Linux隔离探测入口不完整")
    return program, required


def verify(image: str, root: Path) -> tuple[dict[str, JsonValue], bool]:
    """镜像先解析到内容ID再运行；无用户挂载或凭据，记录源码摘要以限定证据范围。"""
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    image_id = inspected.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("镜像内容ID无效")
    isolation, required = existing_isolation_probe(root / "examples/verify_linux_sandbox.py")
    program = isolation + "\nMODULE_NAMES = " + repr(sorted(MODULE_PATHS)) + "\n" + DELIVERY_PROBE
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--user",
        "10001:10001",
        "--env",
        "TEST_MARKER=nonsecret-marker",
        "-i",
        "--entrypoint",
        "python",
        image_id,
        "-I",
        "-",
    ]
    completed = subprocess.run(
        command, input=program, text=True, capture_output=True, timeout=180, check=False
    )
    result: dict[str, JsonValue] = {
        "schema_version": "inkrag.linux-acceptance.v1",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "image": image,
        "image_id": image_id,
        "container_exit_code": completed.returncode,
        "constraints": {
            "network": "none",
            "read_only": True,
            "tmpfs_mb": 256,
            "cap_drop": "ALL",
            "no_new_privileges": True,
            "memory_mb": 1024,
            "cpus": 1,
            "pids_limit": 128,
            "user": "10001:10001",
            "host_mounts": [],
        },
        "credentials": "Only a nonsecret test marker; no application credentials passed.",
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "scope": "Real Linux Landlock/seccomp child; temporary local asset store, no PG/S3/OIDC/HTTP/browser/HA evidence.",
    }
    reports = [
        TypeAdapter(dict[str, JsonValue]).validate_json(line)
        for line in completed.stdout.splitlines()
        if line.strip()
    ]
    passed = completed.returncode == 0 and len(reports) == 2
    if len(reports) == 2:
        base, delivery = reports
        base_passed = required.issubset(base) and all(base.get(name) is True for name in required)
        checks = delivery.get("checks")
        delivery_passed = (
            isinstance(checks, dict)
            and DELIVERY_CHECKS.issubset(checks)
            and all(checks.get(name) is True for name in DELIVERY_CHECKS)
        )
        expected: dict[str, JsonValue] = {
            name: hashlib.sha256((root / path).read_bytes()).hexdigest()
            for name, path in MODULE_PATHS.items()
        }
        identical = delivery.get("module_sha256") == expected
        result.update(
            {
                "isolation": base,
                "delivery": delivery,
                "host_module_sha256": expected,
                "isolation_checklist_complete": base_passed,
                "delivery_checklist_complete": delivery_passed,
                "source_matches_host": identical,
            }
        )
        passed = passed and base_passed and delivery_passed and identical
    result["passed"] = passed
    return result, passed


def main() -> int:
    """只写指定正式证据路径；任一探测缺失或失败返回非零，不修改运行源码。"""
    parser = argparse.ArgumentParser(description="最终Linux镜像真实解析与隔离验收")
    parser.add_argument("--image", default="inkrag-platform:0.1.0a1")
    parser.add_argument(
        "--output", type=Path, default=Path("docs/evidence/frontend-migration-linux.json")
    )
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result, passed = verify(str(arguments.image), root)
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".new")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
    print(json.dumps({"passed": passed, "image_id": result["image_id"], "evidence": str(output)}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
