"""把各格式的原生结构写入Docling，统一内容预算和资源关联。"""

from docling_core.types.doc.common.annotations import DescriptionAnnotation
from docling_core.types.doc.common.reference import ProvenanceItem
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.picture.picture import PictureDataType
from docling_core.types.doc.items.table.table_data import TableCell, TableData
from docling_core.types.doc.labels import DocItemLabel
from inkstore import AssetStorage

from .models import AssetBinding, ElementOrigin, ParseError, Warning

MAX_ELEMENTS = 20000
MAX_CELLS = 100000
MAX_RESOURCE_BYTES = 20 * 1024 * 1024


class StructureBuilder:
    """单次解析的结构写入器，不选择资源后端，不读取网络或本地附件。"""

    def __init__(self, document: DoclingDocument, assets: AssetStorage | None) -> None:
        """借入文档与资产接口，所有写入归属于当前解析调用。"""
        self.document, self.assets = document, assets
        self.bindings: list[AssetBinding] = []
        self.origins: list[ElementOrigin] = []
        self.warnings: list[Warning] = []
        self.cell_count = 0

    def warning(self, code: str, message: str, page: int | None = None) -> None:
        """同类同页风险只保存一份，避免图片数量放大交换包。"""
        if not any(item.code == code and item.page == page for item in self.warnings):
            self.warnings.append(Warning(code=code, message=message, page=page))

    def origin(self, reference: str, locator: str, resource_uri: str | None = None) -> None:
        """将元素绑定原件内逻辑位置，超过预算时整次失败。"""
        if len(self.origins) >= MAX_ELEMENTS or len(locator) > 4096:
            raise ParseError("structure_budget_exceeded", "文档元素或逻辑定位超过解析预算")
        self.origins.append(
            ElementOrigin(element_ref=reference, locator=locator, resource_uri=resource_uri)
        )

    def text(
        self,
        text: str,
        locator: str,
        *,
        label: DocItemLabel = DocItemLabel.TEXT,
        heading_level: int | None = None,
        prov: ProvenanceItem | None = None,
    ) -> None:
        """保存非空正文或标题，保持输入遍历顺序及可追溯的逻辑位置。"""
        if not text.strip():
            return
        item = (
            self.document.add_heading(text=text, level=heading_level, prov=prov)
            if heading_level is not None
            else self.document.add_text(label=label, text=text, prov=prov)
        )
        self.origin(item.self_ref, locator)

    def table(
        self,
        cells: list[TableCell],
        rows: int,
        columns: int,
        locator: str,
        prov: ProvenanceItem | None = None,
    ) -> None:
        """保存带合并跨度的原生表格，空表不创建占位内容。"""
        if not rows or not columns:
            return
        self.cell_count += len(cells)
        if self.cell_count > MAX_CELLS or rows * columns > MAX_CELLS:
            raise ParseError("structure_budget_exceeded", "表格超过100000个单元格预算")
        item = self.document.add_table(
            data=TableData(table_cells=cells, num_rows=rows, num_cols=columns), prov=prov
        )
        self.origin(item.self_ref, locator)

    def image(
        self,
        data: bytes | None,
        media_type: str,
        locator: str,
        *,
        alt: str = "",
        prov: ProvenanceItem | None = None,
        resource_uri: str | None = None,
    ) -> None:
        """图片字节只进入注入仓库；外链仅记录位置，绝不自动抓取或解释为正文。"""
        if data is not None and (not data or len(data) > MAX_RESOURCE_BYTES):
            raise ParseError("resource_budget_exceeded", "图片为空或超过20MiB预算")
        annotations: list[PictureDataType] = (
            [DescriptionAnnotation(text=alt, provenance="source-alt-text")] if alt else []
        )
        item = self.document.add_picture(annotations=annotations, prov=prov)
        self.origin(item.self_ref, locator, resource_uri)
        page = prov.page_no if prov is not None else None
        if data is None:
            self.warning(
                "external_image_not_fetched", "图片外链只保留来源，未自动访问网络或本地路径", page
            )
        elif self.assets is None:
            self.warning(
                "images_not_extracted", "未配置资产仓库，图片只保留逻辑位置和原件引用", page
            )
        else:
            asset = self.assets.put_bytes(data)
            self.bindings.append(
                AssetBinding(
                    asset_id=asset.asset_id,
                    element_ref=item.self_ref,
                    role="image",
                    page=page,
                    media_type=media_type,
                )
            )


def table_cell(
    text: str, row: int, column: int, *, rows: int = 1, columns: int = 1, header: bool = False
) -> TableCell:
    """按零基半开区间构造Docling单元格，显式保存合并范围。"""
    return TableCell(
        text=text,
        start_row_offset_idx=row,
        end_row_offset_idx=row + rows,
        start_col_offset_idx=column,
        end_col_offset_idx=column + columns,
        row_span=rows,
        col_span=columns,
        column_header=header,
    )
