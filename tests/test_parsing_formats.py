"""使用真实格式文件验证结构、资源与失败边界，不把可读取等同视觉解析质量。"""

import base64
import io
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from docling_core.types.doc.labels import DocItemLabel
from docx import Document
from inkparse import FileSource, ParseError, Parser, ParseRequest, ParseResult, load_stored_result
from inkstore.local import LocalAssetStore
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from inkrag.models import ProcessingConfig
from inkrag.search import result_nodes


def parse_file(path: Path, text: str) -> ParseResult:
    """将源文字实际写入对应格式，再调用正式解析入口。"""
    path.write_text(text, encoding="utf-8")
    return Parser().parse(ParseRequest(source=FileSource(path=str(path))))


def image_bytes() -> bytes:
    """生成真实两像素PNG，不依赖远程素材或假图片扩展名。"""
    target = io.BytesIO()
    Image.new("RGB", (2, 2), color="red").save(target, format="PNG")
    return target.getvalue()


def test_json_and_xml_keep_types_paths_and_empty_containers(tmp_path: Path) -> None:
    """同名字段、路径特殊字符、布尔、空值和XML命名空间均不会被拼平覆盖。"""
    parsed = parse_file(tmp_path / "data.json", '{"a/b":{"~x":[true,null,{}]},"b":[]}')
    assert [text.text for text in parsed.document.texts] == [
        "/a~1b/~0x/0 = true",
        "/a~1b/~0x/1 = null",
        "/a~1b/~0x/2 = {}",
        "/b = []",
    ]
    assert parsed.origins[0].locator == "json:/a~1b/~0x/0"
    decimal = parse_file(tmp_path / "decimal.json", '{"precise":1.12345678901234567890123456789}')
    assert decimal.document.texts[0].text == "/precise = 1.12345678901234567890123456789"
    xml = parse_file(
        tmp_path / "data.xml",
        '<root xmlns:x="urn:test"><x:p id="1">前<b>中</b>后</x:p><x:p/></root>',
    )
    text = "\n".join(item.text for item in xml.document.texts)
    assert "/root[1]/{urn:test}p[1]/@id = 1" in text
    assert "/root[1]/{urn:test}p[1]/b[1]/tail() = 后" in text
    assert "/root[1]/{urn:test}p[2] = <empty/>" in text
    assert ParseResult.model_validate_json(xml.model_dump_json()) == xml


def test_csv_native_table_survives_json_and_retrieval_bridge(tmp_path: Path) -> None:
    """CSV引号内逗号和换行保持为单元格，只有表格的文档也能进入检索桥。"""
    parsed = parse_file(tmp_path / "data.csv", '型号,说明,空值\nA100,"支持,多行\n保修12个月",\n')
    assert not parsed.document.texts and len(parsed.document.tables) == 1
    table = parsed.document.tables[0]
    assert table.data.num_rows == 2 and table.data.num_cols == 3
    assert table.data.table_cells[4].text == "支持,多行\n保修12个月"
    assert table.data.table_cells[5].text == ""
    nodes = result_nodes("tenant", "kb", "doc", parsed, ProcessingConfig())
    assert "保修12个月" in nodes[0].get_content()
    assert nodes[0].metadata["parse_origin"]["element_ref"] == "#/tables/0"


def test_html_semantic_table_embedded_image_and_no_external_fetch(tmp_path: Path) -> None:
    """静态HTML保留标题、合并单元格和图片字节，外链只作为来源记录。"""
    png = image_bytes()
    encoded = base64.b64encode(png).decode()
    source = tmp_path / "manual.html"
    source.write_text(
        "<html><head><script>秘密脚本</script></head><body><h1>产品手册</h1>"
        '<p>保修<strong>12个月</strong>。</p><table><tr><th rowspan="2">型号</th>'
        "<td>A100</td></tr><tr><td>B200</td></tr></table>"
        f'<img alt="架构图" src="data:image/png;base64,{encoded}">'
        '<img src="http://127.0.0.1:1/do-not-fetch"></body></html>',
        encoding="utf-8",
    )
    store = LocalAssetStore.open(tmp_path / "assets")
    parsed = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=store)
    assert parsed.document.texts[0].label == DocItemLabel.SECTION_HEADER
    assert "秘密脚本" not in parsed.model_dump_json()
    assert parsed.document.tables[0].data.table_cells[0].row_span == 2
    assert len(parsed.document.pictures) == 2 and len(parsed.assets) == 1
    assert store.read(parsed.assets[0].asset_id) == png
    assert any(item.resource_uri == "http://127.0.0.1:1/do-not-fetch" for item in parsed.origins)
    assert any(warning.code == "external_image_not_fetched" for warning in parsed.warnings)
    assert load_stored_result(store, parsed.result_id) == parsed


def test_docx_native_heading_merged_table_and_images(tmp_path: Path) -> None:
    """真实DOCX经过成熟库读取，表格纵横合并和嵌入图片保持，不编造页码。"""
    source = tmp_path / "manual.docx"
    document = Document()
    document.add_heading("企业手册", 1)
    document.add_paragraph("A100保修12个月。")
    table = document.add_table(rows=2, cols=3)
    table.cell(0, 0).text = "型号"
    table.cell(0, 0).merge(table.cell(1, 0))
    table.cell(0, 1).text = "合并"
    table.cell(0, 1).merge(table.cell(0, 2))
    table.cell(1, 1).text, table.cell(1, 2).text = "A100", "12个月"
    png = image_bytes()
    document.add_picture(io.BytesIO(png))
    document.save(str(source))
    store = LocalAssetStore.open(tmp_path / "assets")
    parsed = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=store)
    assert parsed.engine.name == "python-docx"
    assert parsed.document.texts[0].label == DocItemLabel.SECTION_HEADER
    assert parsed.document.tables[0].data.table_cells[0].row_span == 2
    assert parsed.document.tables[0].data.table_cells[1].col_span == 2
    assert not parsed.document.pages
    assert store.read(parsed.assets[0].asset_id) == png
    assert all(origin.locator.startswith("ooxml:word/document.xml/") for origin in parsed.origins)


def test_pptx_native_text_table_picture_notes_and_slide_coordinates(tmp_path: Path) -> None:
    """真实PPTX保留物理幻灯片、形状范围、合并表格、图片和演讲备注。"""
    source = tmp_path / "manual.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    shape.text_frame.text = "A100保修12个月。"
    table_shape = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(2))
    table = table_shape.table
    table.cell(0, 0).text = "合并标题"
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(1, 0).text, table.cell(1, 1).text = "A100", "12个月"
    png = image_bytes()
    slide.shapes.add_picture(io.BytesIO(png), Inches(6), Inches(1), width=Inches(1))
    notes = slide.notes_slide.notes_text_frame
    assert notes is not None
    notes.text = "内部讲解备注"
    presentation.save(str(source))
    store = LocalAssetStore.open(tmp_path / "assets")
    parsed = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=store)
    assert parsed.engine.name == "python-pptx" and set(parsed.document.pages) == {1}
    text = parsed.document.texts[0]
    assert text.prov[0].page_no == 1 and text.prov[0].bbox.l == 72
    assert parsed.document.tables[0].data.table_cells[0].col_span == 2
    assert any(item.text == "内部讲解备注" for item in parsed.document.texts)
    assert store.read(parsed.assets[0].asset_id) == png and parsed.assets[0].page == 1


@pytest.mark.parametrize(
    ("name", "text", "code"),
    [
        ("data.json", '{"x":1,"x":2}', "duplicate_json_key"),
        ("data.xml", '<!DOCTYPE r [<!ENTITY e "boom">]><r>&e;</r>', "unsafe_xml"),
        ("data.html", '<table><tr><td rowspan="2">越界</td></tr></table>', "unsupported_table"),
        ("data.doc", "旧格式", "unsupported_format"),
    ],
)
def test_unsafe_and_unsupported_formats_fail_explicitly(
    tmp_path: Path, name: str, text: str, code: str
) -> None:
    """重复键、实体、歧义表格及legacy DOC不能作为成功解析静默交付。"""
    with pytest.raises(ParseError) as caught:
        parse_file(tmp_path / name, text)
    assert caught.value.code == code


def test_office_zip_budget_rejects_compression_bomb(tmp_path: Path) -> None:
    """在成熟Office读取器之前限制解压预算，不把压缩后文件大小当内存上限。"""
    source = tmp_path / "large.docx"
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * (20 * 1024 * 1024 + 1))
    with pytest.raises(ParseError) as caught:
        Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    assert caught.value.code == "archive_budget_exceeded"


def test_v3_result_without_new_origins_still_validates(tmp_path: Path) -> None:
    """可选结构定位为空时保持v3摘要语义，既有文本交换包能够重读。"""
    parsed = parse_file(tmp_path / "old.txt", "原文")
    packet: dict[str, object] = json.loads(parsed.model_dump_json())
    packet.pop("origins")
    assert ParseResult.model_validate_json(json.dumps(packet)).result_id == parsed.result_id
