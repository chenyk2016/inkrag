"""验证同名文件的默认 ID，以及改名后沿用回执 ID 更新原资料。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from inkrag import RAG, SiliconFlowEmbedding


def main() -> None:
    """使用临时文件与真实硅基流动向量，不改动已有知识目录。"""
    with TemporaryDirectory(prefix="inkrag-document-id-") as directory:
        root = Path(directory)
        original = root / "support" / "manual.md"
        other = root / "training" / "manual.md"
        original.parent.mkdir()
        other.parent.mkdir()
        original.write_text("LEGACY102 旧版手册要求检查接线。", encoding="utf-8")
        other.write_text("TRAINING102 培训手册要求核对记录。", encoding="utf-8")

        with RAG.open(root / "knowledge", embedding=SiliconFlowEmbedding()) as rag:
            first = rag.ingest(original)
            second = rag.ingest(other)
            assert first.document_id != second.document_id
            print(f"support/manual.md  {first.document_id}")
            print(f"training/manual.md {second.document_id}")

            renamed = original.rename(original.with_name("manual-v2.md"))
            renamed.write_text("UPDATED102 新版手册要求断电后检查接线。", encoding="utf-8")
            replacement = rag.ingest(renamed, document_id=first.document_id)
            assert replacement.document_id == first.document_id
            assert replacement.resource_id == first.resource_id
            assert replacement.resource_revision == first.resource_revision + 1

            updated = rag.retrieve("UPDATED102", mode="hybrid", top_k=1)
            assert updated.hits[0].metadata["document_id"] == first.document_id
            assert updated.hits[0].metadata["source"]["name"] == "manual-v2.md"
            assert not rag.retrieve("LEGACY102", mode="keyword").hits
            training = rag.retrieve("TRAINING102", mode="keyword", top_k=1)
            assert training.hits[0].metadata["document_id"] == second.document_id
            print(
                f"改名后复用资料 ID，原资料版本 {first.resource_revision} → {replacement.resource_revision}"
            )


if __name__ == "__main__":
    main()
