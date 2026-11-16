"""从已提交的 Git 树生成公开源码包，显式保留可再分发的示例和文档。"""

import argparse
import io
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "docs/public-source-files.txt"
REQUIRED = {
    "README.md",
    "LICENSE",
    "NOTICE",
    "pyproject.toml",
    "docs/learn/index.html",
    MANIFEST,
    "scripts/export-public-source.py",
}
SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"github_pat_[a-zA-Z0-9_]{30,}|gh[pousr]_[a-zA-Z0-9]{30,}|"
    rb"AKIA[0-9A-Z]{16}|sk-[a-zA-Z0-9_-]{36,}"
)
ALLOWED_LINKS = {
    "Dockerfile": "Dockerfile.platform",
    "compose.yaml": "packages/inkrag-platform/src/inkrag_platform/resources/compose.yaml",
}


def git(*args: str) -> bytes:
    """使用项目 Git 仓库，只读取传入的已提交版本。"""
    return subprocess.check_output(["git", *args], cwd=ROOT)


def reviewed_paths(content: str) -> list[str]:
    """只接受逐路径审定的清单，目录和通配符都不能自动加入发行。"""
    paths = [line for line in content.splitlines() if line and not line.startswith("#")]
    if len(paths) != len(set(paths)) or not REQUIRED <= set(paths):
        raise ValueError("公开文件清单有重复或缺少发行入口")
    for name in paths:
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or str(path) != name
            or ".." in path.parts
            or any(char in name for char in "*?[]\\")
            or name.startswith(":")
            or not name.isprintable()
            or {".git", ".inkrag", "node_modules", "__pycache__"} & set(path.parts)
            or (path.name.startswith(".env") and name != ".env.example")
            or path.suffix in {".pem", ".key", ".p12", ".sqlite3"}
        ):
            raise ValueError(f"公开文件清单含不安全路径：{name}")
    return paths


def export(revision: str, output: Path) -> int:
    """校验选中内容后原子写入 tar，保留 Git 文件模式及内部链接。"""
    commit = (
        git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
    )
    tracked = set(git("ls-tree", "-r", "--name-only", "-z", commit).decode().split("\0"))
    selected = reviewed_paths(git("show", f"{commit}:{MANIFEST}").decode())
    missing = set(selected) - tracked
    if missing:
        raise ValueError("公开文件清单的路径不在该提交：" + ", ".join(sorted(missing)))
    data = git("archive", "--format=tar", "--prefix=inkrag/", commit, "--", *selected)
    archived: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            relative = member.name.removeprefix("inkrag/")
            if member.isdir():
                continue
            if relative not in selected or relative in archived:
                raise ValueError(f"归档含未审定或重复的路径：{relative}")
            archived.add(relative)
            if member.issym() and ALLOWED_LINKS.get(relative) == member.linkname:
                continue
            if not member.isfile():
                raise ValueError(f"归档含非预期文件类型：{relative}")
            stream = archive.extractfile(member)
            if stream is None or SECRET.search(stream.read()):
                raise ValueError(f"归档含疑似凭据或读取失败：{relative}")
    if archived != set(selected):
        raise ValueError("归档与公开文件清单不一致")
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
    os.replace(temporary, output)
    return len(selected)


def main() -> int:
    """接收已提交版本和输出文件，失败时报告文件名而非匹配内容。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        count = export(args.ref, args.output)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"无法导出公开源码包：{exc}", file=sys.stderr)
        return 1
    print(f"已导出 {count} 个受控路径到 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
