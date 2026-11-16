"""生成只含教程的 GitHub Pages 目录，并将源码链接固定到该次构建的提交。"""

from __future__ import annotations

import argparse
import os
import re
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
TUTORIAL = ROOT / "docs/learn/index.html"
_ATTRIBUTE = re.compile(r"\s+([^\s=/>]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+)))?")


def github_link(href: str, source: Path, repository: str, sha: str) -> str | None:
    """从教程位置解析本地文件，并保留目标片段及查询参数。"""
    url = urlsplit(href)
    if url.scheme or url.netloc or not url.path:
        return None
    path = unquote(url.path)
    target = (
        (ROOT / path.lstrip("/")) if path.startswith("/") else (source.parent / path)
    ).resolve()
    if not target.is_relative_to(ROOT) or not target.is_file():
        raise ValueError(f"教程链接未指向仓库中的文件：{href}")
    relative = quote(target.relative_to(ROOT).as_posix())
    return urlunsplit(
        ("https", "github.com", f"/{repository}/blob/{sha}/{relative}", url.query, url.fragment)
    )


class TutorialLinks(HTMLParser):
    """只替换 HTML 标签的 href，保留教程原有脚本、样式和其他源码。"""

    def __init__(self, html: str, source: Path, repository: str, sha: str) -> None:
        """记录原文位置，避免重新序列化 HTML。"""
        super().__init__(convert_charrefs=False)
        self.source, self.repository, self.sha = source, repository, sha
        self.offsets = [0, *(match.end() for match in re.finditer("\n", html))]
        self.changes: list[tuple[int, int, str]] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """仅改写解析器确认存在的 href 属性，忽略脚本和注释中的同名文字。"""
        if not any(key == "href" and value is not None for key, value in attrs):
            return
        raw_tag = self.get_starttag_text()
        if raw_tag is None:
            return
        line, column = self.getpos()
        offset = self.offsets[line - 1] + column
        for match in _ATTRIBUTE.finditer(raw_tag):
            if match[1].lower() != "href":
                continue
            for group in (2, 3, 4):
                if (raw_value := match[group]) is None:
                    continue
                rewritten = github_link(unescape(raw_value), self.source, self.repository, self.sha)
                if rewritten is not None:
                    start, end = match.span(group)
                    value = escape(rewritten, quote=True)
                    if group == 4:
                        value = f'"{value}"'
                    self.changes.append((offset + start, offset + end, value))
                break


def build(output: Path, repository: str, sha: str) -> int:
    """写入教程首页，遇到不存在的目标或混入其他制品时停止构建。"""
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("PAGES_REPOSITORY 应为 owner/repo")
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("PAGES_SHA 应为 40 位提交 SHA")
    html = TUTORIAL.read_text(encoding="utf-8")
    changes = TutorialLinks(html, TUTORIAL, repository, sha).changes
    for start, end, value in reversed(changes):
        html = html[:start] + value + html[end:]
    output.mkdir(parents=True, exist_ok=True)
    target = output / "index.html"
    if (
        target.is_symlink()
        or target.resolve() == TUTORIAL
        or any(item != target for item in output.iterdir())
    ):
        raise ValueError("教程输出目录必须独立，仅允许生成 index.html")
    target.write_text(html, encoding="utf-8")
    return len(changes)


def main() -> int:
    """从环境读取仓库与提交，输出目录由命令行明确提供。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        count = build(
            args.output, os.environ.get("PAGES_REPOSITORY", ""), os.environ.get("PAGES_SHA", "")
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"已生成教程首页，固定 {count} 个仓库链接。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
