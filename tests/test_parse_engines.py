"""验证显式解析引擎、统一交换结构和真实格式，不将接口成功视为质量验收。"""

import base64
import io
import os
from pathlib import Path
from typing import Literal

import pytest
from docling_core.types.doc.items.table.table_data import RichTableCell
from inkparse import (
    ExcludeOperation,
    FileSource,
    ParseError,
    ParseOptions,
    Parser,
    ParseRequest,
    ParseResult,
    TableCellOperation,
    export_markdown,
    load_result,
    revise_structure,
    save_result,
)
from inkstore.local import LocalAssetStore
from llama_index.core.embeddings import MockEmbedding
from PIL import Image, ImageDraw

from inkrag import RAG

pytest.importorskip("docling")


def parse_docling(source: Path) -> ParseResult:
    """通过正式入口选择Docling，不直接调用第三方解析器绕过契约。"""
    return Parser().parse(
        ParseRequest(source=FileSource(path=str(source)), options=ParseOptions(engine="docling"))
    )


def test_builtin_default_keeps_legacy_digest(tmp_path: Path) -> None:
    """默认内置选择不增加旧包字段；移除省略的默认引擎后旧摘要仍可重读。"""
    source = tmp_path / "source.txt"
    source.write_text("内置解析保持默认", encoding="utf-8")
    request = ParseRequest(source=FileSource(path=str(source)))
    default = Parser().parse(request)
    explicit = Parser().parse(
        request.model_copy(update={"options": ParseOptions(engine="builtin")})
    )
    assert default.result_id == explicit.result_id
    assert default.options.model_dump() == {"format": "auto", "max_bytes": 20 * 1024 * 1024}
    restored = ParseResult.model_validate_json(default.model_dump_json())
    assert restored.options.engine == "builtin"
    assert restored.result_id == default.result_id


def test_markdown_engines_preserve_distinct_content_contracts(tmp_path: Path) -> None:
    """内置保留原语法，Docling产生标题表格，交换重读及Markdown导出均保留正文。"""
    source = tmp_path / "policy.md"
    text = "# 售后政策\n\n| 商品 | 期限 |\n| --- | --- |\n| 耳机 | 30天 |\n"
    source.write_text(text, encoding="utf-8")
    builtin = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    docling = parse_docling(source)
    assert builtin.document.texts[0].text == text
    assert not builtin.document.tables
    assert docling.engine.name == "docling"
    assert docling.options.engine == "docling"
    assert len(docling.document.tables) == 1
    assert any(item.text == "售后政策" for item in docling.document.texts)
    assert builtin.source.sha256 == docling.source.sha256
    assert builtin.result_id != docling.result_id
    packet = tmp_path / "result.json"
    save_result(docling, packet)
    restored = load_result(packet)
    assert restored.result_id == docling.result_id
    export_markdown(restored, tmp_path / "result.md")
    rendered = (tmp_path / "result.md").read_text()
    assert "售后政策" in rendered and "耳机" in rendered and "30天" in rendered
    assert str(tmp_path) not in restored.model_dump_json()


@pytest.mark.parametrize(
    ("suffix", "format_name", "display_name"),
    [(".data", "markdown", "手册"), (".md", "auto", "手册.html")],
)
def test_docling_uses_selected_format_with_independent_display_name(
    tmp_path: Path, suffix: str, format_name: Literal["auto", "markdown"], display_name: str
) -> None:
    """实际转换遵循选定格式，并把自定义展示名保存进结果。"""
    source = tmp_path / ("source" + suffix)
    source.write_text("# Checkout policy\n\nWarranty applies.\n", encoding="utf-8")
    result = Parser().parse(
        ParseRequest(
            source=FileSource(path=str(source), name=display_name),
            options=ParseOptions(engine="docling", format=format_name),
        )
    )
    assert result.source.format == "markdown"
    assert result.document.name == result.source.name == display_name
    assert [item.text for item in result.document.texts] == [
        "Checkout policy",
        "Warranty applies.",
    ]


def test_docling_rich_cell_edit_reaches_export_and_sdk_retrieval(tmp_path: Path) -> None:
    """富文本格只经原表进入索引，审校后的值跨交换包与检索可见。"""
    source = tmp_path / "policy.html"
    source.write_text(
        "<h1>Policy</h1><table><caption>Private table note</caption>"
        "<tr><th>Product</th><th>Coverage</th></tr>"
        "<tr><td>Sensor</td><td><p>obsolete</p><p>oldvalue</p></td></tr></table>",
        encoding="utf-8",
    )
    original = parse_docling(source)
    table = original.document.tables[0]
    assert any(isinstance(cell, RichTableCell) for cell in table.data.table_cells)
    caption = table.captions[0].cref
    revised = revise_structure(
        original,
        [
            TableCellOperation(
                kind="table_cell", element_ref=table.self_ref, row=1, column=1, text="approved"
            ),
            ExcludeOperation(kind="exclude", element_ref=caption, excluded=True),
        ],
    )
    packet = tmp_path / "policy.json"
    markdown = tmp_path / "policy.md"
    save_result(revised, packet)
    export_markdown(load_result(packet), markdown)
    rendered = markdown.read_text(encoding="utf-8")
    assert "approved" in rendered and "oldvalue" not in rendered
    assert "Private table note" not in rendered
    assert "oldvalue" in original.document.export_to_markdown()
    assert revised.origins == original.origins
    embedding = MockEmbedding(model_name="rich-cell-boundary-test", embed_dim=4)
    with RAG.open(tmp_path / "knowledge", embedding=embedding) as rag:
        rag.ingest(source, document_id="policy", parse_options=ParseOptions(engine="docling"))
        hits = rag.retrieve("oldvalue", mode="keyword").hits
        assert len(hits) == 1
        assert "Coverage: obsolete oldvalue" in hits[0].get_content()
        assert hits[0].metadata["parse_origin"]["element_ref"] == table.self_ref
        rag.ingest_parsed(packet, document_id="policy")
        assert not rag.retrieve("oldvalue", mode="keyword").hits
        hits = rag.retrieve("approved", mode="keyword").hits
        assert len(hits) == 1
        assert "Coverage: approved" in hits[0].get_content()
        assert "Private table note" not in hits[0].get_content()
        assert hits[0].metadata["parse_origin"]["element_ref"] == table.self_ref
        assert hits[0].metadata["parse_origin"]["element_locator"] == "docling:#/tables/0"


def test_xlsx_requires_explicit_docling_engine(tmp_path: Path) -> None:
    """真实Excel可由Docling转换，内置引擎不能自动切换或误报支持。"""
    from openpyxl import Workbook

    source = tmp_path / "policy.xlsx"
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["商品", "期限"])
    sheet.append(["耳机", "30天"])
    book.save(source)
    with pytest.raises(ParseError) as caught:
        Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    assert caught.value.code == "unsupported_format"
    import zipfile

    from inkparse import export_bundle
    from inkstore.local import LocalAssetStore

    storage = LocalAssetStore.open(tmp_path / "assets")
    parsed = Parser().parse(
        ParseRequest(source=FileSource(path=str(source)), options=ParseOptions(engine="docling")),
        assets=storage,
    )
    assert parsed.source.format == "xlsx"
    assert parsed.document.tables
    assert "耳机" in parsed.document.export_to_markdown()

    with zipfile.ZipFile(
        io.BytesIO(export_bundle(parsed, storage, include_original=True))
    ) as archive:
        assert archive.read("original/source.xlsx") == source.read_bytes()


@pytest.mark.parametrize(
    ("extension", "text", "expected"),
    [
        ("txt", "# 按字面读取", "# 按字面读取"),
        ("json", '{"x":1.1234567890123456789}', "/x = 1.1234567890123456789"),
        ("xml", '<r a="1">正文</r>', "/r[1]/@a = 1"),
    ],
)
def test_docling_generic_inputs_reject_unsupported_format(
    tmp_path: Path, extension: str, text: str, expected: str
) -> None:
    """Docling未装配通用格式时明确拒绝，不能偷偷切回内置工具。"""
    assert expected
    source = tmp_path / ("source." + extension)
    source.write_text(text, encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        parse_docling(source)
    assert caught.value.code == "unsupported_format"


def test_docling_html_assets_and_external_reference(tmp_path: Path) -> None:
    """内嵌PNG保持字节身份，无法连接的外链仅引用，输出没有本地路径或内嵌字节。"""
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color="red").save(buffer, format="PNG")
    png = buffer.getvalue()
    encoded = base64.b64encode(png).decode()
    source = tmp_path / "manual.html"
    source.write_text(
        "<html><body><h1>说明</h1><p>正文</p>"
        f'<img src="data:image/png;base64,{encoded}">'
        '<img src="http://127.0.0.1:1/must-not-fetch.png"></body></html>',
        encoding="utf-8",
    )
    store = LocalAssetStore.open(tmp_path / "assets")
    parsed = Parser().parse(
        ParseRequest(source=FileSource(path=str(source)), options=ParseOptions(engine="docling")),
        assets=store,
    )
    assert len(parsed.document.pictures) == 2
    assert len(parsed.assets) == 1
    assert store.read(parsed.assets[0].asset_id) == png
    assert any(
        origin.resource_uri == "http://127.0.0.1:1/must-not-fetch.png" for origin in parsed.origins
    )
    assert any(note.code == "external_image_not_fetched" for note in parsed.warnings)
    assert all(picture.image is None for picture in parsed.document.pictures)
    packet = parsed.model_dump_json()
    assert str(tmp_path) not in packet
    assert encoded not in packet
    assert ParseResult.model_validate_json(packet).result_id == parsed.result_id


def test_docling_missing_models_fails_without_builtin_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内置可读的真实文本PDF，在Docling缺模型时必须明确失败而不是返回内置结果。"""
    source = Path(__file__).parents[1] / "assets/samples/sensor.pdf"
    builtin = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    assert builtin.document.texts
    monkeypatch.delenv("INKPARSE_MODELS_PATH", raising=False)
    with pytest.raises(ParseError) as caught:
        parse_docling(source)
    assert caught.value.code == "models_unavailable"
    assert str(tmp_path) not in caught.value.diagnostic().model_dump_json()


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("INKPARSE_MODELS_PATH"), reason="需要显式本地Docling模型工件"
)
def test_docling_real_pdf_with_prepared_models() -> None:
    """真实模型处理固定文本PDF并保留物理页来源；不据此宣称OCR或企业质量达标。"""
    source = Path(__file__).parents[1] / "assets/samples/sensor.pdf"
    parsed = parse_docling(source)
    assert parsed.engine.name == "docling"
    assert parsed.document.pages
    assert "E102" in parsed.document.export_to_markdown()
    assert any(item.prov for item in parsed.document.texts)


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("INKPARSE_MODELS_PATH"), reason="需要显式预置Docling模型")
def test_docling_image_bundle_preserves_original(tmp_path: Path) -> None:
    """实际图片OCR后含原件交付使用真实固定扩展，不信任上传文件名生成路径。"""
    import zipfile

    from inkparse import export_bundle
    from inkstore.local import LocalAssetStore
    from PIL import Image, ImageDraw

    source = tmp_path / "policy.png"
    image = Image.new("RGB", (600, 150), "white")
    ImageDraw.Draw(image).text((30, 40), "Warranty 30 days", fill="black", font_size=32)
    image.save(source)
    storage = LocalAssetStore.open(tmp_path / "assets")
    parsed = Parser().parse(
        ParseRequest(source=FileSource(path=str(source)), options=ParseOptions(engine="docling")),
        assets=storage,
    )
    assert parsed.source.format == "image"
    assert "30" in parsed.document.export_to_markdown()
    with zipfile.ZipFile(
        io.BytesIO(export_bundle(parsed, storage, include_original=True))
    ) as archive:
        assert archive.read("original/source.png") == source.read_bytes()


def test_docling_xlsx_archive_budget_before_conversion(tmp_path: Path) -> None:
    """新增Excel输入同样先校验解压预算，SDK也不把压缩体积当展开体积。"""
    import zipfile

    source = tmp_path / "oversized.xlsx"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"x" * (20 * 1024 * 1024 + 1))
    with pytest.raises(ParseError) as caught:
        parse_docling(source)
    assert caught.value.code == "archive_budget_exceeded"


def test_builtin_rejects_explicit_ocr(tmp_path: Path) -> None:
    """不能把不支持识字的内置工具标记为 OCR 已开启。"""
    source = tmp_path / "source.txt"
    source.write_text("正文")
    with pytest.raises(ParseError) as caught:
        Parser().parse(
            ParseRequest(
                source=FileSource(path=str(source)), options=ParseOptions(ocr_enabled=True)
            )
        )
    assert caught.value.code == "unsupported_option"


@pytest.mark.parametrize(
    "options",
    [
        ParseOptions(table_structure=True),
        ParseOptions(engine="docling", ocr_mode="full_page"),
        ParseOptions(engine="docling", table_mode="fast"),
    ],
)
def test_advanced_options_reject_unsupported_format(tmp_path: Path, options: ParseOptions) -> None:
    """显式参数不能被不适用的格式或引擎静默忽略。"""
    source = tmp_path / "source.md"
    source.write_text("正文")
    with pytest.raises(ParseError, match="仅适用于") as caught:
        Parser().parse(ParseRequest(source=FileSource(path=str(source)), options=options))
    assert caught.value.code == "unsupported_option"


@pytest.mark.parametrize(
    "options",
    [
        ParseOptions(engine="docling", ocr_enabled=False, ocr_mode="full_page"),
        ParseOptions(engine="docling", table_structure=False, table_mode="fast"),
    ],
)
def test_disabled_feature_rejects_dependent_options(tmp_path: Path, options: ParseOptions) -> None:
    """关闭父开关后不接受无效的子参数，且校验先于模型读取。"""
    source = tmp_path / "source.png"
    Image.new("RGB", (30, 30), "white").save(source)
    with pytest.raises(ParseError, match="关闭时") as caught:
        Parser().parse(ParseRequest(source=FileSource(path=str(source)), options=options))
    assert caught.value.code == "unsupported_option"


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("INKPARSE_MODELS_PATH"), reason="需要显式预置Docling模型")
def test_full_page_ocr_without_table_model(tmp_path: Path) -> None:
    """真实离线转换以全页OCR读取文字，关闭表格结构仍保留配置和准确警告。"""
    from PIL import ImageDraw

    source = tmp_path / "full-page.png"
    image = Image.new("RGB", (600, 150), "white")
    ImageDraw.Draw(image).text((30, 40), "Warranty 30 days", fill="black", font_size=32)
    image.save(source)
    result = Parser().parse(
        ParseRequest(
            source=FileSource(path=str(source)),
            options=ParseOptions(
                engine="docling", ocr_enabled=True, ocr_mode="full_page", table_structure=False
            ),
        )
    )
    assert "30" in result.document.export_to_markdown()
    assert result.options.ocr_mode == "full_page"
    assert result.options.table_structure is False
    assert any("表格结构识别已关闭" in warning.message for warning in result.warnings)


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("INKPARSE_MODELS_PATH"), reason="需要显式预置Docling模型")
def test_docling_ocr_switch_changes_image_extraction(tmp_path: Path) -> None:
    """同一真实图片仅改变 OCR 开关；关闭时不得伪造可检索文字或成功空包。"""
    from PIL import ImageDraw

    source = tmp_path / "ocr-switch.png"
    image = Image.new("RGB", (600, 150), "white")
    ImageDraw.Draw(image).text((30, 40), "Warranty 30 days", fill="black", font_size=32)
    image.save(source)
    request = ParseRequest(
        source=FileSource(path=str(source)),
        options=ParseOptions(engine="docling", ocr_enabled=True),
    )
    enabled = Parser().parse(request)
    assert "30" in enabled.document.export_to_markdown()
    assert enabled.options.ocr_enabled is True
    with pytest.raises(ParseError) as caught:
        Parser().parse(
            request.model_copy(
                update={"options": ParseOptions(engine="docling", ocr_enabled=False)}
            )
        )
    assert caught.value.code == "empty_document"


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("INKPARSE_MODELS_PATH"), reason="需要显式预置Docling模型")
def test_ocr_off_real_table_image_is_empty_not_over_budget(tmp_path: Path) -> None:
    """关闭OCR的栅格表格只剩零维检测框，应报告无内容。"""
    source = tmp_path / "table.png"
    image = Image.new("RGB", (960, 480), "white")
    draw = ImageDraw.Draw(image)
    columns = (45, 470, 915)
    rows = (48, 144, 240, 336, 432)
    for x in columns:
        draw.line((x, rows[0], x, rows[-1]), fill="black", width=4)
    for y in rows:
        draw.line((columns[0], y, columns[-1], y), fill="black", width=4)
    values = (
        ("Product", "Warranty"),
        ("Sensor", "30 days"),
        ("Probe", "90 days"),
        ("Meter", "120 days"),
    )
    for y, row in zip(rows[:-1], values, strict=True):
        for x, value in zip(columns[:-1], row, strict=True):
            draw.text((x + 25, y + 28), value, fill="black", font_size=36)
    image.save(source)
    document = parse_docling(source).document
    assert len(document.tables) == 1
    assert document.tables[0].data.num_rows == 4
    assert document.tables[0].data.num_cols == 2
    assert "Warranty" in document.export_to_markdown()
    with pytest.raises(ParseError) as caught:
        Parser().parse(
            ParseRequest(
                source=FileSource(path=str(source)),
                options=ParseOptions(engine="docling", ocr_enabled=False),
            )
        )
    assert caught.value.code == "empty_document"
