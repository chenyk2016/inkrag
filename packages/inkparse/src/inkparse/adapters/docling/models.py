"""Docling固定离线工件清单；能力探测不初始化模型或读取用户资料。"""

from pathlib import Path

REQUIRED_MODELS = (
    "docling-project--docling-layout-heron/model.safetensors",
    "docling-project--docling-layout-heron/config.json",
    "docling-project--docling-layout-heron/preprocessor_config.json",
    "docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors",
    "docling-project--docling-models/model_artifacts/tableformer/accurate/tm_config.json",
    "RapidOcr/PP-OCRv6_rec_small.onnx",
    "RapidOcr/PP-OCRv6_det_small.onnx",
    "RapidOcr/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "RapidOcr/ppocrv6_dict.txt",
)


def models_ready(configured: str | None) -> bool:
    """仅检查固定模型文件是否存在且非空；不把存在检查宣称为完整性或质量验收。"""
    if not configured:
        return False
    root = Path(configured)
    try:
        return all(
            (root / name).is_file() and (root / name).stat().st_size > 0 for name in REQUIRED_MODELS
        )
    except OSError:
        return False
