"""直接调用 inkrag 代码包，在临时索引上演示三种检索方式。"""

import argparse
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from inkparse import FileSource, Parser, ParseRequest
from llama_index.core.embeddings import BaseEmbedding
from opensearchpy import OpenSearch
from opensearchpy.exceptions import TransportError

from inkrag import SiliconFlowEmbedding
from inkrag.search import SearchBackend
from inkrag.siliconflow import DIMENSIONS

MODES: tuple[Literal["keyword", "vector", "hybrid"], ...] = ("keyword", "vector", "hybrid")


def run_demo(client: OpenSearch, embedding: BaseEmbedding, question: str) -> None:
    """直接建索引、入库、冻结和检索，结束时删除本次临时索引。"""
    search = SearchBackend(
        client, embedding, model_revision=embedding.model_name, dimensions=DIMENSIONS, replicas=0
    )
    tenant, kb, build = uuid4().hex, uuid4().hex, uuid4().hex
    generation = search.generation_name(tenant, kb, build)
    samples = Path(__file__).resolve().parents[1] / "assets" / "samples"
    try:
        search.create_build(tenant, kb, build)
        for filename in ("sensor.md", "refund.md"):
            source = FileSource(path=str(samples / filename))
            parsed = Parser().parse(ParseRequest(source=source))
            search.upsert_result(tenant, kb, generation, uuid4().hex, parsed)
        search.freeze(tenant, kb, generation)
        for mode in MODES:
            hits = search.query(tenant, kb, generation, question, mode=mode, top_k=3)
            print(f"\n{mode} · {len(hits)} 条")
            for hit in hits:
                print(hit.get_content())
    finally:
        try:
            client.transport.perform_request("DELETE", f"/{generation}")
        except TransportError as error:
            if error.status_code != 404:
                raise


def main() -> None:
    """使用用户指定的开发 OpenSearch 和环境中的硅基流动凭据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--opensearch-url", required=True, help="有权限创建测试索引的开发 OpenSearch"
    )
    parser.add_argument("--query", default="E102 故障应该怎么处理？")
    args = parser.parse_args()
    key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not key:
        parser.error("请先设置 SILICONFLOW_API_KEY")
    embedding = SiliconFlowEmbedding(key)
    client = OpenSearch([str(args.opensearch_url)], timeout=10, max_retries=0)
    try:
        client.transport.perform_request("GET", "/")
        run_demo(client, embedding, str(args.query))
    finally:
        client.close()


if __name__ == "__main__":
    main()
