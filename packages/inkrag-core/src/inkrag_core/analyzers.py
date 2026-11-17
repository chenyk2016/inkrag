"""提供不依赖词典的中文基线和可选的 Jieba 分词。"""

import re
import unicodedata
import warnings
from collections.abc import Callable, Iterable, Iterator
from functools import cache
from tempfile import TemporaryDirectory

_HAN = re.compile(r"([\u3400-\u9fff\uf900-\ufaff]+)")
_WORDS = re.compile(r"[^\W_]+(?:[-_][^\W_]+)*")


def _pieces(text: str) -> Iterable[tuple[bool, str]]:
    """统一宽度、大小写；保留英数型号、中文词组及分隔边界。"""
    return (
        (position % 2 == 1, part)
        for position, part in enumerate(_HAN.split(unicodedata.normalize("NFKC", text).lower()))
        if part
    )


class BigramAnalyzer:
    """将中文拆成单字及相邻双字，英文、数字及连字符型号保留为词项。"""

    fingerprint = "unicode-cjk-bigram-v1"

    def analyze(self, text: str) -> tuple[str, ...]:
        """保存重复词项，让 BM25 获得原始词频。"""
        terms: list[str] = []
        for han, part in _pieces(text):
            if han:
                terms.extend(part)
                terms.extend(part[index : index + 2] for index in range(len(part) - 1))
            else:
                terms.extend(_WORDS.findall(part))
        return tuple(terms)


@cache
def _jieba_cut() -> Callable[[str], Iterator[str]]:
    """只在选择 Jieba 时载入固定词典，并使用独立的缓存目录。"""
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"^invalid escape sequence '\\[.s]'$",
                category=SyntaxWarning,
                module=r"^(?:.*[/\\])?jieba[/\\](?:finalseg[/\\])?__init__(?:\.py)?$",
            )
            from jieba import Tokenizer
    except ModuleNotFoundError as exc:
        if exc.name != "jieba":
            raise
        raise RuntimeError("Jieba 分词需要安装 inkrag-core[jieba]") from None
    tokenizer = Tokenizer()
    with TemporaryDirectory(prefix="inkrag-core-jieba-") as directory:
        tokenizer.tmp_dir = directory
        tokenizer.initialize()
    return tokenizer.cut


class JiebaAnalyzer:
    """使用固定 Jieba 词典；产品可自行注入领域词典及版本标识。"""

    fingerprint = "jieba-0.42.1-v1"

    def analyze(self, text: str) -> tuple[str, ...]:
        """中文使用词典，型号与英文复用 Unicode 词项规则。"""
        terms: list[str] = []
        for han, part in _pieces(text):
            terms.extend(_jieba_cut()(part) if han else _WORDS.findall(part))
        return tuple(terms)
