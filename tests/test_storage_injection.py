"""验证存储替换边界，无需改动解析/RAG业务代码，也不会隐式回退本地。"""

import hashlib
from pathlib import Path

import pytest
from inkparse import FileSource, Parser, ParseRequest, load_stored_result, save_result
from inkstore import AssetInfo, AssetStorage, StorageError, StoredRecord
from inkstore.local import LocalAssetStore
from llama_index.core.embeddings import MockEmbedding

from inkrag import RAG, Error


class MemoryStorage:
    """仅用于接口替换验证的测试后端，不是生产回退实现。"""

    def __init__(self) -> None:
        """初始化独立字典及可控写入故障。"""
        self.data: dict[str, bytes] = {}
        self.records: dict[str, StoredRecord] = {}
        self.fail = False

    def put_bytes(self, data: bytes) -> AssetInfo:
        """模拟按摘要持久写入，故障时明确失败。"""
        if self.fail:
            raise StorageError("storage_failed", "测试存储不可用")
        asset_id = hashlib.sha256(data).hexdigest()
        self.data[asset_id] = data
        return AssetInfo(asset_id=asset_id, size_bytes=len(data))

    def read(self, asset_id: str) -> bytes:
        """验证缺失与摘要，不把未知资源当作空内容。"""
        if asset_id not in self.data:
            raise StorageError("missing_asset", "资源不存在")
        data = self.data[asset_id]
        if hashlib.sha256(data).hexdigest() != asset_id:
            raise StorageError("corrupt_asset", "资源摘要不一致")
        return data

    def get(self, asset_id: str) -> AssetInfo:
        """返回完整性验证后的元数据。"""
        return AssetInfo(asset_id=asset_id, size_bytes=len(self.read(asset_id)))

    def save_record(self, record: StoredRecord) -> None:
        """验证引用后存储记录快照，防止原生模型被外部变更。"""
        self.get(record.source_asset_id)
        for ref in record.references:
            self.get(ref.asset_id)
        if record.record_id in self.records and self.records[record.record_id] != record:
            raise StorageError("record_conflict", "不可变记录冲突")
        self.records[record.record_id] = StoredRecord.model_validate_json(record.model_dump_json())

    def load_record(self, record_id: str) -> StoredRecord:
        """读取记录并验证其引用，供业务适配层还原。"""
        record = self.records[record_id]
        self.get(record.source_asset_id)
        for ref in record.references:
            self.get(ref.asset_id)
        return StoredRecord.model_validate_json(record.model_dump_json())


@pytest.mark.parametrize("backend", ["local", "memory"])
def test_business_flow_with_replaced_storage(tmp_path: Path, backend: str) -> None:
    """相同业务流程替换两个存储实现，结果和资源关联保持不变。"""
    storage: AssetStorage = (
        LocalAssetStore.open(tmp_path / "external") if backend == "local" else MemoryStorage()
    )
    source = tmp_path / "source.txt"
    source.write_text("A100保修12个月")
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=storage)
    packet = tmp_path / "parsed.json"
    save_result(result, packet)
    with RAG.open(
        tmp_path / "knowledge",
        embedding=MockEmbedding(model_name="injection", embed_dim=4),
        assets=storage,
    ) as rag:
        assert rag.assets is storage
        rag.ingest_parsed(packet)
        hit = rag.retrieve("保修").hits[0]
        assert storage.read(hit.metadata["parse_origin"]["source_asset_id"]) == source.read_bytes()
        assert (
            load_stored_result(storage, hit.metadata["parse_origin"]["result_id"]).result_id
            == result.result_id
        )
    assert not (tmp_path / "knowledge" / "assets").exists()
    assert result.source.asset_id
    assert storage.read(result.source.asset_id) == source.read_bytes()  # RAG不关闭借入服务。


def test_no_implicit_storage_or_missing_injection_fallback(tmp_path: Path) -> None:
    """没有注入时仍可纯文本入库；需要资产的JSON明确拒绝，不创建隐式仓库。"""
    source = tmp_path / "source.txt"
    source.write_text("旧的有效内容")
    storage = MemoryStorage()
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=storage)
    packet = tmp_path / "parsed.json"
    save_result(result, packet)
    with RAG.open(
        tmp_path / "knowledge", embedding=MockEmbedding(model_name="no-assets", embed_dim=4)
    ) as rag:
        old = rag.ingest(source)
        with pytest.raises(Error) as caught:
            rag.ingest_parsed(packet)
        assert caught.value.code == "storage_required"
        with pytest.raises(Error, match="未注入"):
            _ = rag.assets
        assert rag.retrieve("内容").snapshot_id == old.snapshot_id
    assert not (tmp_path / "knowledge" / "assets").exists()


def test_injected_storage_failure_preserves_snapshot(tmp_path: Path) -> None:
    """注入后端失败时保留旧知识，绝不静默写入备用目录。"""
    storage = MemoryStorage()
    source = tmp_path / "source.txt"
    source.write_text("已提交的内容")
    with RAG.open(
        tmp_path / "knowledge",
        embedding=MockEmbedding(model_name="failed-store", embed_dim=4),
        assets=storage,
    ) as rag:
        old = rag.ingest(source)
        storage.fail = True
        source.write_text("失败的新内容")
        with pytest.raises(Error) as caught:
            rag.ingest(source)
        assert caught.value.code == "storage_failed"
        assert rag.retrieve("内容").snapshot_id == old.snapshot_id
    assert not (tmp_path / "knowledge" / "assets").exists()
