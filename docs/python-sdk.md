# InkRAG Python SDK 参数参考

独立 Python 包名是 `inkrag`，公开入口集中在 [`inkrag/__init__.py`](../src/inkrag/__init__.py)。使用 Python 3.12 和支持 FTS5 的 SQLite。原件、资料元数据和检索索引保存在 `RAG.open()` 指定的目录，向量化可以调用硅基流动托管 API。工作台接入另见[查询平台已发布库](dual-retrieval-guide.md#从-python-查询平台已发布库)。

```python
from inkrag import HybridConfig, ParseOptions, ProcessingConfig, RAG, SiliconFlowEmbedding

embedding = SiliconFlowEmbedding(model="BAAI/bge-m3", embed_batch_size=8, timeout=20)
with RAG.open("./knowledge", embedding=embedding, write_lock_timeout=5) as rag:
    receipt = rag.ingest(
        "manual.pdf",
        document_id="manual",
        parse_options=ParseOptions(engine="builtin", format="pdf", max_bytes=10 * 1024**2),
        processing=ProcessingConfig(chunk_size=384, chunk_overlap=48, include_title=True),
    )
    result = rag.retrieve(
        "E102 故障怎么处理？",
        mode="hybrid",
        top_k=5,
        hybrid_config=HybridConfig(
            keyword_top_k=25, vector_top_k=40,
            fusion="relative_score", keyword_weight=0.65,
        ),
    )
    print(receipt.snapshot_id, [(hit.get_content(), hit.metadata) for hit in result.hits])
```

## 打开知识库 `RAG.open()`

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `path` | 必填 | 知识目录路径。空目录初始化为新库，用同一路径重开已有库。 |
| `embedding` | 必填 | 向量化对象。可传 `SiliconFlowEmbedding()` 或其他 LlamaIndex `BaseEmbedding` 实现。 |
| `model_revision` | 使用 `embedding.model_name` | 调用方声明的权重或向量语义版本。重开时与模型名称一起核对，模型或维度切换使用新目录重建索引。 |
| `assets` | `None` | 注入 `inkstore.AssetStorage`。需要保存和读取解析图片等附属资产时，可传本地 `LocalAssetStore` 等实现。 |
| `write_lock_timeout` | `30` 秒 | 初始化和入库等写操作等待同一目录写锁的时间。`0` 立即尝试，`-1` 一直等，其他取值为有限非负秒数。 |
| `lexical_profile` | `None` | 新入库快照的关键词规则。空库默认用 `"legacy"`，已有库默认沿用当前快照。显式传 `"jieba-v1"` 可从下一次入库起启用 Jieba 和编号匹配。查询始终遵循所查快照的规则。 |

`jieba-v1` 在入库和查询中使用同一份固定的 Jieba 0.42.1 自带词典，全文排序仍由 SQLite FTS5 BM25 完成。只查询 `AB-123` 这类编号时，关键词和混合模式走精确编号检索。编号带问题时，关键词路给精确编号加分，向量路仍使用原问题。详见[方案及对照](search-backend.md#可选的-jieba-词项索引)。

## 入库 `rag.ingest()` 和 `rag.ingest_parsed()`

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `source` | 必填 | `ingest()` 接收本地原件路径，`ingest_parsed()` 接收 `inkparse` 交换 JSON 的路径。 |
| `document_id` | 由解析后的绝对路径生成 | 资料的稳定 ID。同名文件在不同目录时会得到不同的默认 ID。同一路径再次入库，或显式沿用同一 ID，会替换原资料在当前快照中的片段。手动传值时去除首尾空白，允许 1–256 字符。 |
| `processing` | `ProcessingConfig()` | 配置切分和标题增强。两种入库入口均支持。 |
| `parse_options` | `ParseOptions()` 的默认行为 | `ingest()` 的原件解析设置，随本次入库任务冻结。交换 JSON 已包含自己的解析结果。 |

首次入库后可以保存回执中的 `document_id`。文件改名或搬到别的目录后，省略 ID 会按新路径入库为新资料。将原回执的 ID 传给下一次 `ingest()`，即可更新原资料。

```python
first = rag.ingest("support/manual.md")
# 资料在磁盘上已改名并修订。
updated = rag.ingest("support/manual-v2.md", document_id=first.document_id)
assert updated.resource_id == first.resource_id
assert updated.resource_revision == first.resource_revision + 1
```

提供 `SILICONFLOW_API_KEY` 后可运行独立示例，验证两个同名文件的 ID 不同，以及复用 ID 后资料版本递增。 [资料身份 Demo](../examples/document_identity.py)

```sh
uv run --locked --package inkrag --no-dev python examples/document_identity.py
```

`ParseOptions` 与独立 `inkparse` 包共用。内置解析是默认值。Docling 是按需安装的解析工具，PDF 或图片使用的模型工件需要按[解析说明](parsing-core.md)准备。

| `ParseOptions` 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `engine` | `builtin` | 选择 `builtin` 或 `docling`。所选工具决定可读格式和识别能力。 |
| `format` | `auto` | 按扩展名识别，也可明确指定 `text`、`markdown`、`pdf`、`html`、`json`、`xml`、`csv`、`docx`、`pptx`、`xlsx`、`image` 之一，由解析工具验证支持情况。 |
| `max_bytes` | `20 MiB` | 原件读取上限，单位字节，范围 1–20 MiB。入库复制原件时即执行上限。 |
| `ocr_enabled` | `None`，按工具默认 | Docling PDF 或图片默认打开 OCR，`False` 可关闭。 |
| `ocr_mode` | `None`，Docling 使用 `auto` | `auto` 交给 Docling 判断识别范围，`full_page` 强制整页 OCR。用于开启 OCR 的 Docling PDF 或图片。 |
| `table_structure` | `None`，Docling 默认开启 | 控制 Docling PDF 或图片是否识别表格结构。 |
| `table_mode` | `None`，Docling 使用 `accurate` | 已启用表格结构识别时，可选 `fast` 或 `accurate` 调节速度和精度。 |

| `ProcessingConfig` 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `chunking` | `auto` | Markdown 按标题分节后切分，其他格式按句切分。也可显式指定 `markdown` 或 `sentence`。 |
| `chunk_size` | `256` | 单片段的 token 预算，范围 32–8192，使用 LlamaIndex 的切分 tokenizer。选用较短上下文的向量模型时，可相应减小。 |
| `chunk_overlap` | `32` | 相邻片段重叠 token 数。可为 0，必须小于 `chunk_size`。 |
| `include_title` | `False` | 将章节标题或资料名称加在片段前面，供向量和关键词索引使用。标题计入片段预算。 |

## 向量化 `SiliconFlowEmbedding()`

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `api_key` | 环境变量 `SILICONFLOW_API_KEY` | 硅基流动调用凭证。显式值优先。 |
| `model` | `BAAI/bge-m3` | 硅基流动提供的向量模型 ID。入库和查询使用同一模型。 |
| `dimensions` | `None`，采用模型默认维度 | 向支持指定输出维度的 Qwen3 模型请求输出长度，取值由具体模型决定。设置值计入模型身份。 |
| `embed_batch_size` | `16` | 每次入库请求最多提交的片段数。SDK 接受 1–2048，服务端还会按实际模型限制。 |
| `timeout` | `30` 秒 | 每次硅基流动 HTTP 请求的连接和读写超时。取有限正数。 |

模型 ID、支持的输出维度和输入限制见[硅基流动嵌入 API](https://api-docs.siliconflow.cn/docs/api/embeddings-post)。

## 查询 `rag.retrieve()`

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `query` | 必填 | 查询文本。去除首尾空白后允许 1–4000 字符。 |
| `top_k` | `3` | 最终最多返回的片段数，范围 1–100。实际结果数取决于可用命中。 |
| `mode` | `vector` | `keyword` 走本地 BM25，`vector` 按向量相似度召回，`hybrid` 并用两路。 |
| `hybrid_config` | `HybridConfig()` 的默认行为 | `hybrid` 模式下配置每路召回数量和融合方式。 |

| `HybridConfig` 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `keyword_top_k` | `max(top_k, 20)` | 混合检索中关键词路送入融合的候选上限，显式值范围 1–100。 |
| `vector_top_k` | `max(top_k, 20)` | 混合检索中向量路送入融合的候选上限，显式值范围 1–100。 |
| `fusion` | `rrf` | `rrf` 按两路排名融合。`relative_score` 先在每路内归一化分数，再加权合并。 |
| `keyword_weight` | `relative_score` 时 `0.5` | 关键词路权重，向量路取 `1 - keyword_weight`。仅适用于 `relative_score`，取值在 0 与 1 之间。 |

检索参数在当次查询生效。解析或切分参数改动后，用原 `document_id` 重新入库即可更新对应资料。模型、模型版本或显式向量维度变更时，使用新目录重建知识库。

`IngestionResult.document_id` 返回本次实际使用的资料 ID，`resource_id` 是库内资源 ID。回执也提供版本、快照、片段数和任务 ID。可用 `rag.jobs.get(job_id)` 检查任务。`RetrievalResult.hits` 包含原文、得分和来源，`snapshot_id` 与 `trace_id` 用于核对结果来自哪次快照和查询。 [可运行示例](../examples/quickstart.py)
