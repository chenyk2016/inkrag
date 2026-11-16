"""在 Python 进程中检索同一已发布知识库，对照三种检索方式。"""

import argparse
import json
import os

from inkrag_platform.sdk import published_connection

from inkrag import RAG


def main() -> None:
    """读取宿主配置和调用凭证，输出实际发布、命中和原文定位。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-id", required=True, help="已发布知识库的 ID")
    parser.add_argument("query", help="要检索的问题")
    args = parser.parse_args()
    token = os.environ.get("INKRAG_TOKEN")
    if not token:
        parser.error("请在运行环境提供 INKRAG_TOKEN")

    with RAG.open_published(published_connection(args.kb_id, token)) as rag:
        release_id = None
        for mode in ("keyword", "vector", "hybrid"):
            result = rag.retrieve(args.query, mode=mode)
            if release_id is not None and result["release_id"] != release_id:
                raise SystemExit("查询期间线上发布已切换，请重试。")
            release_id = result["release_id"]
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
