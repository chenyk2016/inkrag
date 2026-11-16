"""分词接口及词项契约，供关键词索引写入和检索共用。"""

from dataclasses import dataclass
from typing import Literal, Protocol

type LexicalProfile = Literal["legacy", "jieba-v1"]

LEGACY_LEXICAL: LexicalProfile = "legacy"
JIEBA_LEXICAL: LexicalProfile = "jieba-v1"
IDENTIFIER_BOOST = 8.0


@dataclass(frozen=True)
class NativeLexicalText:
    """把原文交给索引引擎自己的分析器。"""

    text: str


@dataclass(frozen=True)
class LexicalTerms:
    """可由两个后端消费的预切词项及规范化编号。

    正文词项应为后端能够原样索引的单词单元（例如汉字词和不含空格的字母词），
    索引端保留重复词以计算词频。纯编号标记为一个规范化编号值，只用于查询。
    """

    terms: tuple[str, ...]
    identifiers: tuple[str, ...] = ()
    exact_identifier: str | None = None


type LexicalInput = NativeLexicalText | LexicalTerms


class LexicalAnalyzer(Protocol):
    """为一份索引提供成对的正文和查询分析规则，约定分词输出形式。"""

    def analyze(self, text: str, *, query: bool = False) -> LexicalInput:
        """返回由引擎分析的原文，或供索引直接消费的词项和编号。"""
        ...
