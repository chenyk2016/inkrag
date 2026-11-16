"""演示原件/图片资产、解析结果以及检索来源的完整本地关联。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from inkparse import FileSource, Parser, ParseRequest, load_stored_result, save_result
from inkstore.local import LocalAssetStore
from llama_index.core.embeddings import MockEmbedding

from inkrag import RAG


def main() -> None:
    """使用真实样本文件与临时仓库；固定向量仅展示来源关联，不证明召回质量。"""
    source = Path(__file__).parents[1] / "assets/samples/sensor.pdf"
    with TemporaryDirectory(prefix="inkparse-assets-example-") as temporary:
        root = Path(temporary)
        store = LocalAssetStore.open(root / "parse-assets")
        result = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=store)
        packet = root / "parsed.json"
        save_result(result, packet)
        with RAG.open(
            root / "knowledge",
            assets=store,
            embedding=MockEmbedding(model_name="asset-example", embed_dim=4),
        ) as rag:
            rag.ingest_parsed(packet)
            origin = rag.retrieve("E102").hits[0].metadata["parse_origin"]
            assert rag.assets.read(origin["source_asset_id"]) == source.read_bytes()
            assert load_stored_result(store, origin["result_id"]).result_id == result.result_id
            print("已从检索结果解析到原件资产；同页图片数量：", len(origin["related_asset_ids"]))


if __name__ == "__main__":
    main()
