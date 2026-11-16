"""直接用 Python 包入库本地文件并检索，向量由硅基流动生成。"""

import argparse
from pathlib import Path

from inkrag import RAG, SiliconFlowEmbedding


def main() -> None:
    """入库后对照三种检索；省略文件可重开已有知识目录直接查询。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", type=Path, nargs="*", help="要入库的文件")
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--query", default="E102怎么处理？")
    args = parser.parse_args()
    with RAG.open(args.storage, embedding=SiliconFlowEmbedding()) as rag:
        for source in args.sources:
            receipt = rag.ingest(source)
            print(
                "已入库",
                source.name,
                "资料 ID",
                receipt.document_id,
                "片段数",
                receipt.node_count,
                "快照",
                receipt.snapshot_id,
            )
        for mode in ("keyword", "vector", "hybrid"):
            result = rag.retrieve(args.query, mode=mode, top_k=3)
            print(f"\n{mode} 命中 {len(result.hits)} 条")
            for hit in result.hits:
                print(hit.metadata["source"], "页码", hit.metadata["page"])
                print(hit.get_content())


if __name__ == "__main__":
    main()
