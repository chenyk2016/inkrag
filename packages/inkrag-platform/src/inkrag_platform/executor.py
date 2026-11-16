"""受限解析子进程入口；环境中没有PG、队列、对象存储或身份凭据。"""

import errno
import hashlib
import os
import resource
import sys
from pathlib import Path

from inkparse import FileSource, ParseError, ParseOptions, Parser, ParseRequest
from inkstore import AssetInfo, StorageError, StoredRecord


class ScratchStorage:
    """临时产物传输目录，不是本地业务存储或持久化降级后端。"""

    def __init__(self, root: Path) -> None:
        """创建受限工作目录并记录64MiB/256个产物预算。"""
        self.root = root
        self.used = 0
        (root / "objects").mkdir()
        (root / "records").mkdir()

    def _path(self, group: str, identity: str) -> Path:
        """仅允许SHA256命名，杜绝输入路径访问宿主文件。"""
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise StorageError("invalid_asset", "资产身份不是SHA256")
        return self.root / group / identity

    def put_bytes(self, data: bytes) -> AssetInfo:
        """向有界临时目录写摘要资源，由父进程重新校验并上传。"""
        if not data or len(data) > 20 * 1024 * 1024:
            raise StorageError("invalid_asset", "资源超过20MiB或为空")
        identity = hashlib.sha256(data).hexdigest()
        target = self._path("objects", identity)
        if not target.exists():
            self.used += len(data)
            if self.used > 64 * 1024 * 1024 or len(list(target.parent.iterdir())) >= 256:
                raise StorageError("output_limit", "解析临时产物超过预算")
            target.write_bytes(data)
        return AssetInfo(asset_id=identity, size_bytes=len(data))

    def read(self, asset_id: str) -> bytes:
        """只读取已输出的完整资源。"""
        data = self._path("objects", asset_id).read_bytes()
        if hashlib.sha256(data).hexdigest() != asset_id:
            raise StorageError("corrupt_asset", "临时资源完整性失败")
        return data

    def get(self, asset_id: str) -> AssetInfo:
        """校验资源后返回元信息。"""
        return AssetInfo(asset_id=asset_id, size_bytes=len(self.read(asset_id)))

    def save_record(self, record: StoredRecord) -> None:
        """保存传输记录，业务提交仍由父进程S3和PG控制。"""
        for identity in {record.source_asset_id, *(r.asset_id for r in record.references)}:
            self.get(identity)
        self._path("records", record.record_id).write_text(record.model_dump_json())

    def load_record(self, record_id: str) -> StoredRecord:
        """读取经过契约校验的传输记录。"""
        return StoredRecord.model_validate_json(self._path("records", record_id).read_bytes())


def restrict_filesystem(root: Path) -> None:
    """只放行本次产物目录和只读运行库；其他scratch、/proc与凭据路径默认拒绝。

    使用内核Landlock而非字符串路径过滤，符号链接也不能绕过。要求ABI>=3，
    不可用时拒绝解析；不自动降级。应用前必须单线程，之后seccomp只允许线程型clone，仍禁止创建子进程。
    """
    if sys.platform != "linux":
        raise ParseError("sandbox_unavailable", "文件隔离需要Linux Landlock")
    from py_landlock import AccessFs, Landlock

    if len(list(Path("/proc/self/task").iterdir())) != 1:
        raise ParseError("sandbox_unavailable", "解析限制必须在单线程入口应用")
    policy = Landlock(strict=True)
    if policy.abi_version < 3:
        raise ParseError("sandbox_unavailable", "解析文件隔离需要Landlock ABI 3或更高")
    readable = {
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        Path("/usr/lib").resolve(),
        Path("/lib").resolve(),
        Path("/usr/share/fonts"),
        Path("/etc/fonts"),
        Path("/etc/ld.so.cache"),
        Path("/etc/localtime").resolve(),
        # 数值运行库需要随机源和CPU能力探测，明确只读放行；不开放其余proc/sys。
        Path("/dev/urandom"),
        Path("/dev/null"),
        Path("/proc/cpuinfo"),
        Path("/sys/devices/system/cpu"),
    }
    model_root = os.environ.get("INKPARSE_MODELS_PATH")
    if model_root:
        readable.add(Path(model_root).resolve())
    for path in sorted(readable):
        if path.is_dir():
            policy.allow_read(path)
        elif path.exists():
            # 随机设备是字符设备，不能用is_file漏掉其显式只读授权。
            policy.add_path_rule(
                path,
                access=AccessFs.READ_FILE | AccessFs.WRITE_FILE
                if path == Path("/dev/null")
                else AccessFs.READ_FILE,
            )
    writable = (
        AccessFs.READ_FILE
        | AccessFs.READ_DIR
        | AccessFs.WRITE_FILE
        | AccessFs.TRUNCATE
        | AccessFs.MAKE_REG
        | AccessFs.MAKE_DIR
        | AccessFs.REMOVE_FILE
        | AccessFs.REMOVE_DIR
    )
    policy.add_path_rule(root.resolve(strict=True), access=writable)
    policy.apply()


def restrict(memory_mb: int, seconds: int, strict: bool, root: Path) -> None:
    """限制资源，Linux强制Landlock+seccomp；macOS仅允许显式开发且不假称隔离。

    本函数只在专属解析子进程调用，限制不可撤销。AS硬限制是Linux生产契约，
    macOS不支持可靠的RLIMIT_AS设置，因此开发模式仍有CPU/文件和宿主墙钟上限。
    """
    if strict and sys.platform != "linux":
        raise ParseError("sandbox_unavailable", "需要Linux进程与文件隔离")
    memory = memory_mb * 1024 * 1024
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform == "linux":
        import pyseccomp as seccomp

        restrict_filesystem(root)
        filter_ = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
        for name in (
            "socket",
            "socketpair",
            "connect",
            "bind",
            "listen",
            "accept",
            "accept4",
            "fork",
            "vfork",
            "execve",
            "execveat",
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "mount",
            "umount2",
            "unshare",
            "setns",
            "open_by_handle_at",
            "bpf",
            "keyctl",
            "perf_event_open",
            "kill",
            "tkill",
            "tgkill",
            "pidfd_send_signal",
            "pidfd_getfd",
            "io_uring_setup",
            "io_uring_enter",
            "io_uring_register",
        ):
            filter_.add_rule(seccomp.ERRNO(errno.EPERM), name)
        # clone3参数位于指针结构中，无法逐标志过滤；要求libc回退可检查的clone。
        filter_.add_rule(seccomp.ERRNO(errno.ENOSYS), "clone3")
        filter_.add_rule(
            seccomp.ERRNO(errno.EPERM), "clone", seccomp.Arg(0, seccomp.MASKED_EQ, 0x00010000, 0)
        )
        filter_.load()
    elif strict:
        raise RuntimeError("需要Linux seccomp")


def main() -> int:
    """固定入口只接收工作目录和资源预算，不执行来自资料的指令。"""
    root = Path(sys.argv[1])
    try:
        restrict(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "strict", root)
        sources = list(root.glob("source.*"))
        if len(sources) != 1:
            raise ParseError("invalid_source", "缺少唯一原始文件")
        result = Parser().parse(
            ParseRequest(
                source=FileSource(path=str(sources[0]), name=(root / "name").read_text()),
                options=ParseOptions.model_validate_json((root / "options.json").read_bytes())
                if (root / "options.json").is_file()
                else ParseOptions(),
            ),
            assets=ScratchStorage(root),
        )
        (root / "result.json").write_text(result.model_dump_json())
        return 0
    except (ParseError, StorageError) as exc:
        (root / "error").write_text(exc.code)
        if isinstance(exc, ParseError):
            (root / "error.json").write_text(exc.diagnostic().model_dump_json())
    except MemoryError:
        (root / "error").write_text("parse_memory_limit")
    except Exception:
        (root / "error").write_text("sandbox_failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
