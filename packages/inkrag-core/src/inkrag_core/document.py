"""独立于解析框架的 JSON 证据协议和可替换的切分接口。"""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, TypeGuard

from .contracts import Chunk, Source

SCHEMA = "inkrag.document.v1"
MAX_JSON_BYTES = 32 * 1024 * 1024
_BOUNDARY = re.compile(r"[。！？.!?；;\n]\s*|\s+")


def object_map(value: object) -> TypeGuard[dict[str, object]]:
    """把来自 JSON 的对象收窄成可检查字段的映射。"""
    return isinstance(value, dict)


def object_list(value: object) -> TypeGuard[list[object]]:
    """把来自 JSON 的数组收窄为一组待检查的值。"""
    return isinstance(value, list)


@dataclass(frozen=True, slots=True)
class Part:
    """可检索的正文单元及其原文位置。"""

    text: str
    element_ref: str
    page: int | None = None

    def __post_init__(self) -> None:
        """无内容、位置或有效页码的片段不能成为证据。"""
        if (
            not self.text.strip()
            or not self.element_ref.strip()
            or "\0" in self.text + self.element_ref
        ):
            raise ValueError("证据单元必须包含非空原文和定位")
        if self.page is not None and (
            type(self.page) is not int or not 1 <= self.page <= 2**31 - 1
        ):
            raise ValueError("页码须在 1 至 2147483647 之间，也可以省略")


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """可提交给任意索引的证据正文，完整视觉结构可由解析器另外保存。"""

    name: str
    parts: tuple[Part, ...]
    result_id: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        """保证交换数据有明确的原件名称及来源版本。"""
        if not self.name.strip() or "/" in self.name or "\\" in self.name or "\0" in self.name:
            raise ValueError("name 必须是原件名称")
        if self.result_id is not None and (
            not self.result_id.strip() or len(self.result_id) > 255 or "\0" in self.result_id
        ):
            raise ValueError("result_id 必须是长度不超过 255 的非空标识")
        if self.sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("sha256 必须是 64 位小写十六进制摘要")

    def to_json(self) -> str:
        """导出保留原文、页码、元素及版本的可移植结果。"""
        return json.dumps({"schema": SCHEMA, **asdict(self)}, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, data: str | bytes) -> "ParsedDocument":
        """读取受大小和版本约束的 JSON。结果里的来源作为定位数据处理。"""
        if len(data.encode() if isinstance(data, str) else data) > MAX_JSON_BYTES:
            raise ValueError("解析 JSON 超过 32 MiB")
        raw: object = json.loads(data)
        if not object_map(raw) or raw.get("schema") != SCHEMA:
            raise ValueError(f"需要版本为 {SCHEMA} 的解析 JSON")
        if raw.keys() - {"schema", "name", "parts", "result_id", "sha256"}:
            raise ValueError("解析 JSON 包含未知字段")
        name, parts = raw.get("name"), raw.get("parts")
        result_id, sha256 = raw.get("result_id"), raw.get("sha256")
        if not isinstance(name, str) or not object_list(parts):
            raise ValueError("解析 JSON 缺少 name 或 parts")
        if result_id is not None and not isinstance(result_id, str):
            raise ValueError("result_id 必须是字符串")
        if sha256 is not None and not isinstance(sha256, str):
            raise ValueError("sha256 必须是字符串")
        return cls(name, tuple(_part(item) for item in parts), result_id, sha256)


def _part(raw: object) -> Part:
    """校验单个 JSON 证据单元。"""
    if not object_map(raw) or raw.keys() - {"text", "element_ref", "page"}:
        raise ValueError("parts 中的对象包含未知字段")
    text, element_ref, page = raw.get("text"), raw.get("element_ref"), raw.get("page")
    if not isinstance(text, str) or not isinstance(element_ref, str):
        raise ValueError("证据单元缺少 text 或 element_ref")
    if page is not None and type(page) is not int:
        raise ValueError("page 必须是整数")
    return Part(text, element_ref, page)


class Parser(Protocol):
    """把文件转换为携带来源的证据正文。"""

    def parse(self, path: Path) -> ParsedDocument:
        """解析一份文件，保留每个单元的原文位置。"""
        ...


class Chunker(Protocol):
    """把每个证据单元分成有界片段，定位由调用者自动继承。"""

    def split(self, text: str) -> Iterable[str]:
        """按原文顺序返回可从输入回查的子串。"""
        ...


@dataclass(frozen=True, slots=True)
class CharacterChunker:
    """尽量在标点或空白处断开，按字符预算限制超长段落。"""

    size: int = 600
    overlap: int = 80

    def __post_init__(self) -> None:
        """校验前进幅度，避免过大重叠产生循环。"""
        if not 1 <= self.size <= 100_000 or not 0 <= self.overlap < self.size // 2 + 1:
            raise ValueError("切分长度须在 1 至 100000，重叠不得超过一半")

    def split(self, text: str) -> Iterable[str]:
        """保留原文字符，单个长段无法自然断开时按长度切分。"""
        start = 0
        while start < len(text):
            end = min(len(text), start + self.size)
            if end < len(text):
                for boundary in _BOUNDARY.finditer(text, start + (self.size * 3 // 4), end):
                    end = boundary.end()
            if piece := text[start:end].strip():
                yield piece
            if end == len(text):
                break
            start = end - self.overlap


def chunks_for(document_id: str, parsed: ParsedDocument, chunker: Chunker) -> tuple[Chunk, ...]:
    """切分并生成内容绑定的片段 ID，使旧图边无法命中已修改的原文。"""
    if not document_id.strip() or len(document_id) > 255 or "\0" in document_id:
        raise ValueError("document_id 必须是长度不超过 255 的非空字符串")
    chunks: list[Chunk] = []
    for part in parsed.parts:
        source = Source(parsed.name, part.element_ref, part.page, parsed.result_id, parsed.sha256)
        for text in chunker.split(part.text):
            if len(chunks) >= 10_000:
                raise ValueError("一份资料最多生成 10000 个片段")
            if not text.strip() or text not in part.text or len(text) > 100_000:
                raise ValueError("切分结果须为长度不超过 100000 的非空原文子串")
            payload = json.dumps(
                [document_id, len(chunks), asdict(source), text], ensure_ascii=False
            )
            chunks.append(
                Chunk(hashlib.sha256(payload.encode()).hexdigest(), document_id, text, source)
            )
    if not chunks:
        raise ValueError("没有可检索正文，资料未被替换")
    return tuple(chunks)
