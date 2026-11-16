"""显式准备Docling离线CPU模型；只下载公开工件，不读取或打印服务凭据。"""

import argparse
import hashlib
import json
import os
from pathlib import Path

from inkparse.adapters.docling.models import models_ready


def main() -> int:
    """下载固定引擎使用的布局/表格/中文OCR，生成工件摘要清单后才报告可用。"""
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    from docling.utils.model_downloader import download_models

    parser = argparse.ArgumentParser(description="准备Docling本地离线模型")
    parser.add_argument("--output", type=Path, default=Path(".inkrag/docling-models"))
    args = parser.parse_args()
    destination = args.output.resolve()
    download_models(
        destination,
        with_code_formula=False,
        with_picture_classifier=False,
        rapidocr_models=["onnxruntime:ch"],
        progress=False,
    )
    if not models_ready(str(destination)):
        raise RuntimeError("模型工件未准备完整")
    files: dict[str, str] = {}
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.name == "manifest.json" or ".cache" in path.parts:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        files[path.relative_to(destination).as_posix()] = digest.hexdigest()
    (destination / "manifest.json").write_text(
        json.dumps(files, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"已准备Docling离线模型，共{len(files)}项工件；解析阶段不下载模型。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
