"""用合成售后资料完成一次无密钥的入库、精确查询和快照重开。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from llama_index.core.embeddings import MockEmbedding

from inkrag import RAG


def main() -> None:
    """真实执行本地持久化与编号匹配，固定向量只用于演示安装。"""
    with TemporaryDirectory(prefix="inkrag-quickstart-") as directory:
        root = Path(directory)
        model = MockEmbedding(model_name="offline-demo", embed_dim=4)
        source = root / "support.md"
        source.write_text(
            "# 售后手册\n\nE102 温度传感器故障。先断电，再检查接线。\n",
            encoding="utf-8",
        )
        with RAG.open(root / "knowledge", embedding=model, lexical_profile="jieba-v1") as rag:
            receipt = rag.ingest(source, document_id="support-manual")
        with RAG.open(root / "knowledge", embedding=model) as rag:
            result = rag.retrieve("E102", mode="keyword")
            if result.snapshot_id != receipt.snapshot_id or len(result.hits) != 1:
                raise RuntimeError("重开后的快照或命中数量与入库结果不一致")
            hit = result.hits[0]
            if "先断电" not in hit.get_content() or hit.metadata["document_id"] != "support-manual":
                raise RuntimeError("检索结果缺少预期的正文或资料身份")
            print(hit.get_content())
            print("来源", hit.metadata["source"]["name"])


if __name__ == "__main__":
    main()
