"""验证人工文字修订的不可变版本、原文定位及入库来源。"""

from pathlib import Path

import pytest
from inkparse import FileSource, ParseError, Parser, ParseRequest, revise_result


def test_revision_keeps_original_and_creates_lineage(tmp_path: Path) -> None:
    """文字变化只进入新结果，原件摘要与原生定位继续指向原始资料。"""
    path = tmp_path / "manual.md"
    path.write_text("# 保修\nA100保修12个月")
    original = Parser().parse(ParseRequest(source=FileSource(path=str(path))))
    saved = original.model_dump_json()
    revised = revise_result(original, {"#/texts/0": "# 保修\nA100保修24个月"})
    assert revised.revision == 2
    assert revised.previous_result_id == original.result_id
    assert revised.result_id != original.result_id
    assert revised.source == original.source
    assert revised.document.texts[0].text.endswith("24个月")
    assert revised.document.texts[0].orig == original.document.texts[0].orig
    assert original.model_dump_json() == saved


@pytest.mark.parametrize(
    "changes", [{}, {"#/texts/99": "错位"}, {"#/texts/0": ""}, {"#/texts/0": "原文"}]
)
def test_invalid_revision_is_explicit(tmp_path: Path, changes: dict[str, str]) -> None:
    """空修订、未知引用、全空正文与无实际变化不会产生伪版本。"""
    path = tmp_path / "manual.txt"
    path.write_text("原文")
    result = Parser().parse(ParseRequest(source=FileSource(path=str(path))))
    with pytest.raises(ParseError) as caught:
        revise_result(result, changes)
    assert caught.value.code == "invalid_revision"
