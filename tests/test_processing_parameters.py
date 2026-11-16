"""以真实解析结果验证可预览的切分参数与索引标题增强。"""

from pathlib import Path

import pytest
from inkparse import FileSource, Parser, ParseRequest
from llama_index.core import Document
from llama_index.core.utils import get_tokenizer

from inkrag.documents import read_documents, split_documents, split_native_documents
from inkrag.models import Error, ProcessingConfig, Provenance, Source
from inkrag.search import result_nodes


def test_real_parse_chunk_sizes_and_title_toggle(tmp_path: Path) -> None:
    """长正文的大小设置改变实际片段，标题增强仍可回查同一原文元素。"""
    path = tmp_path / "保修政策.txt"
    path.write_text(
        " ".join(f"Clause {i} covers sensor warranty for thirty six months." for i in range(150))
    )
    parsed = Parser().parse(ParseRequest(source=FileSource(path=str(path))))
    small = result_nodes("t", "kb", "doc", parsed, ProcessingConfig(chunk_size=64, chunk_overlap=8))
    large = result_nodes(
        "t", "kb", "doc", parsed, ProcessingConfig(chunk_size=192, chunk_overlap=8)
    )
    titled = result_nodes(
        "t",
        "kb",
        "doc",
        parsed,
        ProcessingConfig(chunk_size=64, chunk_overlap=8, include_title=True),
    )
    assert len(small) > len(large) > 1
    assert small[0].get_content() != large[0].get_content()
    tokenize = get_tokenizer()
    assert all(len(tokenize(node.get_content())) <= 64 for node in titled)
    assert all(node.metadata["indexed_title"] == path.name for node in titled)
    assert all(node.get_content().startswith(f"[标题] {path.name}\n\n") for node in titled)
    assert all(not node.get_content().startswith("[标题]") for node in small)
    assert {node.metadata["parse_origin"]["element_ref"] for node in titled} == {
        node.metadata["parse_origin"]["element_ref"] for node in small
    }
    origin = Provenance(
        document_id="doc",
        resource_id="r",
        resource_revision=1,
        source=Source(name=path.name, uri=str(path)),
        format="text",
        processing_revision="test",
    )
    sdk_nodes = split_documents(
        read_documents(path, origin, parsed),
        ProcessingConfig(chunk_size=64, chunk_overlap=8, include_title=True),
    )
    assert [node.get_content() for node in sdk_nodes] == [node.get_content() for node in titled]
    assert all(node.metadata["indexed_title"] == path.name for node in sdk_nodes)


def test_nearest_heading_and_element_boundaries(tmp_path: Path) -> None:
    """章节标题沿原生元素传递，短段落不会因较大预算而跨来源合并。"""
    path = tmp_path / "manual.md"
    path.write_text("# 使用指南\n\n开机前检查电源。\n\n## 保修条款\n\n传感器保修三十六个月。\n")
    parsed = Parser().parse(ParseRequest(source=FileSource(path=str(path))))
    nodes = result_nodes("t", "kb", "doc", parsed, ProcessingConfig(include_title=True))
    warranty = next(node for node in nodes if "传感器" in node.get_content())
    assert warranty.metadata["indexed_title"].endswith("保修条款")
    assert "开机前" not in warranty.get_content()
    assert warranty.metadata["parse_origin"]["element_ref"]


def test_actual_model_budget_and_title_overlap_rejection() -> None:
    """实际模型预算约束标题和正文总长度，非法组合不会静默截断。"""
    tokenizer = get_tokenizer()
    docs = [
        Document(
            text="Warranty covers sensors. " * 100,
            metadata={"format": "text", "source": {"name": "Warranty"}},
        )
    ]
    nodes = split_native_documents(
        docs,
        ProcessingConfig(chunk_size=256, chunk_overlap=8, include_title=True),
        tokenizer=tokenizer,
        max_tokens=48,
    )
    assert all(len(tokenizer(node.get_content())) <= 48 for node in nodes)
    with pytest.raises(Error, match="重叠预算"):
        split_native_documents(
            docs, ProcessingConfig(chunk_size=256, chunk_overlap=48), max_tokens=48
        )
    with pytest.raises(Error, match="标题占用"):
        split_native_documents(
            docs,
            ProcessingConfig(chunk_size=64, chunk_overlap=45, include_title=True),
            max_tokens=48,
        )
    long_title = [
        Document(
            text="body", metadata={"format": "text", "source": {"name": "Very long title " * 100}}
        )
    ]
    with pytest.raises(Error, match="标题占用"):
        split_native_documents(
            long_title, ProcessingConfig(chunk_size=64, chunk_overlap=0, include_title=True)
        )


def test_markdown_strategy_preserves_sections() -> None:
    """显式Markdown策略按标题分段，句子策略允许同一元素内标题共存。"""
    text = "# One\n\nFirst paragraph.\n\n# Two\n\nSecond paragraph."

    def document() -> Document:
        """每次返回独立文档，避免库内元数据变更影响对照。"""
        return Document(text=text, metadata={"format": "text", "source": {"name": "manual"}})

    sentence = split_native_documents([document()], ProcessingConfig(chunking="sentence"))
    markdown = split_native_documents([document()], ProcessingConfig(chunking="markdown"))
    assert len(sentence) == 1
    assert len(markdown) == 2
