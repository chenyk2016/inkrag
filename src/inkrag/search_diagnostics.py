"""从同次OpenSearch原生解释摘取有界数值事实，不保存Lucene原文或向量。"""

import math
import re

from pydantic import JsonValue


def _number(value: JsonValue) -> float | None:
    """解释中的分数只接受有限数字，布尔值不能冒充零或一。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _keyword_score(explanation: dict[str, JsonValue]) -> bool:
    """识别本适配器的词项或编号计分，未知树不猜造通道。"""
    pending = [(explanation, 0)]
    for _index in range(256):
        if not pending:
            return False
        item, depth = pending.pop()
        description = item.get("description")
        if isinstance(description, str) and description.startswith(
            ("weight(text:", "weight(lexical_terms:", "ConstantScore(identifiers:")
        ):
            return True
        children = item.get("details")
        if depth < 8 and isinstance(children, list):
            pending.extend(
                (child, depth + 1) for child in children[:128] if isinstance(child, dict)
            )
    return False


def _channel(explanation: dict[str, JsonValue]) -> str | None:
    """按原生解释形态区分两个受控子查询，单通道命中不能按数组位置命名。"""
    description = explanation.get("description")
    if isinstance(description, str) and re.fullmatch(r"within top \d+(?: docs)?", description):
        return "vector"
    return "keyword" if _keyword_score(explanation) else None


def native_explanation(
    explanation: JsonValue, score: float, mode: str, rank: int
) -> dict[str, JsonValue]:
    """只投影同次最终命中的通道分数和融合贡献；未知版本明确标未识别。

    OpenSearch RRF解释给出贡献1/(60+rank)，未单列rank。只在贡献与整数排名
    互相一致时反推该排名，标注来源；不在应用层重新排序或补查召回通道。
    """
    unknown: dict[str, JsonValue] = {"status": "unrecognized", "channels": []}
    if not isinstance(explanation, dict):
        return unknown
    value = _number(explanation.get("value"))
    if value is None or not math.isclose(value, score, rel_tol=1e-5, abs_tol=1e-8):
        return unknown
    if mode != "hybrid":
        return {
            "status": "captured",
            "channels": [{"channel": mode, "raw_score": value, "rank": rank}],
            "rank_source": "single_channel_final_order",
        }
    details = explanation.get("details")
    if explanation.get("description") != "rrf combination of:" or not isinstance(details, list):
        return unknown
    if not 1 <= len(details) <= 2:
        return unknown
    channels: list[JsonValue] = []
    names: set[str] = set()
    for item in details:
        if not isinstance(item, dict):
            return unknown
        children = item.get("details")
        contribution = _number(item.get("value"))
        if (
            item.get("description") != "rrf, rank_constant [60] normalization of:"
            or contribution is None
            or contribution <= 0
            or not isinstance(children, list)
            or len(children) != 1
            or not isinstance(children[0], dict)
        ):
            return unknown
        raw = children[0]
        channel, raw_score = _channel(raw), _number(raw.get("value"))
        recovered_rank = round(1 / contribution - 60)
        if (
            channel is None
            or channel in names
            or raw_score is None
            or not 1 <= recovered_rank <= 100_000
            or not math.isclose(contribution, 1 / (60 + recovered_rank), rel_tol=1e-5)
        ):
            return unknown
        names.add(channel)
        channels.append(
            {
                "channel": channel,
                "raw_score": raw_score,
                "rrf_contribution": contribution,
                "rank": recovered_rank,
            }
        )
    return {
        "status": "captured",
        "channels": channels,
        "combination": "rrf",
        "rank_constant": 60,
        "rank_source": "inverse_native_rrf_contribution",
    }
