"""真实原生解析的审校、离线资源与安全错误回归，不使用伪解析结果。"""

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table_data import RichTableCell, TableData
from docling_core.types.doc.items.text import TextItem
from inkparse import (
    FileSource,
    ParseError,
    Parser,
    ParseRequest,
    ParseResult,
    StructureOperation,
    effective_document,
    export_bundle,
    export_markdown,
    revise_structure,
    save_bundle,
)
from inkparse.models import ParsedPayload, fingerprint
from inkstore import StorageError
from inkstore.local import LocalAssetStore
from PIL import Image
from pydantic import TypeAdapter


def operations(value: str) -> list[StructureOperation]:
    """按实际对外JSON协议校验操作，不绕过类型和未知字段检查。"""
    return TypeAdapter(list[StructureOperation]).validate_json(value)


def parsed_html(tmp_path: Path) -> tuple[ParseResult, LocalAssetStore]:
    """解析含合法doctype、合并表格、真实内嵌PNG与外链的HTML。"""
    picture = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(picture, format="PNG")
    uri = base64.b64encode(picture.getvalue()).decode()
    source = tmp_path / "source.html"
    source.write_text(
        "<!DOCTYPE html><html><head><title>私有标题</title></head><body>"
        '<h1>库存清单</h1><p>旧备注</p><table><tr><th colspan="2">合并标题</th>'
        "</tr><tr><td>A</td><td>2</td></tr></table>"
        f'<img src="data:image/png;base64,{uri}" alt="源图片说明">'
        '<img src="https://invalid.example/not-fetched.png"></body></html>'
    )
    storage = LocalAssetStore.open(tmp_path / "assets")
    return Parser().parse(
        ParseRequest(source=FileSource(path=str(source))), assets=storage
    ), storage


def test_doctype_is_not_body_and_v3_remains_byte_protocol(tmp_path: Path) -> None:
    """合法文档声明和head不会成为正文，v3不会发出v4专属字段。"""
    result, _ = parsed_html(tmp_path)
    assert [item.text for item in result.document.texts] == ["库存清单", "旧备注"]
    assert result.schema_version == "inkparse.result.v3"
    assert "editor" not in json.loads(result.model_dump_json())
    assert ParseResult.model_validate_json(result.model_dump_json()).result_id == result.result_id


def test_excluding_direct_rich_cell_element_preserves_valid_projection(tmp_path: Path) -> None:
    """文档允许富格直接引用图片，排除及恢复该图片要保留可序列化结构。"""
    original, _ = parsed_html(tmp_path)
    document = DoclingDocument(name=original.document.name)
    table = document.add_table(data=TableData(num_rows=1, num_cols=1))
    picture = document.add_picture(parent=table)
    document.add_table_cell(
        table,
        RichTableCell(
            text="",
            start_row_offset_idx=0,
            end_row_offset_idx=1,
            start_col_offset_idx=0,
            end_col_offset_idx=1,
            ref=picture.get_ref(),
        ),
    )
    payload = ParsedPayload.model_validate(
        {
            **original.model_dump(exclude={"result_id"}),
            "document": document,
            "assets": [],
            "origins": [],
        }
    )
    result = ParseResult.model_validate({**payload.model_dump(), "result_id": fingerprint(payload)})
    excluded = revise_structure(
        result, operations('[{"kind":"exclude","element_ref":"#/pictures/0","excluded":true}]')
    )
    projected = effective_document(excluded)
    assert projected.validate_tree(projected.body)
    assert not isinstance(projected.tables[0].data.table_cells[0], RichTableCell)
    export_markdown(excluded, tmp_path / "excluded.md")
    restored = revise_structure(
        excluded, operations('[{"kind":"exclude","element_ref":"#/pictures/0","excluded":false}]')
    )
    assert isinstance(effective_document(restored).tables[0].data.table_cells[0], RichTableCell)


def test_structure_revision_preserves_machine_and_real_origins(tmp_path: Path) -> None:
    """一次真实修订覆盖六类操作，人工块无伪造位置，清空噪声不再受旧限制阻挡。"""
    original, _ = parsed_html(tmp_path)
    before = original.model_dump_json()
    revised = revise_structure(
        original,
        operations("""[
      {"kind":"text","element_ref":"#/texts/1","text":""},
      {"kind":"table_cell","element_ref":"#/tables/0","row":1,"column":1,"text":"8"},
      {"kind":"picture_caption","element_ref":"#/pictures/0","text":"系统架构"},
      {"kind":"picture_description","element_ref":"#/pictures/0","text":"人工说明：读写分离"},
      {"kind":"exclude","element_ref":"#/pictures/1","excluded":true},
      {"kind":"append_text","text":"人工补充","label":"section_header"}
    ]"""),
    )
    assert original.model_dump_json() == before
    assert revised.schema_version == "inkparse.result.v4"
    assert revised.previous_result_id == original.result_id and revised.revision == 2
    assert revised.source == original.source and revised.assets == original.assets
    assert revised.origins == original.origins
    assert revised.document.texts[1].orig == "旧备注"
    assert revised.document.tables[0].data.table_cells[-1].text == "8"
    assert revised.editor is not None
    assert revised.editor.excluded_refs == ["#/pictures/1"]
    assert len(revised.editor.manual_refs) == 3
    assert all(
        not revised.document.texts[int(ref.split("/")[-1])].prov
        for ref in revised.editor.manual_refs
    )
    assert ParseResult.model_validate_json(revised.model_dump_json()) == revised
    output = tmp_path / "result.md"
    export_markdown(revised, output)
    assert "人工补充" in output.read_text() and "读写分离" in output.read_text()


def test_exclusion_and_restore_only_affect_effective_tree(tmp_path: Path) -> None:
    """被排除内容不进入有效正文，恢复后原文、引用和次序保持。"""
    original, _ = parsed_html(tmp_path)
    revised = revise_structure(
        original, operations('[{"kind":"exclude","element_ref":"#/texts/1","excluded":true}]')
    )
    text = [
        item.text
        for item, _ in effective_document(revised).iterate_items()
        if isinstance(item, TextItem)
    ]
    assert "旧备注" not in text
    assert revised.document.texts[1].text == "旧备注"
    restored = revise_structure(
        revised, operations('[{"kind":"exclude","element_ref":"#/texts/1","excluded":false}]')
    )
    assert (
        effective_document(restored).export_to_markdown() == original.document.export_to_markdown()
    )
    assert restored.previous_result_id == revised.result_id


def test_merged_cell_and_failed_edit_are_atomic(tmp_path: Path) -> None:
    """合并格内部坐标和未知引用拒绝，已经执行的前序编辑不会污染输入。"""
    original, _ = parsed_html(tmp_path)
    before = original.model_dump_json()
    with pytest.raises(ParseError, match="左上角"):
        revise_structure(
            original,
            operations("""[
          {"kind":"text","element_ref":"#/texts/0","text":"不能泄漏到输入"},
          {"kind":"table_cell","element_ref":"#/tables/0","row":0,"column":1,"text":"坏地址"}
        ]"""),
        )
    assert original.model_dump_json() == before
    with pytest.raises(ParseError, match="不存在"):
        revise_structure(
            original, operations('[{"kind":"exclude","element_ref":"#/texts/999","excluded":true}]')
        )
    with pytest.raises(ParseError, match="没有实际"):
        revise_structure(
            original, operations('[{"kind":"text","element_ref":"#/texts/0","text":"库存清单"}]')
        )


def test_portable_bundle_contains_relative_images_and_optional_original(tmp_path: Path) -> None:
    """脱离服务仍有相对图片、原生JSON及校验清单，默认不夹带原文件。"""
    original, storage = parsed_html(tmp_path)
    revised = revise_structure(
        original, operations('[{"kind":"exclude","element_ref":"#/texts/1","excluded":true}]')
    )
    for include_original in (False, True):
        packet = export_bundle(revised, storage, include_original=include_original)
        with zipfile.ZipFile(io.BytesIO(packet)) as archive:
            names = archive.namelist()
            assert len([name for name in names if name.startswith("assets/")]) == 1
            assert (
                "original/source.html" in names
                if include_original
                else "original/source.html" not in names
            )
            markdown = archive.read("document.md").decode()
            assert "旧备注" not in markdown and "assets/" in markdown
            assert "/tmp/" not in markdown
            assert (
                ParseResult.model_validate_json(archive.read("document.json")).result_id
                == revised.result_id
            )
            manifest = json.loads(archive.read("manifest.json"))
            for identity, resource in manifest["resources"].items():
                assert hashlib.sha256(archive.read(resource["path"])).hexdigest() == identity
            for name, info in manifest["files"].items():
                assert hashlib.sha256(archive.read(name)).hexdigest() == info["sha256"]
            assert manifest["external_resources"][0]["resource_uri"].startswith(
                "https://invalid.example/"
            )
            assert all(not name.startswith("/") and ".." not in name.split("/") for name in names)


def test_parser_diagnostic_has_safe_position_and_recovery(tmp_path: Path) -> None:
    """真实坏JSON和XML给出位置与恢复建议，不回显私有正文，不鼓励直接重试。"""
    for filename, content, code in [
        ("bad.json", '{"private-secret": 1,\n}', "invalid_json"),
        ("bad.xml", "<root>\n<private-secret></root>", "invalid_xml"),
    ]:
        source = tmp_path / filename
        source.write_text(content)
        with pytest.raises(ParseError) as caught:
            Parser().parse(ParseRequest(source=FileSource(path=str(source))))
        detail = caught.value.diagnostic()
        assert detail.code == code and detail.location and detail.suggestion
        assert not detail.retryable
        assert (
            "private-secret" not in detail.model_dump_json()
            and str(tmp_path) not in detail.model_dump_json()
        )


def test_merged_cell_expansion_is_bounded_before_serialization(tmp_path: Path) -> None:
    """合法输入的大合并格不能让一次有界文字修改放大为巨量JSON占满API内存。"""
    source = tmp_path / "merged.html"
    source.write_text('<table><tr><td colspan="64">Original</td></tr></table>')
    original = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    from inkparse import TableCellOperation

    with pytest.raises(ParseError, match="预算"):
        revise_structure(
            original,
            [
                TableCellOperation(
                    kind="table_cell",
                    element_ref="#/tables/0",
                    row=0,
                    column=0,
                    text="A" * (1024 * 1024),
                )
            ],
        )
    assert original.document.tables[0].data.table_cells[0].text == "Original"


def test_file_bundle_failure_keeps_existing_delivery(tmp_path: Path) -> None:
    """逐资源写ZIP成功可读；中途缺图时旧文件不被半成品替换，临时文件清理。"""
    result, storage = parsed_html(tmp_path)
    destination = tmp_path / "delivery.zip"
    save_bundle(result, storage, destination, include_original=True)
    before = destination.read_bytes()
    with zipfile.ZipFile(destination) as archive:
        assert "original/source.html" in archive.namelist()
    (tmp_path / "assets" / "objects" / result.assets[0].asset_id).unlink()
    with pytest.raises(StorageError):
        save_bundle(result, storage, destination)
    assert destination.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "assets",
        "delivery.zip",
        "source.html",
    ]


def test_markdown_keeps_original_syntax_and_manual_heading(tmp_path: Path) -> None:
    """原Markdown语法不被转义，新增原生标题在独立导出及ZIP保持同一层级。"""
    source = tmp_path / "source.md"
    source.write_text("# 原标题\n\n**原格式**")
    storage = LocalAssetStore.open(tmp_path / "assets")
    original = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=storage)
    revised = revise_structure(
        original, operations('[{"kind":"append_text","label":"section_header","text":"补充标题"}]')
    )
    destination = tmp_path / "result.md"
    export_markdown(revised, destination)
    expected = "# 原标题\n\n**原格式**\n\n## 补充标题"
    assert destination.read_text() == expected
    with zipfile.ZipFile(io.BytesIO(export_bundle(revised, storage))) as archive:
        assert archive.read("document.md").decode() == expected


def test_corrupt_png_crc_is_safe_and_preserves_existing_bundle(tmp_path: Path) -> None:
    """真实IDAT坏CRC仍严格拒绝，安全诊断不回显内部异常，旧包和原始字节保留。"""
    valid_result, storage = parsed_html(tmp_path)
    destination = tmp_path / "delivery.zip"
    save_bundle(valid_result, storage, destination)
    before = destination.read_bytes()
    broken = bytearray(storage.read(valid_result.assets[0].asset_id))
    idat_offset = broken.index(b"IDAT")
    length = int.from_bytes(broken[idat_offset - 4 : idat_offset], "big")
    broken[idat_offset + 4 + length] ^= 1
    raw = bytes(broken)
    with pytest.raises(SyntaxError, match="checksum"), Image.open(io.BytesIO(raw)) as image:
        image.verify()
    encoded = base64.b64encode(raw).decode()
    source = tmp_path / "broken-image.html"
    source.write_text(
        f'<p>private-corrupt-image-marker</p><img src="data:image/png;base64,{encoded}">'
    )
    result = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=storage)
    original_result_id = result.result_id
    files_before = sorted(path.name for path in tmp_path.iterdir())
    with pytest.raises(ParseError) as caught:
        save_bundle(result, storage, destination, include_original=True)
    detail = caught.value.diagnostic()
    assert detail.code == "invalid_asset" and detail.location == "#/pictures/0"
    assert detail.suggestion and "修复" in detail.message and not detail.retryable
    assert "private-corrupt-image-marker" not in detail.model_dump_json()
    assert "IDAT" not in detail.model_dump_json() and "checksum" not in detail.model_dump_json()
    assert str(tmp_path) not in detail.model_dump_json()
    assert destination.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == files_before
    assert result.result_id == original_result_id and storage.read(result.assets[0].asset_id) == raw
    with pytest.raises(ParseError) as byte_output:
        export_bundle(result, storage)
    assert byte_output.value.code == "invalid_asset"
