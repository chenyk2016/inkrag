"""Linux专用已锁定绑定的最小类型面，便于macOS静态验证；运行仍使用原包。"""

from enum import IntFlag
from os import PathLike
from typing import Self

class AccessFs(IntFlag):
    """仅声明本产品实际使用的内核文件访问位。"""

    WRITE_FILE = 1 << 1
    READ_FILE = 1 << 2
    READ_DIR = 1 << 3
    REMOVE_DIR = 1 << 4
    REMOVE_FILE = 1 << 5
    MAKE_DIR = 1 << 7
    MAKE_REG = 1 << 8
    TRUNCATE = 1 << 14

class Landlock:
    """上游规则构造器，最小内核版本由调用方明确核对。"""

    def __init__(self, *, strict: bool = ...) -> None: ...
    @property
    def abi_version(self) -> int: ...
    def allow_read(self, *paths: str | PathLike[str]) -> Self: ...
    def add_path_rule(self, *paths: str | PathLike[str], access: AccessFs) -> Self: ...
    def apply(self) -> None: ...
