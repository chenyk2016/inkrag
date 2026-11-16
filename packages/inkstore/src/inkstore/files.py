"""本地适配器使用的原子文件写入，不携带业务结果模型。"""

import os
import tempfile
from pathlib import Path

from .contracts import StorageError


def atomic_write(path: Path, data: bytes) -> None:
    """在目标目录临时写入并fsync后替换；清理本次临时文件，不创建父目录。"""
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise StorageError("write_failed", "结果保存失败，原文件保留") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
