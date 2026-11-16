"""从公开行为验证本地 SDK；原生 MockEmbedding 只用于隔离故障边界。"""

import sqlite3
import subprocess
import sys
import traceback
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from llama_index.core import StorageContext, VectorStoreIndex
from llama_index.core.embeddings import MockEmbedding
from pydantic import JsonValue
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine, ExecutionContext

from inkrag import RAG, Error, HybridConfig, ParseOptions, ProcessingConfig
from inkrag import storage as snapshot_storage
from inkrag.lexical import LexicalProfile
from inkrag.published import PublishedQuery
from inkrag.sqlite_keyword import KEYWORD_INDEX

SAMPLE = Path(__file__).resolve().parents[1] / "assets/samples/sensor.pdf"


def test_open_published_preserves_request_and_releases_connection() -> None:
    """仅传调用者显式设置的选项，退出作用域后释放并拒绝查询。"""
    opened = False

    @contextmanager
    def connect() -> Generator[PublishedQuery]:
        """用可观察状态验证连接的打开和释放。"""
        nonlocal opened
        opened = True

        def execute(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
            """回传原始请求，检查缺省与显式字段。"""
            assert opened
            return request

        try:
            yield execute
        finally:
            opened = False

    with RAG.open_published(connect()) as rag:
        assert rag.retrieve("E102") == {"query": "E102"}
        assert rag.retrieve("E102", mode="hybrid", top_k=3, filters=[]) == {
            "query": "E102",
            "mode": "hybrid",
            "top_k": 3,
            "filters": [],
        }
    assert not opened
    with pytest.raises(Error) as closed:
        rag.retrieve("E102")
    assert closed.value.code == "closed"


@pytest.fixture
def model() -> MockEmbedding:
    """提供明确标识的原生固定向量模型，结果不当作检索质量证据。"""
    return MockEmbedding(model_name="unit-model", embed_dim=4)


def test_ingest_update_and_reopen(tmp_path: Path, model: MockEmbedding) -> None:
    """同身份更新替换旧片段，保留其他资料，任务与重开均读取已提交版本。"""
    root, file = tmp_path / "knowledge", tmp_path / "guide.md"
    file.write_text("# 售后\n\nE102传感器故障需要检查接线。", encoding="utf-8")
    with RAG.open(root, embedding=model) as rag:
        assert not rag.retrieve("E102").hits
        receipt = rag.ingest(file, document_id=" manual ")
        assert receipt.document_id == "manual"
        assert receipt.resource_revision == 1
        assert rag.jobs.get(receipt.job_id).status == "succeeded"
        file.write_text("# 更新\n\n新的故障规则。", encoding="utf-8")
        replacement = rag.ingest(file, document_id="manual")
        assert replacement.document_id == receipt.document_id
        assert replacement.resource_id == receipt.resource_id
        assert replacement.resource_revision == 2
        result = rag.retrieve("故障", top_k=100)
        assert result.snapshot_id == replacement.snapshot_id
        assert all("新的故障规则" in hit.get_content() for hit in result.hits)
        assert result.hits[0].metadata["section"] == "更新"
        assert result.hits[0].metadata["source"]["name"] == "guide.md"
        assert (root / result.hits[0].metadata["source"]["uri"]).read_text() == file.read_text()
    with RAG.open(root, embedding=model) as rag:
        assert rag.retrieve("故障").snapshot_id == replacement.snapshot_id
    with pytest.raises(Error, match="关闭"):
        rag.retrieve("故障")
    with pytest.raises(Error, match="关闭"):
        rag.jobs.get(receipt.job_id)


def test_default_document_id_uses_path_and_can_be_reused_after_rename(
    tmp_path: Path, model: MockEmbedding
) -> None:
    """同名不同路径各自入库，同路径或复用回执身份时更新已有资料。"""
    original = tmp_path / "source" / "manual.txt"
    other = tmp_path / "other" / "manual.txt"
    original.parent.mkdir()
    other.parent.mkdir()
    original.write_text("OLD102 手册旧内容", encoding="utf-8")
    other.write_bytes(original.read_bytes())
    with RAG.open(tmp_path / "knowledge", embedding=model) as rag:
        first = rag.ingest(original)
        second = rag.ingest(other)
        assert first.document_id != second.document_id
        original.write_text("REFRESH102 原路径修订", encoding="utf-8")
        repeated = rag.ingest(original)
        assert repeated.document_id == first.document_id
        assert repeated.resource_revision == first.resource_revision + 1
        renamed = original.rename(original.with_name("manual-v2.txt"))
        renamed.write_text("NEW102 手册新内容", encoding="utf-8")
        replacement = rag.ingest(renamed, document_id=first.document_id)
        assert replacement.document_id == first.document_id
        assert replacement.resource_id == first.resource_id
        assert replacement.resource_revision == repeated.resource_revision + 1
        assert not rag.retrieve("REFRESH102", mode="keyword").hits
        assert (
            rag.retrieve("NEW102", mode="keyword").hits[0].metadata["source"]["name"]
            == renamed.name
        )
        assert [
            hit.metadata["document_id"] for hit in rag.retrieve("OLD102", mode="keyword").hits
        ] == [second.document_id]


def test_pdf_keeps_physical_page(tmp_path: Path, model: MockEmbedding) -> None:
    """空白第一页跳过后，正文仍报告实际的第二页，不能重新从一编号。"""
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.append(PdfReader(SAMPLE))
    source = tmp_path / "manual.pdf"
    writer.write(source)
    with RAG.open(tmp_path / "knowledge", embedding=model) as rag:
        rag.ingest(source)
        hits = rag.retrieve("E102", top_k=100).hits
        assert any("E102" in hit.get_content() for hit in hits)
        assert all(hit.metadata["page"] == 2 and hit.metadata["section"] is None for hit in hits)


def test_embedded_dual_retrieval_reopens_and_tracks_document_replacement(
    tmp_path: Path, model: MockEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """三路共享快照和来源，关键词不访问模型，旧格式与文档替换保持正确。"""
    root, manual, other = tmp_path / "库 #100%", tmp_path / "manual.md", tmp_path / "other.md"
    manual.write_text("# 故障手册\n\nE102传感器需要检查接线。", encoding="utf-8")
    other.write_text("# 退货条款\n\n配件可在七日内办理退款。", encoding="utf-8")
    with RAG.open(root, embedding=model) as rag:
        assert rag.retrieve("E102", mode="hybrid").hits == []
        rag.ingest(manual, document_id="manual")
        receipt = rag.ingest(other, document_id="refund")
        for mode in ("keyword", "vector", "hybrid"):
            result = rag.retrieve("E102 传感器", top_k=2, mode=mode)
            assert result.snapshot_id == receipt.snapshot_id
            assert result.hits and all(hit.metadata["source"]["name"] for hit in result.hits)
            if mode != "vector":
                assert result.hits[0].metadata["document_id"] == "manual"

    # 删除测试派生文件模拟升级前创建的快照，避免测试依赖另一版源码。
    keyword_path = root / "snapshots" / receipt.snapshot_id / KEYWORD_INDEX
    keyword_path.unlink()
    with RAG.open(root, embedding=model) as rag:

        def unavailable(_self: MockEmbedding, _query: str) -> list[float]:
            """证明旧快照关键词检索也不触发查询向量化。"""
            raise AssertionError("keyword requested an embedding")

        with monkeypatch.context() as scope:
            scope.setattr(MockEmbedding, "_get_query_embedding", unavailable)
            keyword = rag.retrieve("传感", mode="keyword")
            assert [hit.metadata["document_id"] for hit in keyword.hits] == ["manual"]
            assert not rag.retrieve('" OR zznotpresent : *', mode="keyword").hits
        assert not keyword_path.exists()
        manual.write_text("# 新手册\n\nE103故障需要更换阀门。", encoding="utf-8")
        replacement = rag.ingest(manual, document_id="manual")
        assert not rag.retrieve("E102", mode="keyword").hits
        assert rag.retrieve("E103", mode="keyword").hits[0].metadata["resource_revision"] == 2
        assert rag.retrieve("退款", mode="keyword").snapshot_id == replacement.snapshot_id


def test_jieba_identifier_retrieval_is_exact_and_uses_committed_snapshot(
    tmp_path: Path, model: MockEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保留连字符和全角编号，精确关键词与混合查询都无需查询向量。"""
    root = tmp_path / "knowledge"
    fullwidth_id = "\uff3a\uff38-\uff10\uff10\uff14"
    with RAG.open(root, embedding=model, lexical_profile="jieba-v1") as rag:
        for identity, text in (
            ("manual", f"{fullwidth_id} 电机阀门检查线路。"),
            ("other", "ZX-0040 电机阀门检查线路。"),
            ("different", "AB-123 电机阀门检查线路。咖啡 café。"),
        ):
            source = tmp_path / f"{identity}.txt"
            source.write_text(text, encoding="utf-8")
            rag.ingest(source, document_id=identity)

    def unavailable(_self: MockEmbedding, _query: str) -> list[float]:
        """若精确检索误调用查询向量则立即失败。"""
        raise AssertionError("exact identifier requested an embedding")

    monkeypatch.setattr(MockEmbedding, "_get_query_embedding", unavailable)
    with RAG.open(root, embedding=model) as rag:
        for mode in ("keyword", "hybrid"):
            result = rag.retrieve("zx-004", mode=mode)
            assert [hit.metadata["document_id"] for hit in result.hits] == ["manual"]
            assert fullwidth_id in result.hits[0].get_content()
            assert not rag.retrieve("ZX-04", mode=mode).hits
        mixed = rag.retrieve("ZX-004 电机", mode="keyword")
        assert mixed.hits[0].metadata["document_id"] == "manual"
        assert len(mixed.hits) == 3
        assert rag.retrieve("café", mode="keyword").hits
        assert not rag.retrieve("cafe", mode="keyword").hits


@pytest.mark.parametrize("missing_part", [KEYWORD_INDEX, "keyword-profile"])
def test_current_keyword_snapshot_reports_incomplete_fts_without_fallback(
    tmp_path: Path, model: MockEmbedding, missing_part: str
) -> None:
    """新词表丢失与历史无词表有不同处理，查询不能静默改变分词语义。"""
    root, source = tmp_path / "knowledge", tmp_path / "manual.txt"
    source.write_text("AB-123 电机阀门", encoding="utf-8")
    with RAG.open(root, embedding=model, lexical_profile="jieba-v1") as rag:
        receipt = rag.ingest(source)
        (root / "snapshots" / receipt.snapshot_id / missing_part).unlink()
        with pytest.raises(Error) as missing:
            rag.retrieve("AB-123", mode="keyword")
        assert missing.value.code == "invalid_storage"
        assert rag.retrieve("AB-123", mode="vector").hits


def test_local_lexical_profile_switches_only_at_new_snapshot(
    tmp_path: Path, model: MockEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """词项改版在新快照生效，重建历史词项时继续复用旧向量。"""
    root = tmp_path / "knowledge"
    with RAG.open(root, embedding=model) as rag:
        for identity, text in (("manual", "AB-123 使用手册"), ("other", "AB-1230 使用手册")):
            source = tmp_path / f"{identity}.txt"
            source.write_text(text, encoding="utf-8")
            old = rag.ingest(source, document_id=identity)
        assert {
            hit.metadata["document_id"] for hit in rag.retrieve("AB-123", mode="keyword").hits
        } == {
            "manual",
            "other",
        }
    old_keyword = root / "snapshots" / old.snapshot_id / KEYWORD_INDEX
    original_bytes = old_keyword.read_bytes()
    embedded_texts: list[str] = []

    def track(self: MockEmbedding, text: str) -> list[float]:
        """记录嵌入边界的正文，以验证词项升级未重新计算旧向量。"""
        embedded_texts.append(text)
        return [0.5] * self.embed_dim

    monkeypatch.setattr(MockEmbedding, "_get_text_embedding", track)
    with RAG.open(root, embedding=model, lexical_profile="jieba-v1") as rag:
        assert rag.retrieve("AB-123", mode="keyword").snapshot_id == old.snapshot_id
        assert len(rag.retrieve("AB-123", mode="keyword").hits) == 2
        extra = tmp_path / "extra.txt"
        extra.write_text("新增文档的段落", encoding="utf-8")
        new = rag.ingest(extra, document_id="extra")
    assert embedded_texts == ["新增文档的段落"]
    assert old_keyword.read_bytes() == original_bytes
    with RAG.open(root, embedding=model) as rag:
        result = rag.retrieve("AB-123", mode="keyword")
        assert result.snapshot_id == new.snapshot_id
        assert [hit.metadata["document_id"] for hit in result.hits] == ["manual"]
        extra.write_text("修改新增文档后沿用分词配置", encoding="utf-8")
        inherited = rag.ingest(extra, document_id="extra")
        result = rag.retrieve("AB-123", mode="keyword")
        assert result.snapshot_id == inherited.snapshot_id
        assert [hit.metadata["document_id"] for hit in result.hits] == ["manual"]


def test_ingest_applies_parse_options_to_frozen_input(tmp_path: Path, model: MockEmbedding) -> None:
    """显式格式用于扩展名未知的资料，读取上限及引擎选项均到达实际边界。"""
    source = tmp_path / "manual.data"
    source.write_text("# 故障手册\n\nE102传感器需要检查接线。", encoding="utf-8")
    with RAG.open(tmp_path / "knowledge", embedding=model, write_lock_timeout=0) as rag:
        with pytest.raises(Error) as oversized:
            rag.ingest(source, parse_options=ParseOptions(format="markdown", max_bytes=8))
        assert oversized.value.code == "invalid_document"
        with pytest.raises(Error) as ocr:
            rag.ingest(source, parse_options=ParseOptions(format="markdown", ocr_enabled=True))
        assert ocr.value.code == "unsupported_option"
        receipt = rag.ingest(
            source,
            parse_options=ParseOptions(format="markdown"),
            processing=ProcessingConfig(include_title=True),
        )
        result = rag.retrieve("E102", mode="keyword")
        assert result.snapshot_id == receipt.snapshot_id
        assert result.hits[0].metadata["format"] == "markdown"
        assert result.hits[0].metadata["indexed_title"] == "故障手册"


def test_hybrid_config_controls_candidate_depth_and_weight(
    tmp_path: Path, model: MockEmbedding
) -> None:
    """实际改变两路候选数量及融合得分，并拒绝对单路传混合设置。"""
    with RAG.open(tmp_path / "knowledge", embedding=model) as rag:
        for name, content in (
            ("manual", "E102检查接线"),
            ("refund", "申请退款"),
            ("warranty", "保修一年"),
        ):
            source = tmp_path / f"{name}.txt"
            source.write_text(content, encoding="utf-8")
            rag.ingest(source, document_id=name)
        assert len(rag.retrieve("E102", mode="hybrid").hits) == 3
        config = HybridConfig(
            keyword_top_k=1, vector_top_k=1, fusion="relative_score", keyword_weight=0.8
        )
        result = rag.retrieve("E102", mode="hybrid", hybrid_config=config)
        assert 1 <= len(result.hits) <= 2
        assert result.hits[0].metadata["document_id"] == "manual"
        assert result.hits[0].get_score() >= 0.8
        with pytest.raises(Error) as wrong_mode:
            rag.retrieve("E102", hybrid_config=config)
        assert wrong_mode.value.code == "invalid_retrieval_options"
    with pytest.raises(ValueError, match="keyword_weight"):
        HybridConfig(keyword_weight=0.8)


def test_scan_page_is_not_silently_skipped(tmp_path: Path, model: MockEmbedding) -> None:
    """真实图像页面没有正文时拒绝整份 PDF，避免把缺失内容当作成功。"""
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    image = DecodedStreamObject()
    image.set_data(b"\xff\x00\x00")
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Scan"): image})}
    )
    content = DecodedStreamObject()
    content.set_data(b"q 100 0 0 100 0 0 cm /Scan Do Q")
    page[NameObject("/Contents")] = content
    source = tmp_path / "scan.pdf"
    writer.write(source)
    with RAG.open(tmp_path / "knowledge", embedding=model) as rag:
        with pytest.raises(Error) as caught:
            rag.ingest(source)
        assert caught.value.code == "ocr_required"
        assert caught.value.record_id
        assert rag.jobs.get(caught.value.record_id).status == "failed"
        assert not rag.retrieve("扫描").hits


@pytest.mark.parametrize("failure", ["parse", "embedding", "persist", "keyword_index", "commit"])
def test_failure_preserves_committed_knowledge(
    tmp_path: Path,
    model: MockEmbedding,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """分别在解析、模型、落盘和 SQL 提交注入故障，验证可见性与版本不提前推进。"""
    file, root = tmp_path / "manual.txt", tmp_path / "knowledge"
    file.write_text("已经提交的正文")
    with RAG.open(root, embedding=model) as rag:
        old = rag.ingest(file)
        file.write_bytes(b"\xff" if failure == "parse" else "本次不能提交的正文".encode())

        def broken_embedding(self: MockEmbedding, text: str) -> list[float]:
            """模拟提供方失败，异常原文不能进入公开记录。"""
            raise RuntimeError("private-provider-secret")

        def broken_persist(self: StorageContext, persist_dir: str) -> None:
            """模拟候选快照写入失败，原快照文件仍真实存在。"""
            raise OSError("private-filesystem-secret")

        def broken_keyword(
            _path: Path, _index: VectorStoreIndex, *, lexical_profile: LexicalProfile
        ) -> None:
            """中断词表落盘，已经写出的候选向量仍不能成为当前快照。"""
            assert lexical_profile == "legacy"
            raise OSError("private-keyword-filesystem-secret")

        def broken_commit(
            conn: Connection,
            cursor: object,
            statement: str,
            parameters: object,
            context: ExecutionContext,
            executemany: bool,
        ) -> None:
            """仅中断切换快照的 SQL，后续失败记录仍能保存。"""
            if statement.startswith("UPDATE knowledge_spaces"):
                raise OSError("private-database-secret")

        with monkeypatch.context() as scope:
            if failure == "embedding":
                scope.setattr(MockEmbedding, "_get_text_embedding", broken_embedding)
            if failure == "persist":
                scope.setattr(StorageContext, "persist", broken_persist)
            if failure == "keyword_index":
                scope.setattr(snapshot_storage, "persist_keyword_index", broken_keyword)
            if failure == "commit":
                event.listen(Engine, "before_cursor_execute", broken_commit)
            try:
                with pytest.raises(Error) as caught:
                    rag.ingest(file)
            finally:
                if failure == "commit":
                    event.remove(Engine, "before_cursor_execute", broken_commit)
        assert "private-" not in str(caught.value)
        assert "private-" not in "".join(traceback.format_exception(caught.value))
        assert caught.value.record_id
        assert rag.jobs.get(caught.value.record_id).status == "failed"
        for mode in ("vector", "keyword"):
            result = rag.retrieve("正文", mode=mode)
            assert result.snapshot_id == old.snapshot_id
            assert [hit.get_content() for hit in result.hits] == ["已经提交的正文"]
        file.write_text("恢复后的正文")
        assert rag.ingest(file).resource_revision == 2


def test_invalid_vectors_and_unexpected_query_failure_are_recorded(
    tmp_path: Path,
    model: MockEmbedding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型返回无效向量或抛出未知异常时，诊断不得被误记为成功。"""
    root, file = tmp_path / "knowledge", tmp_path / "manual.txt"
    file.write_text("诊断原文")
    with RAG.open(root, embedding=model) as rag:
        rag.ingest(file)
        for value in [[], [0.0] * 4, [float("nan")] * 4, [1.0] * 3]:

            def invalid_query(
                self: MockEmbedding, query: str, vector: list[float] = value
            ) -> list[float]:
                """返回本轮指定的无效查询向量。"""
                return vector

            with monkeypatch.context() as scope:
                scope.setattr(MockEmbedding, "_get_query_embedding", invalid_query)
                with pytest.raises(Error) as caught:
                    rag.retrieve("查询")
            assert caught.value.code == "invalid_embedding"

        def unexpected(self: MockEmbedding, query: str) -> list[float]:
            """模拟非业务异常，检验最外层执行诊断。"""
            raise RuntimeError("private-query-secret")

        with monkeypatch.context() as scope:
            scope.setattr(MockEmbedding, "_get_query_embedding", unexpected)
            with pytest.raises(Error) as caught:
                rag.retrieve("查询")
        assert "private-" not in str(caught.value)
        assert "private-" not in "".join(traceback.format_exception(caught.value))
    with sqlite3.connect(root / "metadata.sqlite3") as connection:
        rows = connection.execute("SELECT status,error_code FROM traces").fetchall()
    assert len(rows) == 5 and all(row[0] == "failed" for row in rows)


def test_multi_instance_updates_do_not_lose_documents(tmp_path: Path, model: MockEmbedding) -> None:
    """两个实例同时入库必须在锁内刷新快照，最终保留双方资料。"""
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("甲资料")
    second.write_text("乙资料")
    with (
        RAG.open(tmp_path / "knowledge", embedding=model) as a,
        RAG.open(tmp_path / "knowledge", embedding=model) as b,
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_job = pool.submit(a.ingest, first)
            second_job = pool.submit(b.ingest, second)
            results = [first_job.result(), second_job.result()]
        hits = a.retrieve("资料", top_k=100).hits
        assert {hit.get_content() for hit in hits} == {"甲资料", "乙资料"}
        assert {hit.metadata["resource_id"] for hit in hits} == {
            result.resource_id for result in results
        }


def test_model_and_directory_contracts(tmp_path: Path, model: MockEmbedding) -> None:
    """拒绝旧目录、损坏快照和不同模型，保持借入模型可继续使用。"""
    old = tmp_path / "old"
    old.mkdir()
    (old / "unrelated.txt").write_text("不能自动导入")
    with pytest.raises(Error):
        RAG.open(old, embedding=model)
    assert {p.name for p in old.iterdir()} == {"unrelated.txt"}
    root, file = tmp_path / "new", tmp_path / "file.txt"
    file.write_text("原文")
    with RAG.open(root, embedding=model, model_revision="weights-a") as rag:
        rag.ingest(file)
    with pytest.raises(Error) as caught:
        RAG.open(root, embedding=model, model_revision="weights-b")
    assert caught.value.code == "model_conflict"
    assert len(model.get_query_embedding("仍可借用")) == 4
    with RAG.open(root, embedding=model, model_revision="weights-a") as rag:
        assert rag.retrieve("原文").hits


def test_process_interruption_is_recoverable(tmp_path: Path, model: MockEmbedding) -> None:
    """真实子进程在嵌入时退出后，重开标记中断并继续读取旧快照。"""
    root, file = tmp_path / "knowledge", tmp_path / "file.txt"
    file.write_text("中断前原文")
    with RAG.open(root, embedding=model) as rag:
        previous = rag.ingest(file)
    script = """
import os, sys
from llama_index.core.embeddings import MockEmbedding
from inkrag import RAG
def stop(self, text):
    '在模型执行阶段终止子进程，模拟无法运行清理逻辑的中断。'
    os._exit(13)
MockEmbedding._get_text_embedding = stop
with RAG.open(sys.argv[1], embedding=MockEmbedding(model_name="unit-model", embed_dim=4)) as rag:
    rag.ingest(sys.argv[2])
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), str(file)], timeout=30, check=False
    )
    assert result.returncode == 13
    with RAG.open(root, embedding=model) as rag:
        assert rag.retrieve("原文").snapshot_id == previous.snapshot_id
    with sqlite3.connect(root / "metadata.sqlite3") as connection:
        rows = connection.execute("SELECT status,error_code FROM jobs ORDER BY rowid").fetchall()
    assert rows == [("succeeded", None), ("failed", "interrupted")]


def test_processing_configuration_is_explicit(
    tmp_path: Path, model: MockEmbedding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单次配置覆盖默认策略且不把来源拼入嵌入正文。"""
    inputs: list[str] = []

    def capture(self: MockEmbedding, text: str) -> list[float]:
        """观察真正进入嵌入接口的文本，验证来源排除规则。"""
        inputs.append(text)
        return [0.5] * self.embed_dim

    monkeypatch.setattr(MockEmbedding, "_get_text_embedding", capture)
    file = tmp_path / "long.md"
    file.write_text("# 标题\n\n" + "中文说明。" * 300)
    with RAG.open(tmp_path / "knowledge", embedding=model) as rag:
        result = rag.ingest(file, processing=ProcessingConfig(chunk_size=64, chunk_overlap=8))
        assert result.node_count > 1
        assert all(
            hit.metadata["section"] == "标题" for hit in rag.retrieve("说明", top_k=100).hits
        )

    assert inputs and all(
        "tenant_id" not in text and "processing_revision" not in text for text in inputs
    )


def test_special_directory_name_and_corrupt_storage(tmp_path: Path, model: MockEmbedding) -> None:
    """目录名中的 URL 字符不能改变 SQLite 路径，损坏索引必须明确失败。"""
    root, file = tmp_path / "knowledge?#本地", tmp_path / "file.txt"
    file.write_text("路径验证")
    with RAG.open(root, embedding=model) as rag:
        receipt = rag.ingest(file)
    assert (root / "metadata.sqlite3").is_file()
    with RAG.open(root, embedding=model) as rag:
        assert rag.retrieve("路径").hits
    (root / "snapshots" / receipt.snapshot_id / "index_store.json").write_text("broken")
    with pytest.raises(Error) as caught:
        RAG.open(root, embedding=model)
    assert caught.value.code == "invalid_storage"


@pytest.mark.parametrize("vector", [[], [0.0] * 4, [float("inf")] * 4, [1.0] * 3])
def test_invalid_ingestion_vectors_preserve_previous_snapshot(
    tmp_path: Path,
    model: MockEmbedding,
    monkeypatch: pytest.MonkeyPatch,
    vector: list[float],
) -> None:
    """无效入库向量不能被原生存储接受后直接变成已提交知识。"""
    root, file = tmp_path / "knowledge", tmp_path / "file.txt"
    file.write_text("有效原文")
    with RAG.open(root, embedding=model) as rag:
        previous = rag.ingest(file)

        def invalid(self: MockEmbedding, text: str) -> list[float]:
            """只替换本次模型输出，文件、索引与事务保持真实执行。"""
            return vector

        with monkeypatch.context() as scope:
            scope.setattr(MockEmbedding, "_get_text_embedding", invalid)
            with pytest.raises(Error):
                rag.ingest(file)
        assert rag.retrieve("原文").snapshot_id == previous.snapshot_id


@pytest.mark.parametrize("stale", [False, True])
def test_executor_uses_frozen_task_input(tmp_path: Path, model: MockEmbedding, stale: bool) -> None:
    """执行只读取已登记的原件；过期资料意图失败，不能覆盖更新后的快照。"""
    from filelock import FileLock

    from inkrag.database import Database, Jobs
    from inkrag.ingestion import execute_ingestion, prepare_ingestion
    from inkrag.storage import load_index

    database = Database(tmp_path / "metadata.sqlite3")
    database.prepare(model.model_name, model.model_name, fresh=True)
    file = tmp_path / "input.txt"
    file.write_text("冻结的原始正文")
    try:
        with FileLock(str(tmp_path / ".write.lock")):
            job_id = prepare_ingestion(
                database, tmp_path, file, "manual", "text", ProcessingConfig()
            )
            file.write_text("登记以后发生的变化")
            if stale:
                newer = prepare_ingestion(
                    database, tmp_path, file, "manual", "text", ProcessingConfig()
                )
                latest = execute_ingestion(database, tmp_path, model, newer)
                with pytest.raises(Error) as caught:
                    execute_ingestion(database, tmp_path, model, job_id)
                assert caught.value.code == "stale_revision"
                assert Jobs(database).get(job_id).status == "failed"
                assert database.space().snapshot_id == latest.snapshot_id
            else:
                result = execute_ingestion(database, tmp_path, model, job_id)
                index = load_index(tmp_path, result.snapshot_id, model)
                hits = index.as_retriever().retrieve("正文")
                assert [hit.get_content() for hit in hits] == ["冻结的原始正文"]
    finally:
        database.close()


def test_storage_format_does_not_coerce_boolean(tmp_path: Path, model: MockEmbedding) -> None:
    """格式版本是严格整数，不能利用 Python 的 True 等于 1 混过检查。"""
    root = tmp_path / "knowledge"
    RAG.open(root, embedding=model).close()
    (root / "inkrag.json").write_text('{"format":true}')
    with pytest.raises(Error) as caught:
        RAG.open(root, embedding=model)
    assert caught.value.code == "invalid_storage"
