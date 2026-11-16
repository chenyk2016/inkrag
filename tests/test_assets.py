"""验证真实资产字节、解析引用、删除保护、故障与跨仓库RAG溯源。"""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from inkparse import (
    FileSource,
    ParseError,
    Parser,
    ParseRequest,
    copy_result,
    load_stored_result,
    save_result,
    save_stored_result,
)
from inkstore import StorageError, StoredRecord
from inkstore.local import LocalAssetStore as AssetStore
from llama_index.core.embeddings import MockEmbedding
from pydantic import ValidationError
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
)

from inkrag import RAG, Error


class FixtureWriter(PdfWriter):
    """生成合法间接图片对象的PDF测试夹具。"""

    def register_image(self, image: DecodedStreamObject) -> IndirectObject:
        """在Writer内部登记图像，避免不合法的直接对象导致提取器报错。"""
        return self._add_object(image)


def illustrated_pdf(path: Path) -> bytes:
    """在真实文本PDF中加入1像素嵌入图，保留正文用于文本检索。"""
    writer = FixtureWriter()
    writer.append(PdfReader(Path(__file__).parents[1] / "assets/samples/sensor.pdf"))
    page = writer.pages[0]
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
    resources = page["/Resources"]
    assert isinstance(resources, DictionaryObject)
    resources[NameObject("/XObject")] = DictionaryObject(
        {NameObject("/TestImage"): writer.register_image(image)}
    )
    writer.write(path)
    return path.read_bytes()


def test_store_dedup_change_and_reopen(tmp_path: Path) -> None:
    """相同字节复用资产，同名变化产生新ID；打开后读取原始字节不变。"""
    store = AssetStore.open(tmp_path / "assets")
    source = tmp_path / "a.txt"
    source.write_bytes(b"old")
    old = store.put_file(source)
    assert store.put_bytes(b"old") == old
    source.write_bytes(b"new")
    new = store.put_file(source)
    assert old.asset_id != new.asset_id
    assert len(list((tmp_path / "assets" / "objects").iterdir())) == 2
    assert len(store.list_assets(limit=1, offset=1)) == 1
    assert AssetStore.open(tmp_path / "assets").read(old.asset_id) == b"old"
    store.delete_unreferenced(new.asset_id)
    with pytest.raises(StorageError) as caught:
        store.read(new.asset_id)
    assert caught.value.code == "missing_asset"


def test_parse_commits_original_images_and_relations(tmp_path: Path) -> None:
    """解析前保存原件，嵌入图与原生图片元素绑定，结果与引用可重开读取。"""
    source = tmp_path / "manual.pdf"
    raw = illustrated_pdf(source)
    store = AssetStore.open(tmp_path / "assets")
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=store)
    assert result.source.asset_id is not None
    assert result.source.asset_id == hashlib.sha256(raw).hexdigest()
    assert store.read(result.source.asset_id) == raw
    assert len(result.assets) == len(result.document.pictures) == 1
    image = result.assets[0]
    assert image.element_ref == "#/pictures/0"
    assert image.page == 1
    assert store.read(image.asset_id).startswith(b"\x89PNG")
    assert store.list_records(result.source.asset_id) == [result.result_id]
    save_stored_result(result, store)
    assert len(store.list_records()) == 1
    assert load_stored_result(AssetStore.open(tmp_path / "assets"), result.result_id) == result
    for asset_id in [result.source.asset_id, image.asset_id]:
        with pytest.raises(StorageError) as caught:
            store.delete_unreferenced(asset_id)
        assert caught.value.code == "asset_in_use"


def test_rag_copies_assets_and_resolves_after_source_removal(tmp_path: Path) -> None:
    """RAG导入引用资源后，原文件和源仓库移走仍能从命中取回原件和图片。"""
    source = tmp_path / "manual.pdf"
    raw = illustrated_pdf(source)
    store = AssetStore.open(tmp_path / "parse-assets")
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=store)
    packet = tmp_path / "parsed.json"
    save_result(result, packet)
    model = MockEmbedding(model_name="assets-test", embed_dim=4)
    target = AssetStore.open(tmp_path / "target-store")
    copy_result(result, store, target)
    with RAG.open(tmp_path / "rag", embedding=model, assets=target) as rag:
        rag.ingest_parsed(packet)
    source.unlink()
    (tmp_path / "parse-assets").rename(tmp_path / "offline-store")
    with RAG.open(tmp_path / "rag", embedding=model, assets=target) as rag:
        hit = rag.retrieve("E102").hits[0]
        origin = hit.metadata["parse_origin"]
        assert rag.assets.read(origin["source_asset_id"]) == raw
        assert origin["related_asset_ids"] == [result.assets[0].asset_id]
        assert rag.assets.read(origin["related_asset_ids"][0]).startswith(b"\x89PNG")
        assert load_stored_result(rag.assets, origin["result_id"]).source.name == "manual.pdf"


def test_missing_resource_prevents_rag_commit(tmp_path: Path) -> None:
    """源仓库缺失资源时入库失败、旧知识不变，不把失效引用提交到新快照。"""
    source = tmp_path / "a.txt"
    source.write_text("有效旧知识")
    store = AssetStore.open(tmp_path / "store")
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=store)
    packet = tmp_path / "result.json"
    save_result(result, packet)
    with RAG.open(
        tmp_path / "rag", embedding=MockEmbedding(model_name="missing", embed_dim=4), assets=store
    ) as rag:
        old = rag.ingest(source)
        assert result.source.asset_id
        # 直接模拟磁盘丢失；公开删除接口会保护已引用资源。
        (tmp_path / "store" / "objects" / result.source.asset_id).unlink()
        with pytest.raises(Error) as caught:
            rag.ingest_parsed(packet)
        assert caught.value.code == "missing_asset"
        assert rag.retrieve("知识").snapshot_id == old.snapshot_id
        assert caught.value.record_id
        assert rag.jobs.get(caught.value.record_id).status == "failed"


def test_integrity_and_path_validation(tmp_path: Path) -> None:
    """篡改、超限和路径穿越都明确拒绝；不沿ID读取任意文件。"""
    store = AssetStore.open(tmp_path / "store")
    asset = store.put_bytes(b"original")
    with pytest.raises(ValidationError):
        store.read("../../secret")
    (tmp_path / "store" / "objects" / asset.asset_id).write_bytes(b"tampered")
    with pytest.raises(StorageError) as caught:
        store.read(asset.asset_id)
    assert caught.value.code == "corrupt_asset"
    with pytest.raises(StorageError):
        store.put_bytes(b"")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("不要修改")
    with pytest.raises(StorageError):
        AssetStore.open(unrelated)
    assert (unrelated / "keep.txt").read_text() == "不要修改"


@pytest.mark.parametrize("damage", ["marker", "links"])
def test_open_rejects_incomplete_local_metadata(tmp_path: Path, damage: str) -> None:
    """仓库标记和完整的可读元数据是打开成功的条件。"""
    root = tmp_path / "store"
    AssetStore.open(root)
    if damage == "marker":
        (root / "inkstore.json").write_text('{"format":true}', encoding="utf-8")
    else:
        with sqlite3.connect(root / "metadata.sqlite3") as db:
            db.execute("DROP TABLE links")
    with pytest.raises(StorageError) as caught:
        AssetStore.open(root)
    assert caught.value.code == "invalid_store"


def test_local_store_reports_corrupt_record(tmp_path: Path) -> None:
    """损坏记录无法读取或幂等提交，也不会被新内容覆盖。"""
    root = tmp_path / "store"
    store = AssetStore.open(root)
    asset = store.put_bytes(b"original")
    record = StoredRecord(record_id=asset.asset_id, source_asset_id=asset.asset_id, payload="{}")
    store.save_record(record)
    with sqlite3.connect(root / "metadata.sqlite3") as db:
        db.execute("UPDATE records SET payload=? WHERE id=?", ("damaged", record.record_id))
    with pytest.raises(StorageError) as caught:
        store.load_record(record.record_id)
    assert caught.value.code == "corrupt_record"
    with pytest.raises(StorageError) as caught:
        store.save_record(record)
    assert caught.value.code == "corrupt_record"


def test_failed_parse_keeps_original_without_result(tmp_path: Path) -> None:
    """解析失败保留已导入原件供重试，不能登记成功结果，允许清理未引用资产。"""
    source = tmp_path / "bad.txt"
    source.write_bytes(b"\xff")
    store = AssetStore.open(tmp_path / "store")
    with pytest.raises(ParseError):
        Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=store)
    identity = hashlib.sha256(b"\xff").hexdigest()
    assert store.read(identity) == b"\xff"
    assert store.list_records() == []
    store.delete_unreferenced(identity)


def test_concurrent_dedup_and_result_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多实例写同一资产幂等；结果SQL失败不会登记半份引用。"""
    root = tmp_path / "store"
    AssetStore.open(root)

    def put(_: int) -> str:
        """独立仓库实例并发写入相同字节。"""
        return AssetStore.open(root).put_bytes(b"same").asset_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(put, range(8)))
    assert len(set(ids)) == 1
    store = AssetStore.open(root)
    source = tmp_path / "a.txt"
    source.write_text("持久化失败的解析")
    # SQLite触发器只阻止结果提交，不影响原件导入。
    with sqlite3.connect(root / "metadata.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER fail_result BEFORE INSERT ON records BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    with pytest.raises(StorageError) as caught:
        Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=store)
    assert caught.value.code == "storage_failed"
    assert store.list_records() == []
    assert store.read(hashlib.sha256(source.read_bytes()).hexdigest()) == source.read_bytes()
