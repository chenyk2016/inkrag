"""同一结构化表格可走轻量SDK或正式搜索适配，不因格式枚举丢失互通。"""

from pathlib import Path

from inkparse import FileSource, Parser, ParseRequest, ParseResult, save_result
from inkparse.io import MAX_JSON_BYTES
from inkparse.models import ParsedPayload, fingerprint
from llama_index.core.embeddings import MockEmbedding

from inkrag import RAG
from inkrag.documents import MAX_FILE_BYTES


def test_csv_table_ingestion_and_json_interchange(tmp_path: Path) -> None:
    """CSV原生表格正文与定位穿过解析JSON，重开仍能回查同一资料。"""
    source = tmp_path / "policy.csv"
    source.write_text("项目,保修期限\n传感器,36个月\n", encoding="utf-8")
    parsed = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    exchange = tmp_path / "parsed.json"
    save_result(parsed, exchange)
    embedding = MockEmbedding(model_name="format-boundary-test", embed_dim=4)
    root = tmp_path / "knowledge"
    with RAG.open(root, embedding=embedding) as rag:
        rag.ingest(source, document_id="csv-raw")
        rag.ingest_parsed(exchange, document_id="csv-exchange")
    with RAG.open(root, embedding=embedding) as rag:
        hits = rag.retrieve("保修期限", top_k=20).hits
        assert {hit.metadata["document_id"] for hit in hits} == {"csv-raw", "csv-exchange"}
        assert all("36个月" in hit.get_content() for hit in hits)
        assert all(hit.metadata["format"] == "csv" for hit in hits)
        assert all(hit.metadata["parse_origin"]["element_ref"] == "#/tables/0" for hit in hits)


def test_sdk_ingests_exchange_larger_than_original_file_budget(tmp_path: Path) -> None:
    """有效JSON可以超过原件读取上限，保留非索引字段也不影响可检索正文。"""
    source = tmp_path / "policy.txt"
    source.write_text("Warranty applies.", encoding="utf-8")
    parsed = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    document = parsed.document.model_copy(deep=True)
    # 放大原始转写字段，使测试包超过20MiB，同时保持实际嵌入内容简短。
    document.texts[0].orig = "a" * MAX_FILE_BYTES
    payload = ParsedPayload.model_validate(
        {**parsed.model_dump(exclude={"result_id"}), "document": document}
    )
    parsed = ParseResult.model_validate({**payload.model_dump(), "result_id": fingerprint(payload)})
    exchange = tmp_path / "parsed.json"
    save_result(parsed, exchange)
    assert MAX_FILE_BYTES < exchange.stat().st_size <= MAX_JSON_BYTES

    embedding = MockEmbedding(model_name="exchange-boundary-test", embed_dim=4)
    with RAG.open(tmp_path / "knowledge", embedding=embedding) as rag:
        rag.ingest_parsed(exchange, document_id="warranty")
        hits = rag.retrieve("Warranty", mode="keyword").hits
        assert len(hits) == 1
        assert hits[0].get_content() == "Warranty applies."
        assert hits[0].metadata["parse_origin"]["result_id"] == parsed.result_id
