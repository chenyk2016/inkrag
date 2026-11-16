# inkparse

独立同步解析核心，不依赖 RAG、LlamaIndex、嵌入模型或 HTTP。正式入口为 `inkparse.Parser`，正文复用 Docling 原生 `DoclingDocument`。存储通过独立 `inkstore.AssetStorage` 注入，解析包不决定使用本地、MinIO 或 S3。

```python
from inkparse import FileSource, ParseRequest, Parser, save_result

result = Parser().parse(ParseRequest(source=FileSource(path="manual.docx")))
save_result(result, "parsed.json")
```

| 格式 | 实际读取器与保留内容 | 明确边界 |
|---|---|---|
| TXT、Markdown | UTF-8 正文，Markdown 保留原始语法 | 不重建 Markdown 图片、表格或标题树 |
| 文本 PDF | pypdf 逐物理页正文；配置资产仓库后保存嵌入位图 | 文本位置是整页范围；不重建表格、复杂阅读顺序、矢量图；扫描图页需要 OCR 则拒绝 |
| HTML | BeautifulSoup 静态 DOM 的标题、正文、合并表格、图片逻辑位置和受控 base64 位图 | 不执行脚本、不访问任何外链或本地附件、不推断 CSS 视觉顺序；嵌套或歧义表格拒绝 |
| JSON | 标准库逐叶保留键路径、标量类型、空容器；小数用 Decimal 保持精度 | 重复键拒绝；JSON 指针使用 RFC6901 转义；不猜测业务字段 |
| XML | 标准库保留命名空间、元素序号、属性、混合文字及尾随文字 | UTF-8；DTD/实体声明拒绝；不访问外部资源 |
| CSV | 标准库严格逗号 CSV，原生表格保留引号内逗号或换行及空值 | UTF-8；不猜测分隔符、表头、日期或区域数字；不执行公式 |
| DOCX | python-docx 原生段落与表格，合并跨度、嵌入图及 OOXML 逻辑定位 | 不编造 Word 渲染页码；修订、文本框、嵌入文档和不支持的嵌套表格拒绝；页眉或脚注等辅助内容缺口有显式警告 |
| PPTX | python-pptx 原生文字、合并表格、嵌图、备注，保留幻灯片编号与形状范围 | 形状顺序不等同视觉顺序；组合内定位到组范围；图表、连线或流程关系、SmartArt 与图片理解均有显式警告 |

Legacy DOC/PPT、OCR、独立流程图的语义重建不在当前能力内。复杂 PDF 仍使用文本读取器；成熟读取库的存在不代表已达到企业资料的人工标注质量目标。

## 选择解析工具

默认内置解析保留上述功能和限制；Docling是独立可选适配，不替换已有路径。安装`inkparse[docling]`启用固定版本Docling 2.150.0；基础`inkparse`安装不强制安装该引擎或下载模型。

```python
from inkparse import FileSource, ParseOptions, ParseRequest, Parser

result = Parser().parse(
    ParseRequest(
        source=FileSource(path="manual.docx"),
        options=ParseOptions(engine="docling"),
    ),
    assets=storage,
)
```

内置`builtin`支持原九类格式；`docling`另支持XLSX及PNG/JPEG/TIFF/BMP/WebP图片。PDF与图片使用本地布局、表格和中文OCR模型，需先运行`uv run python scripts/prepare-docling-models.py`，SDK设置`INKPARSE_MODELS_PATH`；平台设置`INKRAG_PARSE_MODELS_PATH`，子进程只借入该只读目录。目录存在不等于质量验收。Docling其他官方格式未装配/验证的依赖，本产品尚不宣称支持。

两种工具都返回同一个`ParseResult`，实际读取版本写入`engine`，选择写入`options.engine`。缺省builtin字段不序列化，旧交换包摘要不因本次新增选项改变。Docling失败不切换内置：Markdown会恢复标题/表格结构，CSV会探测分隔符及首行表头并保存警告；TXT和通用JSON/XML显式复用基础输入适配并标注来源。Docling逻辑定位使用原生元素引用与可用页坐标，不伪造旧OOXML/DOM路径。图片转存注入资产服务，清除原生图片路径与内嵌字节；PPTX备注在有效内容导出与RAG中保留。

实现分别位于`adapters/builtin/`和`adapters/docling/`，公共入口只冻结输入、选择适配、校验及提交。新增工具应新增独立适配目录和显式能力声明，统一返回原生文档、资产绑定、来源与警告，不另造节点模型或动态插件平台。页面读取服务`parse_engines`声明，在文件列表下选择工具；任务持久化`parser_engine`，重试/重解析不随默认配置改选。

## JSON 与资产

Docling PDF/图片可传`ParseOptions(engine="docling", ocr_enabled=True, ocr_mode="full_page", table_structure=True, table_mode="fast")`。识别范围`auto/full_page`默认auto，表格模式`fast/accurate`默认accurate；关闭OCR时不得传范围，关闭表格时不得传模式。其他格式/工具显式传这些高级参数会拒绝，不静默忽略。默认None省略序列化，旧结果摘要不变。

首次机器输出为 `inkparse.result.v3`，结构审校生成显式的 `inkparse.result.v4`。两者包含原件名、格式、sha256、实际引擎版本、原生文档、警告、资源绑定及可选 `origins` 逻辑定位。`coverage` 区分 `text_only` 与 `native_structure`；它说明执行范围，不能作为准确率。原生元素使用 `#/texts/N`、`#/tables/N` 或 `#/pictures/N` 引用；结构格式通过 `origins[].locator` 给出 JSON 指针、XML 路径、HTML 路径、CSV 行范围或 OOXML 位置。空 origins 不改变既有 v3 文本包的摘要语义。新版读取器可读取旧包；旧版拒绝未知字段的严格读取器不保证能读取包含这些新增字段或格式的新包。

```python
from inkparse import load_stored_result

# 应用层配置 storage，Parser 只借入 AssetStorage 协议。
result = Parser().parse(request, assets=storage)
restored = load_stored_result(storage, result.result_id)
```

注入仓库时先保存冻结原件，再保存提取图片，最后提交已校验结果及引用；中途失败可能留下可清理的未引用资产，不提交半份成功结果。`source.asset_id` 关联原件，`assets[]` 将图片 ID 绑定原生元素及物理页。JSON 不包含图片字节、凭据或解析机器绝对路径。未注入仓库时不会自动创建存储，图片有位置及缺口提示，字节不被假装为已保存。外链图片只记录来源，始终不自动访问。

`save_stored_result`、`load_stored_result` 负责解析与通用存储记录之间的映射；`copy_result` 只在调用方显式要求时跨仓库复制。S3/MinIO 的部署与验证属于注入实现及宿主责任。

## 限制、修订与验证

源文件最大 20 MiB，结果 JSON 最大 32 MiB，原生元素最多 20000，表格最多 100000 单元格；结构层级限制 128。Office 压缩包另限 10000 条目、总解压 100 MiB、单条目 20 MiB，拒绝加密或重复条目和 XML 实体。同步核心不执行操作系统隔离；生产宿主须在有时间、内存和并发预算的隔离 worker 中运行。

`revise_result(result, {"#/texts/0": "修订文字"})` 保留已有文字修订入口。结构操作使用 `revise_structure`，创建明示 v4 的新包，不改变输入、冻结原件、机器首版或原始定位：

```python
from inkparse import TextOperation, TableCellOperation, ExcludeOperation, revise_structure

revised = revise_structure(
    result,
    [
        TextOperation(kind="text", element_ref="#/texts/1", text=""),
        TableCellOperation(kind="table_cell", element_ref="#/tables/0", row=1, column=2, text="29"),
        ExcludeOperation(kind="exclude", element_ref="#/pictures/0", excluded=True),
    ],
)
```

| `kind` | 字段与行为 |
|---|---|
| `text` | `element_ref`、`text`；可清空噪声，保留原生 `orig` |
| `table_cell` | `element_ref`、零基 `row` / `column`、`text`；合并格仅接受左上角 |
| `picture_caption` | `element_ref`、`text`；保存原生图片标题关联 |
| `picture_description` | `element_ref`、`text`；保存人工文字，明确标注人工来源 |
| `exclude` | `element_ref`、`excluded`；排除或恢复，不物理删除原生内容 |
| `append_text` | `text`、`label=text\|section_header`；文末补充，不伪造原件页码/定位 |

v4 的 `editor.excluded_refs` 控制有效内容；`manual_refs` 标注人工添加的原生文字；`picture_descriptions` 和 `picture_description_refs` 保留人工图片说明与稳定文字引用。人工文字不会获得 `origins` 或物理页坐标。使用 `effective_document(result).iterate_items()` 消费有效内容，不要直接遍历原生 `texts` 数组：为保留所有来源引用，数组中仍保留被排除元素。普通 Markdown 和 RAG 适配使用同一有效结构树。修订链通过 `revision`、`previous_result_id` 指向旧不可变结果；宿主另外保存资料历史顺序，重新解析原件时不能把二者混为同一版本号。原生文档内部可变，直接修改而不创建新摘要会被校验拒绝。

```python
from inkparse import export_bundle, save_bundle

packet = export_bundle(revised, storage, include_original=False)
save_bundle(revised, storage, "delivery.zip", include_original=True)
```

HTTP和`save_bundle`逐资源写临时ZIP并分块发送或原子替换，不在内存堆积全部附件；`export_bundle`为需要完整字节的SDK调用保留。离线 ZIP 包含 `document.md`、完整 `document.json`、`assets/<sha256>.<extension>` 以及 `manifest.json`。Markdown 图片使用相对路径；清单记录文件摘要、资源路径、未抓取的外链和原件身份。原件只有 `include_original=True` 才附带，固定存于 `original/source.<extension>`，从不把上传文件名当作包内路径。完整 JSON 保留排除内容，Markdown 只包含有效投影。资源缺失、摘要不符、非位图、图片超过4000万像素或总量超过128MiB时整个导出失败，不交付残缺成功包。

`ParseError.diagnostic()` 返回安全的 `code/message/location/suggestion/retryable`。JSON/XML语法错误包含行列、UTF-8错误包含字节位置，不回显私有正文。确定性坏文件不自动重试；宿主提供“替换原件并解析”或新建重新解析意图。在新解析成功前，旧原件和旧可用结果保持可读。PDF 以真实原件页面渲染；DOCX/PPTX 在未装Office渲染引擎时仅提供明确标注的机器首版结构对照，不生成伪造排版页图。

2026-11-09 使用真实生成的 DOCX/PPTX/HTML/JSON/XML/CSV 进行结构和资源回归，覆盖合并表格、引号换行、JSON 小数精度、OOXML 图片、静态外链不抓取及拒绝反例。合成资料通过不代表复杂企业真实文档的质量验收；应以人工标注语料逐格式测正文覆盖、表格单元格或合并关系、顺序和引用正确性。

平台集成测试覆盖审校冲突、历史原文、ZIP 交付、资料归属与隔离解析。用于复现的测试入口、独立安装和本轮验证范围见[发行验收](../../docs/release-readiness.md)。

本仓库中运行：`uv run pytest tests/test_parsing_formats.py tests/test_parsing.py tests/test_parsing_editing.py tests/test_assets.py`。库能力依据：[python-docx](https://python-docx.readthedocs.io/en/latest/api/document.html)、[python-pptx](https://python-pptx.readthedocs.io/en/latest/api/shapes.html)、[BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)。
