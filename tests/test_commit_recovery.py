"""验证已提交但响应失败时按持久化事实核对，不把成功知识改为失败。"""

from pathlib import Path

from llama_index.core.embeddings import MockEmbedding
from sqlalchemy import event
from sqlalchemy.orm import Session

from inkrag import RAG
from inkrag.database import JobRow


def test_post_commit_error_returns_persisted_receipt(tmp_path: Path) -> None:
    """模拟数据库提交成功后驱动/回调报错，成功任务和快照仍保持一致。"""
    source = tmp_path / "manual.txt"
    source.write_text("已持久提交的正文")
    fired = False

    def response_lost(session: Session) -> None:
        """只在最终任务成功事务完成后抛一次，避免混入前置任务登记。"""
        nonlocal fired
        if not fired and any(
            isinstance(row, JobRow) and row.status == "succeeded"
            for row in session.identity_map.values()
        ):
            fired = True
            raise RuntimeError("simulated commit response lost")

    with RAG.open(
        tmp_path / "knowledge", embedding=MockEmbedding(model_name="commit-check", embed_dim=4)
    ) as rag:
        event.listen(Session, "after_commit", response_lost)
        try:
            receipt = rag.ingest(source)
        finally:
            event.remove(Session, "after_commit", response_lost)
        assert fired
        assert rag.jobs.get(receipt.job_id).status == "succeeded"
        result = rag.retrieve("正文")
        assert result.snapshot_id == receipt.snapshot_id
        assert receipt.document_id == result.hits[0].metadata["document_id"]
        assert result.hits[0].get_content() == "已持久提交的正文"
