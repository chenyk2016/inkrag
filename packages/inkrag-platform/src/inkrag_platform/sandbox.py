"""解析在无凭据、受限子进程执行；宿主只校验产物并保存真实对象存储。"""

import ctypes
import hashlib
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from inkparse import ParseDiagnostic, ParseError, ParseOptions, ParseResult, save_stored_result
from inkstore import AssetStorage
from pydantic import ValidationError

from .settings import Settings


def protect_process() -> None:
    """禁止同UID解析子进程读取宿主/proc秘密或跟踪父进程。"""
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            raise RuntimeError("无法启用进程凭据隔离")


def run_parse(
    raw: bytes,
    name: str,
    storage: AssetStorage,
    settings: Settings,
    options: ParseOptions | None = None,
) -> ParseResult:
    """冻结输入，在有界子进程解析，逐项核对摘要后提交不变结果。

    Linux生产强制seccomp；其它系统只能显式关闭隔离用于单元开发。
    失败只留下可回收的未引用对象，PG业务提交始终由worker的租约条件控制。
    """
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise ParseError("invalid_source", "输入为空或超过20MiB")
    if settings.sandbox_required and sys.platform != "linux":
        raise ParseError("sandbox_unavailable", "受限解析需要Linux容器")
    selected = options or ParseOptions()
    memory_mb = (
        settings.docling_memory_mb if selected.engine == "docling" else settings.parse_memory_mb
    )
    seconds = settings.docling_seconds if selected.engine == "docling" else settings.parse_seconds
    protect_process()
    with TemporaryDirectory(prefix="inkrag-parse-") as temporary:
        root = Path(temporary)
        source = root / ("source" + Path(name).suffix.lower())
        source.write_bytes(raw)
        (root / "name").write_text(Path(name).name, encoding="utf-8")
        (root / "options.json").write_text(selected.model_dump_json(), encoding="utf-8")
        environment = {
            "PATH": os.defpath,
            "HOME": str(root),
            "TMPDIR": str(root),
            "LANG": "C.UTF-8",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if settings.parse_models_path is not None:
            environment["INKPARSE_MODELS_PATH"] = str(Path(settings.parse_models_path).resolve())
        environment["HF_HUB_OFFLINE"] = "1"
        environment["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        environment["TRANSFORMERS_OFFLINE"] = "1"
        environment["TOKENIZERS_PARALLELISM"] = "false"
        # -I忽略环境/PYTHONPATH；保留venv解释器路径，不能resolve到系统Python。
        command = [
            sys.executable,
            "-I",
            "-m",
            "inkrag_platform.executor",
            str(root),
            str(memory_mb),
            str(seconds),
            "strict" if settings.sandbox_required else "development",
        ]
        try:
            completed = subprocess.run(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=seconds + 15,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ParseError("parse_timeout", "解析超过时间预算") from None
        result_path = root / "result.json"
        if completed.returncode != 0 or result_path.is_symlink() or not result_path.is_file():
            failure = root / "error"
            code = "sandbox_failed"
            if failure.is_file() and not failure.is_symlink() and failure.stat().st_size <= 80:
                candidate = failure.read_bytes().decode("ascii", errors="replace")
                if re.fullmatch(r"[a-z0-9_]{1,80}", candidate):
                    code = candidate
            if completed.returncode == -signal.SIGXCPU:
                code = "parse_cpu_limit"
            elif completed.returncode == -signal.SIGXFSZ:
                code = "output_limit"
            diagnostic = root / "error.json"
            if (
                diagnostic.is_file()
                and not diagnostic.is_symlink()
                and diagnostic.stat().st_size <= 8192
            ):
                try:
                    detail = ParseDiagnostic.model_validate_json(diagnostic.read_bytes())
                    if detail.code == code:
                        raise ParseError(
                            detail.code,
                            detail.message,
                            location=detail.location,
                            suggestion=detail.suggestion,
                            retryable=detail.retryable,
                        )
                except ValidationError:
                    pass
            raise ParseError(code, "隔离解析失败；请检查格式、资源预算和任务诊断")
        if result_path.stat().st_size > 32 * 1024 * 1024:
            raise ParseError("output_limit", "解析结果超过预算")
        result = ParseResult.model_validate_json(result_path.read_bytes())
        if result.source.sha256 != hashlib.sha256(raw).hexdigest():
            raise ParseError("invalid_artifact", "解析原件摘要不匹配")
        expected = {result.source.asset_id, *(item.asset_id for item in result.assets)}
        if None in expected or len(expected) > 256:
            raise ParseError("invalid_artifact", "解析资源引用不完整或超过预算")
        total = 0
        for identity in sorted(i for i in expected if i is not None):
            path = root / "objects" / identity
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                raise ParseError("invalid_artifact", "解析资源缺失或超过预算")
            total += path.stat().st_size
            if total > 64 * 1024 * 1024:
                raise ParseError("output_limit", "解析资源总量超过64MiB")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != identity:
                raise ParseError("invalid_artifact", "解析资源摘要不匹配")
            storage.put_bytes(data)
        save_stored_result(result, storage)
        return result
