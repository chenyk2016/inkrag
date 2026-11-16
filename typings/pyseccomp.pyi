"""Linux seccomp实际使用的窄类型声明，过滤由原生libseccomp执行。"""

ALLOW: int
MASKED_EQ: int

def ERRNO(x: int) -> int: ...

class Arg:
    """用掩码和比较值匹配指定的系统调用参数。"""

    def __init__(self, arg: int, op: int, data_a: int, data_b: int = 0) -> None: ...

class SyscallFilter:
    """系统调用过滤器。"""

    def __init__(self, defaction: int) -> None: ...
    def add_rule(self, action: int, syscall: int | str, *args: Arg) -> None: ...
    def load(self) -> None: ...
