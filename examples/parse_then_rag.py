"""演示独立JSON交换：先解析，再由RAG消费，不运行HTTP或后台队列。"""

import argparse
from pathlib import Path

from inkparse import FileSource, Parser, ParseRequest, export_markdown, load_result, save_result
from llama_index.embeddings.fastembed import FastEmbedEmbedding

from inkrag import RAG


def main() -> None:
    """把原件、交换JSON与知识目录放在显式输出位置，真实模型由调用者选择。"""
    args = argparse.ArgumentParser(description="独立解析→JSON→RAG")
    args.add_argument("source", type=Path)
    args.add_argument("--output", type=Path, required=True)
    values = args.parse_args()
    values.output.mkdir(parents=True, exist_ok=True)
    result = Parser().parse(ParseRequest(source=FileSource(path=str(values.source))))
    packet = values.output / "parsed.json"
    save_result(result, packet)
    export_markdown(result, values.output / "parsed.md")
    # 前半段已经独立完成；只有从这里起才需要模型与RAG。
    embedding = FastEmbedEmbedding(model_name="BAAI/bge-small-zh-v1.5", threads=2)
    with RAG.open(values.output / "knowledge", embedding=embedding) as rag:
        receipt = rag.ingest_parsed(packet)
        print("解析摘要：", load_result(packet).result_id)
        print("知识快照：", receipt.snapshot_id)
        for hit in rag.retrieve("E102怎么处理？").hits:
            print(hit.get_content(), hit.metadata["parse_origin"])


if __name__ == "__main__":
    main()
