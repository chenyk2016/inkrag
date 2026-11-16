"""使用原生本地存储读写独立快照，不把当前已提交快照原地改写。"""

import os
import re
from pathlib import Path

from llama_index.core import StorageContext, VectorStoreIndex
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.data_structs.data_structs import IndexDict
from llama_index.core.vector_stores.simple import SimpleVectorStore

from .lexical import LEGACY_LEXICAL, LexicalProfile
from .models import Error
from .retrieval import check_vector
from .sqlite_keyword import KEYWORD_INDEX, keyword_profile, persist_keyword_index


def snapshot_path(root: Path, snapshot_id: str) -> Path:
    """将快照身份限制为系统生成的十六进制 ID，拒绝目录逃逸。"""
    if re.fullmatch(r"[0-9a-f]{32}", snapshot_id) is None:
        raise Error("invalid_storage", "快照引用不合法")
    return root / "snapshots" / snapshot_id


def snapshot_lexical_profile(root: Path, snapshot_id: str) -> LexicalProfile:
    """读取当前本地快照的分词版本，供后续写入在锁内继承。"""
    return keyword_profile(snapshot_path(root, snapshot_id) / KEYWORD_INDEX)


def load_index(root: Path, snapshot_id: str | None, embedding: BaseEmbedding) -> VectorStoreIndex:
    """创建独立索引实例；后续写入不会污染其他读取者或旧快照。"""
    if snapshot_id is None:
        return VectorStoreIndex(nodes=[], embed_model=embedding)
    directory = snapshot_path(root, snapshot_id)
    if not directory.is_dir():
        raise Error("invalid_storage", "已提交快照不存在")
    context = StorageContext.from_defaults(persist_dir=str(directory))
    structures = context.index_store.index_structs()
    if len(structures) != 1 or not isinstance(structures[0], IndexDict):
        raise Error("invalid_storage", "存储内容不是唯一的向量索引")
    return VectorStoreIndex(
        index_struct=structures[0], storage_context=context, embed_model=embedding
    )


def vector_dimensions(index: VectorStoreIndex, expected: int | None = None) -> int:
    """校验模型实际输出与已有知识的维度，不接受空、非有限或全零向量。"""
    store = index.storage_context.vector_store
    if not isinstance(store, SimpleVectorStore):
        raise Error("unsupported_storage", "M1 仅支持原生本地向量存储")
    dimensions = expected
    for vector in store.data.embedding_dict.values():
        check_vector(vector, dimensions)
        dimensions = len(vector)
    if dimensions is None:
        raise Error("invalid_embedding", "索引没有有效向量")
    return dimensions


def persist_index(
    root: Path,
    snapshot_id: str,
    index: VectorStoreIndex,
    *,
    lexical_profile: LexicalProfile = LEGACY_LEXICAL,
) -> None:
    """完整保存并刷新候选快照；数据库事务随后才允许切换引用。

    目录以新 ID 创建，从不覆盖旧快照。文件写入或刷新失败时保留旧数据库引用；
    进程中断可能留下孤立候选目录，不自动删除可能需要排查的原件或候选。
    """
    directory = snapshot_path(root, snapshot_id)
    directory.mkdir(parents=True, exist_ok=False)
    index.storage_context.persist(persist_dir=str(directory))
    persist_keyword_index(directory / KEYWORD_INDEX, index, lexical_profile=lexical_profile)
    for path in directory.iterdir():
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    if os.name == "posix":
        for path in [directory, directory.parent]:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
