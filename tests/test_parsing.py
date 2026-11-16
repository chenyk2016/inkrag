"""验证独立交换协议、真实读取、失败保留与双包串联，不使用模拟解析结果。"""

import json
from pathlib import Path

import pytest
from inkparse import (
    FileSource,
    ParseError,
    ParseOptions,
    Parser,
    ParseRequest,
    export_markdown,
    load_result,
    save_result,
)
from llama_index.core.embeddings import MockEmbedding
from pydantic import ValidationError
from pypdf import PdfReader, PdfWriter

from inkrag import RAG, Error


def test_json_roundtrip_and_rag_bridge(tmp_path: Path) -> None:
    """真实Markdown经JSON保存重读后入库，来源、正文与重开后引用保持一致。"""
    original = tmp_path / "manual.md"
    original.write_text("# 售后\n\nA100保修12个月。", encoding="utf-8")
    result = Parser().parse(ParseRequest(source=FileSource(path=str(original))))
    packet = tmp_path / "result.json"
    save_result(result, packet)
    restored = load_result(packet)
    assert restored.result_id == result.result_id
    assert restored.document.texts[0].text == original.read_text()
    export_markdown(restored, tmp_path / "out.md")
    assert (tmp_path / "out.md").read_text() == original.read_text()
    original.unlink()
    model = MockEmbedding(model_name="bridge-test", embed_dim=4)
    with RAG.open(tmp_path / "rag", embedding=model) as rag:
        receipt = rag.ingest_parsed(packet, document_id="manual")
        hits = rag.retrieve("保修", top_k=100).hits
        assert "12个月" in hits[0].get_content()
        origin = hits[0].metadata["parse_origin"]
        assert origin["result_id"] == result.result_id
        assert origin["original_name"] == "manual.md"
        assert origin["element_ref"] == "#/texts/0"
        assert hits[0].metadata["source"]["name"] == "result.json"
        assert rag.jobs.get(receipt.job_id).status == "succeeded"
    with RAG.open(tmp_path / "rag", embedding=model) as rag:
        assert rag.retrieve("保修").snapshot_id == receipt.snapshot_id


def test_pdf_keeps_blank_page_and_real_text(tmp_path: Path) -> None:
    """Docling原生页来源保留PDF物理页，标为整页范围而非精确文字坐标。"""
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.append(PdfReader(Path(__file__).parents[1] / "assets/samples/sensor.pdf"))
    original = tmp_path / "source.pdf"
    writer.write(original)
    result = Parser().parse(ParseRequest(source=FileSource(path=str(original))))
    assert "E102" in result.document.texts[0].text
    assert result.document.texts[0].prov[0].page_no == 2
    assert set(result.document.pages) == {1, 2}
    assert result.warnings[0].code == "pdf_text_only"
    save_result(result, tmp_path / "parsed.json")
    with RAG.open(tmp_path / "rag", embedding=MockEmbedding(model_name="pdf", embed_dim=4)) as rag:
        rag.ingest_parsed(tmp_path / "parsed.json")
        assert rag.retrieve("E102").hits[0].metadata["page"] == 2


def test_invalid_input_and_unknown_protocol(tmp_path: Path) -> None:
    """空白、编码、格式、大小与协议错误均拒绝，不转换为成功空结果。"""
    source = tmp_path / "text.txt"
    for data, code in [(b" ", "empty_document"), (b"\xff", "parse_failed")]:
        source.write_bytes(data)
        with pytest.raises(ParseError) as caught:
            Parser().parse(ParseRequest(source=FileSource(path=str(source))))
        assert caught.value.code == code
    source.write_text("超过预算")
    with pytest.raises(ParseError, match="预算"):
        Parser().parse(
            ParseRequest(source=FileSource(path=str(source)), options=ParseOptions(max_bytes=1))
        )
    with pytest.raises(ValidationError):
        ParseRequest.model_validate_json('{"schema_version":"unknown","source":{"path":"a"}}')
    source.rename(tmp_path / "source.doc")
    with pytest.raises(ParseError) as caught:
        Parser().parse(ParseRequest(source=FileSource(path=str(tmp_path / "source.doc"))))
    assert caught.value.code == "unsupported_format"


def test_tampering_rejected_and_rag_failure_preserves_snapshot(tmp_path: Path) -> None:
    """修改原生可变内容使摘要失效；坏包入库留下失败任务且不替换已有知识。"""
    source = tmp_path / "source.txt"
    source.write_text("正确的原始内容")
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    packet = tmp_path / "result.json"
    save_result(result, packet)
    model = MockEmbedding(model_name="test", embed_dim=4)
    with RAG.open(tmp_path / "rag", embedding=model) as rag:
        old = rag.ingest_parsed(packet)
        result.document.texts[0].text = "被修改而未产生新版本"
        with pytest.raises(ValidationError):
            save_result(result, packet)
        assert load_result(packet).document.texts[0].text == "正确的原始内容"
        packet.write_text(result.model_dump_json())
        with pytest.raises(Error) as caught:
            rag.ingest_parsed(packet)
        assert caught.value.code == "invalid_result"
        assert caught.value.record_id
        assert rag.jobs.get(caught.value.record_id).status == "failed"
        assert rag.retrieve("内容").snapshot_id == old.snapshot_id


def test_atomic_export_failure_keeps_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """替换失败保留已保存交换包，并清除本次临时文件。"""
    import inkparse.io as output

    original = tmp_path / "original.txt"
    original.write_text("原始文字")
    result = Parser().parse(ParseRequest(source=FileSource(path=str(original))))
    packet = tmp_path / "result.json"
    save_result(result, packet)
    before = packet.read_bytes()

    def broken_replace(source: str, target: Path) -> None:
        """模拟目标文件替换失败，不执行任何修改。"""
        raise OSError("private-storage-detail")

    monkeypatch.setattr(output.os, "replace", broken_replace)
    with pytest.raises(ParseError) as caught:
        save_result(result, packet)
    assert caught.value.code == "write_failed"
    assert packet.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["original.txt", "result.json"]


def test_exchange_excludes_private_path_and_schema_is_versioned(tmp_path: Path) -> None:
    """输出只含原件名与字节摘要，公开结构可生成JSON Schema。"""
    from inkparse import ParseResult

    source = tmp_path / "local.txt"
    source.write_text("独立解析")
    request = ParseRequest.model_validate_json(
        json.dumps({"source": {"kind": "file", "path": str(source)}})
    )
    result = Parser().parse(request)
    assert str(tmp_path) not in result.model_dump_json()
    assert ParseResult.model_json_schema()["properties"]["schema_version"]["enum"] == [
        "inkparse.result.v3",
        "inkparse.result.v4",
    ]
