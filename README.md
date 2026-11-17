# InkRAG

把手册、制度和业务文档变成能回查原文的检索证据。查「E102 故障怎么处理」时，调用方可以核对原文步骤、页码和资料版本。

[Core 使用指南](docs/core-guide.md) · [交互学习手册](docs/learn/index.html) · [文档索引](docs/index.md) · [GitHub 发行版](https://github.com/chenyk2016/inkrag/releases) · [English](README.en.md)

## 选择入口

| 使用场景 | 入口 |
| --- | --- |
| 在 Python 应用中实现文档检索，替换分词、模型与数据后端 | [`inkrag-core`](docs/core-guide.md)，支持关键词、向量、融合、图与分步检索。 |
| 只解析文件，导出页码、表格、图片关联及风险提示 | [`inkparse`](docs/parsing-core.md)，扫描件 OCR 可选 Docling。 |
| 现有应用继续使用 `inkrag.RAG` | [既有 Python SDK](docs/python-sdk.md)，保留原有接口和知识目录。 |
| 多人协作审校并把知识发布给其他系统查询 | [工作台](#工作台)与 [HTTP API](docs/platform-http.md)，支持导入预览、修订、候选试查、发布和回滚。 |

## 快速开始

需要 Python 3.12 和 uv。先克隆仓库，后续命令均从源码目录运行。首次运行会下载 Python 依赖。

```sh
git clone https://github.com/chenyk2016/inkrag.git
cd inkrag
```

### Core 本地试跑

```sh
uv run --locked --no-dev --package inkrag-core python examples/core_retrieval.py
```

这个示例用合成资料和确定性向量，在本地执行入库、检索并输出证据及来源。使用自己的嵌入服务或数据库时见 [Core 使用指南](docs/core-guide.md)。

<a name="1-无密钥试跑"></a>

### 既有 SDK 无密钥试跑

```sh
uv run --locked --no-dev python examples/offline_quickstart.py
```

这个例子使用固定测试向量，实际执行解析、写入 SQLite、本地关键词检索、编号匹配和重开。它用于验证安装和数据链路。要试语义检索，换用下一节的真实向量模型。

<a name="2-使用真实向量"></a>

### 既有 SDK 使用真实向量

将 `SILICONFLOW_API_KEY` 注入当前进程环境，再运行：

```sh
uv run --locked --no-dev python examples/quickstart.py \
  --storage ./knowledge --query 'E102 故障怎么处理？' assets/samples/sensor.pdf
```

此示例会把文档片段和问题发送给配置的硅基流动向量 API。用自己的资料前先确认允许发送的内容。也可在 [Python SDK](docs/python-sdk.md) 中传入自己持有的嵌入模型。

```python
from inkrag import RAG, SiliconFlowEmbedding

with RAG.open("./knowledge", embedding=SiliconFlowEmbedding()) as rag:
    receipt = rag.ingest("manual.pdf", document_id="support-manual")
    for hit in rag.retrieve("E102 怎么处理？", mode="hybrid").hits:
        print(hit.get_content(), hit.metadata["source"], hit.metadata["page"])
```

<a name="3-运行工作台"></a>

## 工作台

需要 Docker Engine 或 Docker Desktop（带 Compose）。首次启动会获取依赖镜像与中文向量模型，并在环回地址启动服务。

```sh
uv run --locked --package inkrag-platform --extra rag inkrag up --rebuild
```

打开终端显示的工作台地址，使用本机 `.inkrag/access.json` 中的开发账号登录。创建知识库 → 添加 `assets/samples/sensor.pdf` → 查看预览 → 确认导入 → 在「检索测试」问 E102。也可以关闭自动生效，手动验证并发布候选。请保管好 `.inkrag` 内的配置和账号文件。

```sh
uv run --locked --package inkrag-platform --extra rag inkrag status
uv run --locked --package inkrag-platform --extra rag inkrag down
```

停止服务会保留资料卷。使用独立的 `INKRAG_STATE_DIR` 可以创建另一个开发栈。生产部署使用 [单独的部署方案](docs/deployment.md)。

## 架构

| 模块 | 职责 |
| --- | --- |
| [`inkrag-core`](docs/core-guide.md) | 通过可替换接口组装解析、分块、分词、嵌入、保存和检索策略 |
| [`inkparse`](packages/inkparse/README.md) | 读取原件并保留来源，提供可替换的解析适配器 |
| [`inkstore`](packages/inkstore/README.md) | 保存原件、派生资产和不可变记录，提供本地与 S3 适配 |
| [`inkrag`](docs/python-sdk.md) | 原有 SDK 与平台用的片段准备和检索 |
| [`inkrag-platform`](docs/platform-implementation.md) | 按可信身份管理任务、授权、候选和发布。HTTP 和已发布库 Python 查询共用核心 |
| [`web`](web/README.md) | React 工作台，通过 HTTP 展示真实资料、任务、版本和评测 |

Core 可选择 SQLite 或 PostgreSQL + pgvector。工作台的现有架构使用 PostgreSQL 管理事实与发布指针、S3 保存资产、RabbitMQ 传递后台任务、OpenSearch 检索、TEI 生成向量、OIDC 提供登录。线上查询固定到选定的发布索引，并应用当前资料授权。

## 文档与验证

| 我想做什么 | 入口 |
| --- | --- |
| 理解数据流、读源码、跟 AI 搭建自己的版本 | [当前源码的交互手册](docs/learn/index.html)（克隆后用浏览器打开） · [公开发行的在线教程](https://chenyk2016.github.io/inkrag/) |
| 选择 Core 组件并验证 | [配置、使用与验收](docs/core-guide.md) |
| 延用 Python 或 HTTP 集成 | [Python SDK 参数](docs/python-sdk.md) · [HTTP v2](docs/platform-http.md) · [三种检索示例](docs/dual-retrieval-guide.md) |
| 确认支持的格式和索引规则 | [解析契约](docs/parsing-core.md) · [检索实现与分词扩展](docs/search-backend.md) |
| 自行部署及排查故障 | [生产部署方案](docs/deployment.md) · [运行配置](docs/production-runbook.md) |
| 了解已验证能力及交付缺口 | [架构、缺陷与验收](docs/release-readiness.md) · [发行流程](docs/releasing.md) · [GitHub CI](https://github.com/chenyk2016/inkrag/actions/workflows/quality.yml) |
| 提交补丁或报告问题 | [贡献指南](CONTRIBUTING.md) · [安全问题](SECURITY.md) |

```sh
npm ci --prefix web
uv sync --locked --all-packages --all-extras
sh scripts/check.sh
```

GitHub 当前已发行版本为预发行版本。生产部署按目标拓扑、资料和指标单独验收故障转移、异地恢复、持续容量和业务召回。

## 许可证

InkRAG 项目代码与文档采用 [Apache License 2.0](LICENSE)。第三方组件沿用其各自的许可证，见 [NOTICE](NOTICE) 和 [第三方声明](THIRD_PARTY_NOTICES.md)。
