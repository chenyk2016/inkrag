"""显式启用的真实中文模型验证，不使用固定向量作为质量证据。"""

import os
from pathlib import Path

import pytest
from llama_index.embeddings.fastembed import FastEmbedEmbedding

from inkrag import RAG

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("INKRAG_REAL_MODEL") != "1", reason="需显式启用真实本地模型与缓存"
    ),
]


def test_real_model_ingestion_retrieval_and_reopen(tmp_path: Path) -> None:
    """用真实模型验证格式来源、相关原文及跨实例重开。"""
    samples = Path(__file__).resolve().parents[1] / "assets/samples"
    model = FastEmbedEmbedding(model_name="BAAI/bge-small-zh-v1.5", threads=2)
    root = tmp_path / "knowledge"
    with RAG.open(root, embedding=model, model_revision="bge-small-zh-v1.5") as rag:
        receipts = [rag.ingest(samples / name) for name in ["sensor.md", "refund.md", "sensor.pdf"]]
        assert all(rag.jobs.get(receipt.job_id).status == "succeeded" for receipt in receipts)
        result = rag.retrieve("七天无理由退货的条件是什么", top_k=1)
        assert result.hits[0].metadata["source"]["name"] == "refund.md"
        assert "七天内" in result.hits[0].get_content()
        assert "不影响二次销售" in result.hits[0].get_content()
        sensor = rag.retrieve("E102温度传感器故障怎么处理", top_k=3)
        assert any("E102" in hit.get_content() for hit in sensor.hits)
        pdf = next(hit for hit in sensor.hits if hit.metadata["format"] == "pdf")
        assert pdf.metadata["page"] == 1
        print("真实查询命中：", result.hits[0].metadata["source"]["name"], result.hits[0].score)
        print(
            "故障来源：",
            [(hit.metadata["source"]["name"], hit.metadata["page"]) for hit in sensor.hits],
        )
        snapshot = sensor.snapshot_id
    with RAG.open(root, embedding=model, model_revision="bge-small-zh-v1.5") as rag:
        assert (
            rag.retrieve("七天无理由退货", top_k=1).hits[0].metadata["source"]["name"]
            == "refund.md"
        )
        assert rag.retrieve("E102").snapshot_id == snapshot
