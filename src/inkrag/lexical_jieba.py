"""使用固定 Jieba 词典切正文，并为可精确匹配的型号保留独立词项。"""

import re
import unicodedata
from functools import cache
from tempfile import TemporaryDirectory

from jieba import Tokenizer

from .lexical import LexicalTerms

# 仅识别有字母和数字的常见型号形态，避免把条款号、日期和普通数字当作设备编号。
_CODE = re.compile(
    r"(?<![a-z0-9_-])(?=[a-z0-9_-]*[0-9])[a-z][a-z0-9]{0,31}"
    r"(?:[-_][a-z0-9]{1,31}){0,3}(?![a-z0-9_-])",
    re.IGNORECASE | re.ASCII,
)
_WORD = re.compile(r"[^\W_]+")


@cache
def _tokenizer() -> Tokenizer:
    """加载随包固定的词典，避免读取其他程序留下的通用 Jieba 缓存。"""
    tokenizer = Tokenizer()
    with TemporaryDirectory(prefix="inkrag-jieba-") as cache_directory:
        tokenizer.tmp_dir = cache_directory
        tokenizer.initialize()
    return tokenizer


class JiebaAnalyzer:
    """在两种搜索后端上使用同一套中文词项和编号规则。"""

    def analyze(self, text: str, *, query: bool = False) -> LexicalTerms:
        """提取编号后切正文，查询词项去重，索引词项保留词频。"""
        normalized = unicodedata.normalize("NFKC", text).lower()
        identifiers = tuple(
            dict.fromkeys(match.group().upper() for match in _CODE.finditer(normalized))
        )
        body = _CODE.sub(" ", normalized)
        terms = (
            tuple(word for piece in _tokenizer().cut(body) for word in _WORD.findall(piece))
            if _WORD.search(body) is not None
            else ()
        )
        if not query:
            return LexicalTerms(terms, identifiers)
        # 纯编号沿用同一规则判断，不允许标点或解释文字触发精确模式。
        original = unicodedata.normalize("NFKC", text)
        exact = original.upper() if _CODE.fullmatch(original) else None
        return LexicalTerms(tuple(dict.fromkeys(terms)), identifiers, exact)
