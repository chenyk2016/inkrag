"""用同一真实 OpenSearch 索引隔离比较中文词项与精确编号规则。"""

import argparse
import hashlib
import json
import math
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, JsonValue

from inkrag.lexical import JIEBA_LEXICAL, LEGACY_LEXICAL
from inkrag.lexical_jieba import JiebaAnalyzer
from inkrag.opensearch_keyword import (
    keyword_fields,
    keyword_mapping,
    keyword_query,
    opensearch_analyzer,
)
from inkrag.retrieval import validate_query

ARMS = ("standard", "jieba_terms", "jieba_identifiers")
TOP_K = 5


class Document(BaseModel):
    """待比较片段的稳定 ID 和同一份正文。"""

    id: str
    text: str


class Question(BaseModel):
    """每个证据组必须找回至少一个片段，预期空结果用空分组表示。"""

    id: str
    question: str
    groups: list[list[str]]
    cohort: str = ""


class Corpus(BaseModel):
    """复用已冻结的原文和问题，保留外部证据的原始位置。"""

    documents: list[Document]
    questions: list[Question]


@dataclass
class Measurement:
    """记录同一查询多次读取的排序和两种耗时。"""

    ids: list[str] = field(default_factory=list[str])
    prep_ms: list[float] = field(default_factory=list[float])
    client_ms: list[float] = field(default_factory=list[float])
    server_ms: list[int] = field(default_factory=list[int])


def fixture_data() -> tuple[list[dict[str, str]], dict[str, list[Question]]]:
    """构造可解释的型号撞词、别名规范化和越权反例。"""
    documents = [
        ("fixture-target", "AB-123 温度传感器故障。更换控制板后重新校准温度传感器。", "a"),
        ("fixture-near", "AB-1234 温度传感器故障。请更换温度传感器和控制板。", "a"),
        ("fixture-other", "ZX-123 温度传感器故障。需要校准温度传感器。", "a"),
        ("fixture-side", "控制板更换之后，应记录温度偏差和校准时间。", "a"),
        ("fixture-revoked", "AB-123 温度传感器故障。继续使用已撤销的临时旁路。", "a"),
        ("fixture-forbidden", "AB-123 温度传感器故障。外部租户材料。", "b"),
        ("fixture-legacy", "E102 控制模块故障。先关闭电源。", "a"),
        ("fixture-sibling", "E1020 控制模块故障。联系维修。", "a"),
    ]
    questions = [
        ("s01", "AB-123", [["fixture-target"]], "纯型号"),
        ("s02", "\uff41\uff42\uff0d\uff11\uff12\uff13", [["fixture-target"]], "全角与大小写"),
        ("s03", "AB-1234", [["fixture-near"]], "相近型号"),
        ("s04", "ZX-123", [["fixture-other"]], "共享编号尾缀"),
        ("s05", "AB-123 温度传感器故障", [["fixture-target"]], "型号与问句"),
        ("s06", "AB-1239", [], "未知型号"),
        ("s07", "E102", [["fixture-legacy"]], "无连字符编号"),
    ]
    return (
        [
            {"id": id_, "text": text, "tenant": tenant, "kb": "fixture"}
            for id_, text, tenant in documents
        ],
        {
            "fixture": [
                Question(id=id_, question=query, groups=groups, cohort=cohort)
                for id_, query, groups, cohort in questions
            ]
        },
    )


def make_query(question: Question, kb: str, arm: str) -> dict[str, JsonValue]:
    """固定范围和 top-k，仅替换全文查询字段与编号规则。"""
    filters: list[JsonValue] = [
        {"term": {"tenant": "a"}},
        {"term": {"kb": kb}},
        {"bool": {"must_not": [{"ids": {"values": ["fixture-revoked"]}}]}},
    ]
    query = validate_query(question.question, TOP_K, "keyword")
    profile = LEGACY_LEXICAL if arm == "standard" else JIEBA_LEXICAL
    if arm == "jieba_terms":
        terms = JiebaAnalyzer().analyze(query.text, query=True)
        prepared = replace(terms, identifiers=(), exact_identifier=None)
        return keyword_query(prepared, filters)
    prepared = query.keywords(opensearch_analyzer(profile))
    return keyword_query(prepared, filters)


def create_index(client: httpx.Client, name: str, docs: list[dict[str, str]]) -> None:
    """同一物理索引同时写原文和词项，避免候选版本及数据顺序成为变量。"""
    client.put(
        name,
        json={
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "properties": {
                    "tenant": {"type": "keyword"},
                    "kb": {"type": "keyword"},
                    **keyword_mapping(),
                }
            },
        },
    ).raise_for_status()
    lines: list[str] = []
    analyzer = opensearch_analyzer(JIEBA_LEXICAL)
    for doc in docs:
        fields = keyword_fields(analyzer.analyze(doc["text"]))
        lines.append(json.dumps({"index": {"_id": doc["id"]}}))
        lines.append(
            json.dumps(
                {
                    "tenant": doc["tenant"],
                    "kb": doc["kb"],
                    "text": doc["text"],
                    **fields,
                },
                ensure_ascii=False,
            )
        )
    response = (
        client.post(
            f"{name}/_bulk",
            params={"refresh": "true"},
            headers={"content-type": "application/x-ndjson"},
            content=("\n".join(lines) + "\n").encode(),
        )
        .raise_for_status()
        .json()
    )
    if response["errors"]:
        raise RuntimeError("A/B 样本写入不完整")


def p95(values: list[float]) -> float:
    """用最近秩报告样本的 P95，保留原始时间精度至取整前。"""
    return round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3)


def measure(
    client: httpx.Client, index: str, kb: str, question: Question
) -> dict[str, Measurement]:
    """轮换方案的执行顺序，预热两轮并记录七轮同进程请求。"""
    result = {arm: Measurement() for arm in ARMS}
    for iteration in range(9):
        offset = iteration % len(ARMS)
        for arm in (*ARMS[offset:], *ARMS[:offset]):
            prep_start = time.perf_counter()
            query = make_query(question, kb, arm)
            start = time.perf_counter()
            response = (
                client.post(
                    f"{index}/_search",
                    params={"allow_partial_search_results": "false"},
                    json={"query": query, "size": TOP_K, "_source": False},
                )
                .raise_for_status()
                .json()
            )
            elapsed = (time.perf_counter() - start) * 1000
            if response["timed_out"] or response["_shards"]["failed"]:
                raise RuntimeError("OpenSearch 搜索响应不完整")
            if iteration < 2:
                continue
            observed = [hit["_id"] for hit in response["hits"]["hits"]]
            run = result[arm]
            if run.client_ms and run.ids != observed:
                raise RuntimeError(f"排序重复执行不一致 {question.id} {arm}")
            run.ids = observed
            run.prep_ms.append((start - prep_start) * 1000)
            run.client_ms.append(elapsed)
            run.server_ms.append(response["took"])
    return result


def summarize(
    questions: list[Question], measured: list[dict[str, Measurement]]
) -> dict[str, object]:
    """计数按固定人工证据组，未标注片段只计作金标外命中。"""
    summary: dict[str, object] = {}
    for arm in ARMS:
        complete = 0
        first = 0
        outside = 0
        prep_ms: list[float] = []
        client_ms: list[float] = []
        server_ms: list[int] = []
        for question, run in zip(questions, measured, strict=True):
            ids = run[arm].ids
            accepted = set().union(*question.groups)
            complete += (
                int(all(set(group) & set(ids) for group in question.groups))
                if question.groups
                else int(not ids)
            )
            first += int(ids[0] in accepted) if ids else int(not question.groups)
            outside += sum(id_ not in accepted for id_ in ids)
            prep_ms.extend(run[arm].prep_ms)
            client_ms.extend(run[arm].client_ms)
            server_ms.extend(run[arm].server_ms)
        summary[arm] = {
            "all_evidence_groups_in_top_5_or_expected_empty": complete,
            "first_hit_labeled_or_expected_empty": first,
            "hits_outside_frozen_gold": outside,
            "query_preparation_p95_ms": p95(prep_ms),
            "http_round_trip_p95_ms": p95(client_ms),
            "opensearch_took_p95_ms": p95([float(value) for value in server_ms]),
        }
    return summary


def main() -> None:
    """用本机临时索引执行独立对照并保存可复核证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--corpus", type=Path, help="可选中文证据 JSON，默认运行内置编号反例")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if urlsplit(args.endpoint).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("仅允许使用本机隔离 OpenSearch")
    docs, questions = fixture_data()
    corpus_bytes = args.corpus.read_bytes() if args.corpus is not None else None
    if corpus_bytes is not None:
        corpus = Corpus.model_validate(json.loads(corpus_bytes)["input"])
        questions["kubernetes"] = corpus.questions
        docs.extend(
            {"id": doc.id, "text": doc.text, "tenant": "a", "kb": "kubernetes"}
            for doc in corpus.documents
        )
    with httpx.Client(base_url=args.endpoint.rstrip("/") + "/", trust_env=False) as client:
        cluster = client.get("").raise_for_status().json()["version"]["number"]
        if cluster != "3.3.2":
            raise RuntimeError(f"预期 OpenSearch 3.3.2，得到 {cluster}")
        results: dict[str, object] = {}
        for kb, cases in questions.items():
            index = "inkrag-jieba-ab-" + uuid4().hex
            cohort_docs = [doc for doc in docs if doc["kb"] == kb]
            try:
                create_index(client, index, cohort_docs)
                measured = [measure(client, index, kb, question) for question in cases]
                results[kb] = {
                    "documents": len(cohort_docs),
                    "questions": len(cases),
                    "summary": summarize(cases, measured),
                    "per_question": [
                        {
                            **question.model_dump(),
                            "query_terms": list(
                                JiebaAnalyzer()
                                .analyze(
                                    validate_query(question.question, TOP_K, "keyword").text,
                                    query=True,
                                )
                                .terms
                            ),
                            "ranked_ids": {arm: data[arm].ids for arm in ARMS},
                        }
                        for question, data in zip(cases, measured, strict=True)
                    ],
                }
                if any(
                    forbidden in run[arm].ids
                    for run in measured
                    for arm in ARMS
                    for forbidden in ("fixture-revoked", "fixture-forbidden")
                ):
                    raise RuntimeError("A/B 检索返回了撤销或范围外片段")
            finally:
                client.delete(index).raise_for_status()
    evidence = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "engine": f"OpenSearch {cluster}",
        "jieba_version": version("jieba"),
        "corpus": args.corpus.name if args.corpus is not None else "built-in fixtures",
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest() if corpus_bytes else None,
        "source_sha256": {
            name: hashlib.sha256(
                (Path(__file__).resolve().parents[1] / "src/inkrag" / name).read_bytes()
            ).hexdigest()
            for name in (
                "lexical.py",
                "lexical_adapters.py",
                "lexical_jieba.py",
                "opensearch_keyword.py",
                "retrieval.py",
            )
        },
        "design": {
            "standard": "正文 + standard + BM25",
            "jieba_terms": "共享 Jieba 词项 + whitespace + BM25；禁用独立编号路",
            "jieba_identifiers": "词项 + BM25；纯编号精确查，混合问句优先编号",
            "scope": "同组方案共享一个物理索引；单分片、固定语料与过滤，每题每方案预热 2 次和记录 7 次",
            "limitations": "单机串行词检索，不含向量、模型、HTTP 业务服务和生产负载。金标外片段不等同全部不相关。",
        },
        "results": results,
    }
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
