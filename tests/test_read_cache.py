"""验证缓存只复用已提交快照，资料更新与跨实例提交仍立即可见。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NoReturn

import pytest
from llama_index.core.embeddings import MockEmbedding

from inkrag import RAG


def test_warm_reads_do_not_reload_and_changes_are_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """暖查询不触碰磁盘索引；另一实例提交后按新ID加载，不读旧缓存。"""

    def unexpected_load(*_args: object) -> NoReturn:
        """检测暖查询不应发生的磁盘索引重载。"""
        pytest.fail("暖查询重新加载索引")

    path = tmp_path / "source.txt"
    path.write_text("第一版原文")
    model = MockEmbedding(model_name="cache-check", embed_dim=4)
    with RAG.open(tmp_path / "knowledge", embedding=model) as first:
        first.ingest(path)
        assert first.retrieve("原文").hits[0].get_content() == "第一版原文"
        with monkeypatch.context() as scope:
            scope.setattr("inkrag.rag.load_index", unexpected_load)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(first.retrieve, ["原文"] * 16))
            assert all(r.hits[0].get_content() == "第一版原文" for r in results)
            results[0].hits[0].node.set_content("外部修改")
            assert first.retrieve("原文").hits[0].get_content() == "第一版原文"
        path.write_text("第二版原文")
        with RAG.open(tmp_path / "knowledge", embedding=model) as second:
            latest = second.ingest(path)
        result = first.retrieve("原文")
        assert result.snapshot_id == latest.snapshot_id
        assert result.hits[0].get_content() == "第二版原文"
