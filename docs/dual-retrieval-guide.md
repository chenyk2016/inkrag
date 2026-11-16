# 双路召回入门

双路召回同时查关键词和正文向量。关键词 BM25 根据词项匹配排序，适合故障码、型号和条款号。语义向量按语义相近程度找片段，可以用不同说法搜同一件事。混合检索让 OpenSearch 用 RRF 合并两份排名，两路都排在前面的片段会得到两份贡献。每种方式最终都返回原文片段、来源和检索分数。分数用来排序，跨模式时优先比较原文是否相关、排在第几条。

## 用页面对照三种方式

1. 登录工作台，打开有**已发布版本**的知识库，进入左侧「检索测试」。可先按[快速开始](../README.md#3-运行工作台)创建自己的演示库。
2. 输入 `E102 故障应该怎么处理？`。在「检索选项」把结果数选为 **5**，点击「对照三种方式」。页面会用同一个问题、结果数和筛选条件依次查询三种方式，并检查三次查询指向同一发布。
3. 默认先显示「混合检索」。点击「关键词 BM25」或「语义向量」，看同一问题在不同方式下命中的片段及排名。示例中三种方式的第一条都是 `sensor.md`，可以从原文核对断电、等待停转及检查接线的顺序。把题目换成「空调温度传感器报错后要先做什么」，再看改用自然语言时，各路能否仍找回那段原文。
4. 点击「查看来源版本」回到对应资料和解析位置。展开结果上方的「检索方式 · 最多 N 条」可见服务实际执行的方式、结果数和筛选条件。顶部的「查看本次诊断」打开当前选项对应的请求记录。核对完原文或诊断，可从左侧回到「检索测试」继续切换对照结果。
5. 只想查询一路时，在「检索选项」选一种方式后点「检索」。清空选择会沿用已发布服务的默认方式。若对照被拒绝，到「检索配置」检查发布是否向当前调用方开放 `mode`；普通检索仍可使用已发布的默认策略。

本次本机验收样例中，关键词返回 2 条，向量和混合各返回 5 条。后几条有护发素或退货资料，适合用来练习识别尾部噪声。对实际场景，可设定业务字段筛选来缩小范围，并在「质量评测」里用已标注原文的问题对照检索效果。

[当前验证](release-readiness.md)

## 看同一次混合查询的两路贡献

有编辑权限时，在「检索测试」顶部点击「候选验证与对比」，在「A · 验证目标」选择检索方式已配置为混合的已就绪构建。勾选「详细诊断」，输入同一问题后点「验证」，再点「诊断此次试查」。可以查看每条最终命中的关键词和向量排名、各路贡献及融合分数。线上「检索测试」中的三组结果来自三次独立查询。候选的详细诊断显示**同一次混合查询**里的两路贡献。

这个例子中，候选详细诊断显示 `sensor.md` 在两路都排第一。每路贡献约为 `1 / (60 + 1) = 0.01639`，融合分数约为 `0.03279`。诊断展示本次授权返回的命中，完整候选总数与过滤前数量未采集。 [详细诊断契约](workspace-http-contract.md#显式详细诊断)

## 纯 Python 入库和检索

Python 程序直接引入 `inkrag`，用一个目录保存原件、SQLite 元数据和两路索引。给 `SiliconFlowEmbedding()` 提供 `SILICONFLOW_API_KEY`，或显式传入 `api_key=`。向量化将待嵌入的片段和查询发送给硅基流动托管的 BGE-M3。文件解析、关键词检索和索引存取在 Python 进程内完成。这条流程只需 Python 3.12（SQLite 启用 FTS5）、项目分发的 `inkrag` 及其 Python 依赖。

从仓库运行示例。将文档路径换成自己的 PDF、Markdown 或其他已支持格式：

```sh
uv run --locked --package inkrag --no-dev python examples/quickstart.py \
  --storage ./knowledge --query 'E102 故障怎么处理？' assets/samples/sensor.pdf
```

示例逐份入库后执行三种查询。`keyword` 使用 SQLite BM25，适合查编号和原文用词，查询时无需计算向量。`vector` 按向量相似度召回，也是原 `retrieve()` 的默认方式。`hybrid` 同时召回两路候选，默认由 LlamaIndex RRF 按两路排名融合。同一条命中保留原文和来源元数据。再次运行时省略文档路径即可从原目录读取索引，传入相同 `document_id` 入库则更新原资料。 [Python 示例](../examples/quickstart.py)

[Python SDK 参数参考](python-sdk.md)列出解析、切分、向量模型、每路候选数量与融合参数。

<a id="直接调用-python-代码包"></a>

## 从 Python 查询平台已发布库

在可信 Python 服务中，使用 `RAG.open_published()` 直接查询平台已发布的知识库。此调用在当前进程运行，使用平台的 PostgreSQL、OpenSearch 和已配置的向量服务，无需运行 InkRAG HTTP 服务。查询沿用同一份发布、权限、字段筛选和资料禁用规则。

1. 从本仓库运行时使用下方的 `uv run` 命令。接入自己的应用时，从项目分发源安装 `inkrag-platform[rag]`（包含 `inkrag`）。
2. 按[平台配置](production-runbook.md)为 Python 进程提供 `INKRAG_*` 环境变量。端点应使用此进程能访问的地址，配置的向量模型必须匹配已发布索引。代码也可向 `published_connection()` 传入 `settings=`。
3. 在目标知识库「接口与调用方」取得调用凭证。将它作为 `INKRAG_TOKEN` 提供给示例。已有组织访问令牌也可使用。

```sh
uv run --locked --package inkrag-platform --extra rag python examples/published_sdk.py \
  --kb-id "$INKRAG_KB_ID" 'E102 故障应该怎么处理？'
```

```python
import os

from inkrag import RAG
from inkrag_platform.sdk import published_connection

with RAG.open_published(
    published_connection(os.environ["INKRAG_KB_ID"], os.environ["INKRAG_TOKEN"])
) as rag:
    result = rag.retrieve("E102 故障应该怎么处理？", mode="hybrid")
    print(result["effective_request"], result["hits"])
```

[Python Demo](../examples/published_sdk.py) 依次执行 `keyword`、`vector` 和 `hybrid`，需要发布配置允许调用方设置 `mode`。业务代码可省略 `mode` 和 `top_k` 以采用发布默认值。发布允许自定义筛选时，可传 `filters=[{"field": "region", "value": "CN"}]`。结果包含 `release_id`、`trace_id` 和 `hits`。每条命中的 `metadata` 保留资料 ID、页码和解析元素位置。业务拒绝抛出 `inkrag_platform.models.DomainError`，可读取其 `code`。查询参数格式错误抛出 Pydantic 的 `ValidationError`。

可用[底层双路召回示例](../examples/dual_retrieval_sdk.py)在开发 OpenSearch 上用硅基流动向量模型建立临时索引，检验三种方式。

## 用 HTTP 查询已发布知识库

这一节用正式 HTTP 接口查询已发布的知识库。

使用仓库已安装的 Python 3.12 环境和一个有权检索目标库的凭证。可使用组织访问令牌，或在目标库「接口与调用方」取得调用凭证。将凭证放进本次终端的 `INKRAG_TOKEN` 环境变量。

```sh
uv run --locked --package inkrag-platform --extra rag python examples/dual_retrieval.py \
  --url "$INKRAG_URL" \
  --kb-id "$INKRAG_KB_ID" \
  --top-k 5 'E102 故障应该怎么处理？'
```

[HTTP Demo](../examples/dual_retrieval.py) 调用正式 `POST /api/v2/retrieve`，依次传 `mode=keyword`、`vector` 和 `hybrid`。输出包含发布 ID、每种方式的诊断编号、原件名和原文。换自己的知识库时，用自己的工作台地址、库 ID 和问题。省略 `--top-k` 可沿用发布设置。
