"""验证结构证据在本地 SDK 与生产切分中的语义和来源。"""

import base64
import io
from pathlib import Path

from inkparse import (
    ExcludeOperation,
    FileSource,
    Parser,
    ParseRequest,
    PictureDescriptionOperation,
    revise_structure,
)
from inkstore.local import LocalAssetStore
from llama_index.core.utils import get_tokenizer
from PIL import Image

from inkrag.documents import read_documents, split_documents
from inkrag.models import ProcessingConfig, Provenance, Source
from inkrag.search import result_nodes


def test_table_rows_preserve_multi_level_headers_and_budget(tmp_path: Path) -> None:
    """多层列头、合并行头和长正文具有正确关联，小预算不吞掉数值。"""
    source = tmp_path / "policy.html"
    description = " ".join(f"detail{i}." for i in range(100))
    source.write_text(
        "<h1>Release policy</h1><table>"
        '<tr><th rowspan="2">Group</th><th colspan="2">Deadlines</th></tr>'
        "<tr><th>Region</th><th>Limit</th></tr>"
        '<tr><td rowspan="2">Primary</td><td>West</td><td>003.50</td></tr>'
        f"<tr><td>{description}</td><td>009.85</td></tr>"
        "<tr><td>Secondary</td><td>East</td><td>400.00</td></tr></table>"
    )
    parsed = Parser().parse(ParseRequest(source=FileSource(path=str(source))))
    sdk_origin = Provenance(
        document_id="document-a",
        resource_id="resource-a",
        resource_revision=1,
        source=Source(name=source.name, uri="fixture"),
        format="html",
        processing_revision="fixture",
    )
    config = ProcessingConfig(chunk_size=96, chunk_overlap=8)
    nodes = result_nodes("tenant", "kb", "document-a", parsed, config)
    sdk_documents = read_documents(source, sdk_origin, parsed)
    assert (
        [node.get_content() for node in nodes]
        == [node.get_content() for node in split_documents(sdk_documents, config)]
        == [node.get_content() for node in split_documents(sdk_documents, config)]
    )
    assert len({node.id_ for node in nodes}) == len(nodes)
    table_nodes = [
        node for node in nodes if node.metadata["parse_origin"]["element_ref"] == "#/tables/0"
    ]
    for content in ("003.50", "009.85", "400.00"):
        fragment = next(node.text for node in table_nodes if content in node.text)
        assert f"Deadlines / Limit: {content}" in fragment
        assert ("Secondary" if content == "400.00" else "Primary") in fragment
        assert "[章节] Release policy" in fragment
    assert any("detail99." in node.text for node in table_nodes)
    tokenize = get_tokenizer()
    assert all(len(tokenize(node.text)) <= 96 for node in nodes)
    assert all("_table_context" not in node.metadata for node in nodes)


def test_reviewed_picture_description_only_cites_the_exact_picture(tmp_path: Path) -> None:
    """人工图意不冒用原文字定位，也不借用文档内其他图片。"""
    images: list[str] = []
    for color in ("red", "blue"):
        stream = io.BytesIO()
        Image.new("RGB", (3, 2), color).save(stream, format="PNG")
        images.append(base64.b64encode(stream.getvalue()).decode())
    source = tmp_path / "figures.html"
    source.write_text(
        "<p>Original body</p>"
        + "".join(f'<img src="data:image/png;base64,{data}">' for data in images)
        + '<img src="https://invalid.example/unavailable.png">'
    )
    storage = LocalAssetStore.open(tmp_path / "assets")
    original = Parser().parse(ParseRequest(source=FileSource(path=str(source))), assets=storage)
    first, second, missing = original.document.pictures
    assert len(original.assets) == 2
    revised = revise_structure(
        original,
        [
            PictureDescriptionOperation(
                kind="picture_description", element_ref=first.self_ref, text="First reviewed."
            ),
            PictureDescriptionOperation(
                kind="picture_description", element_ref=second.self_ref, text="Excluded reviewed."
            ),
            PictureDescriptionOperation(
                kind="picture_description", element_ref=missing.self_ref, text="Missing picture."
            ),
            ExcludeOperation(kind="exclude", element_ref=second.self_ref, excluded=True),
        ],
    )
    nodes = result_nodes("tenant", "kb", "document-a", revised, ProcessingConfig())
    assert not any("Excluded reviewed." in node.text for node in nodes)
    first_binding = next(
        binding for binding in original.assets if binding.element_ref == first.self_ref
    )
    for node in nodes:
        origin = node.metadata["parse_origin"]
        if node.text == "First reviewed.":
            assert origin["element_ref"].startswith("#/texts/")
            assert origin["element_locator"] is None
            assert origin["related_asset_ids"] == [first_binding.asset_id]
            assert origin["picture_origin"] == {
                "kind": "reviewed_picture_description",
                "element_ref": first.self_ref,
                "asset_id": first_binding.asset_id,
                "page": first_binding.page,
            }
        elif node.text == "Missing picture.":
            assert origin["related_asset_ids"] == []
            assert "picture_origin" not in origin
        else:
            assert "picture_origin" not in origin
    assert {node.text for node in nodes} >= {"First reviewed.", "Missing picture."}
