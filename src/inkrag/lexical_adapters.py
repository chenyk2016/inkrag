"""接入原生分词和历史双字规则，并按已保存的分词版本选择实现。"""

import re

from .lexical import (
    LexicalAnalyzer,
    LexicalProfile,
    LexicalTerms,
    NativeLexicalText,
)
from .lexical_jieba import JiebaAnalyzer

_HAN = re.compile(r"([\u3400-\u9fff\uf900-\ufaff]+)")
_WORDS = re.compile(r"[^\W_]+")


class NativeAnalyzer:
    """委托索引引擎分析原文，例如 OpenSearch 的 standard。"""

    def analyze(self, text: str, *, query: bool = False) -> NativeLexicalText:
        """索引和查询原样传入引擎，保留其分词和重复词规则。"""
        return NativeLexicalText(text)


class LegacyBigramAnalyzer:
    """保留旧 SQLite 快照入库及查询时的单字、双字规则。"""

    def analyze(self, text: str, *, query: bool = False) -> LexicalTerms:
        """索引保留单字和双字，查询的多字连续片段只生成双字。"""
        result: list[str] = []
        for position, part in enumerate(_HAN.split(text)):
            if position % 2 == 0:
                result.extend(_WORDS.findall(part))
                continue
            if not query or len(part) == 1:
                result.extend(part)
            result.extend(part[index : index + 2] for index in range(len(part) - 1))
        return LexicalTerms(tuple(dict.fromkeys(result)) if query else tuple(result))


def lexical_analyzer(profile: LexicalProfile, *, legacy: LexicalAnalyzer) -> LexicalAnalyzer:
    """历史版本由宿主指定兼容实现，同名显式分词版本在各宿主共用。"""
    match profile:
        case "legacy":
            return legacy
        case "jieba-v1":
            return JiebaAnalyzer()
