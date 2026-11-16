"""使用正式检索接口，在同一发布上对照关键词、向量和混合召回。"""

import argparse
import os
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

type Mode = Literal["keyword", "vector", "hybrid"]
MODES: tuple[Mode, ...] = ("keyword", "vector", "hybrid")


class Origin(BaseModel):
    """读取命中原件的名称。"""

    original_name: str | None = None


class Source(BaseModel):
    """读取稳定资料身份和解析来源。"""

    document_id: str
    parse_origin: Origin | None = None


class Hit(BaseModel):
    """保存一条原文证据及其排序分数。"""

    text: str
    score: float | None
    metadata: Source


class EffectiveRequest(BaseModel):
    """使用服务实际执行的方式和结果数。"""

    mode: Mode
    top_k: int


class Result(BaseModel):
    """保存用于复核的发布身份、诊断编号与原文命中。"""

    release_id: str
    trace_id: str
    effective_request: EffectiveRequest
    hits: list[Hit]


def compare(client: httpx.Client, kb_id: str, query: str, top_k: int | None) -> list[Result]:
    """串行执行三次正常检索，发布改变时提示重试。"""
    results: list[Result] = []
    for mode in MODES:
        request: dict[str, str | int] = {"kb_id": kb_id, "query": query, "mode": mode}
        if top_k is not None:
            request["top_k"] = top_k
        response = client.post("/api/v2/retrieve", json=request)
        if not response.is_success:
            raise SystemExit(f"{mode} 检索失败（HTTP {response.status_code}）：{response.text}")
        result = Result.model_validate_json(response.content)
        if results and result.release_id != results[0].release_id:
            raise SystemExit("三次查询期间线上发布已切换，请重试。")
        results.append(result)
    return results


def main() -> None:
    """读取运行参数和私有令牌，输出三路结果及其来源。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="InkRAG 工作台地址")
    parser.add_argument("--kb-id", required=True, help="已发布知识库的 ID")
    parser.add_argument("--top-k", type=int, help="每种方式最多返回多少条；缺省沿用发布设置")
    parser.add_argument("query", help="检索问题")
    args = parser.parse_args()
    token = os.environ.get("INKRAG_TOKEN", "").strip()
    if not token:
        parser.error("请先把组织访问令牌或知识库调用凭证设置到 INKRAG_TOKEN")
    url = str(args.url)
    address = urlsplit(url)
    if address.scheme != "https" and not (
        address.scheme == "http" and address.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        parser.error("请使用 HTTPS 或本机 HTTP 工作台地址")
    with httpx.Client(
        base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=30, trust_env=False
    ) as client:
        results = compare(client, args.kb_id, args.query, args.top_k)
    print(f"发布 {results[0].release_id}")
    for result in results:
        effective = result.effective_request
        print(f"\n{effective.mode} · {len(result.hits)}/{effective.top_k} 条 · {result.trace_id}")
        for rank, hit in enumerate(result.hits, 1):
            source = hit.metadata.parse_origin
            name = source.original_name if source else None
            excerpt = " ".join(hit.text.split())[:150]
            print(f"  {rank}. {name or hit.metadata.document_id}  {excerpt}")


if __name__ == "__main__":
    main()
