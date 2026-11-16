# 生产检索适配

`inkrag.search.SearchBackend`借入官方OpenSearch客户端和LlamaIndex `BaseEmbedding`，不创建数据库、资产仓库或模型，不维护发布指针。PG是资料、任务和发布的事实来源；OpenSearch只保存可重建投影。调用方负责租约、重试、权限及PG条件提交。

## 构建与查询

1. `create_build(tenant_id, knowledge_base_id, build_id, previous_generation, field_schema)`创建独立候选。每个租约attempt使用唯一build_id。返回实际物理索引名，PG应保存这个返回值。
2. 首次构建直接写入；增量构建通过OpenSearch `_reindex`复制前一冻结版本，包括已有向量。只对新增或变更资料重新解析、切分和嵌入，删除资料调用`remove_document`。
3. `upsert_result`将已验证的`ParseResult`桥接为原生LlamaIndex文档，复用共享切分器；文本及表格Markdown参与检索，图片通过原件/元素/资产引用返回。用户标量字段按显式schema校验，不接受原始查询DSL。
4. `freeze`调用后端Blocks API，等待写屏障确认，refresh并校验实际write block，再精确统计节点和资料数量。部分bulk、复制失败、刷新失败均明确报错，候选不能发布。
5. 宿主核对manifest和PG预期资料，带fencing条件更新发布指针。查询开始时pin这个物理generation；适配器不自行查alias，因此并发发布不会改变请求中的版本。

```python
from inkrag.search import SearchBackend

search = SearchBackend(client, embedding,
    model_revision="完整模型工件revision", dimensions=512, replicas=1)
candidate = search.create_build(tenant_id, kb_id, unique_attempt_id,
    previous_generation=published_generation, field_schema={"region": "text"})
search.upsert_result(tenant_id, kb_id, candidate, document_id, parsed,
    metadata={"region": "CN"})
manifest = search.freeze(tenant_id, kb_id, candidate)
# PG核对并条件发布；下一次请求从PG获取published_generation。
hits = search.query(tenant_id, kb_id, published_generation, "保修期限")
```

`query`返回原生`NodeWithScore`；可选HTTP宿主通过`query_json`取得普通JSON，避免parse-only宿主导入LlamaIndex。命中元数据包含可信tenant/KB/document、解析result/revision、原件sha256/asset_id、原生element_ref、同页相关资产ID及用户字段。

## 过滤与质量边界

模式为`keyword`、`vector`、`hybrid`。关键词使用BM25，向量采用Lucene HNSW，混合使用OpenSearch官方`score-ranker-processor`的RRF。tenant、KB及用户条件同时进入BM25与kNN内部filter，在候选Top-K前执行。任何超时、部分分片失败、范围外命中或损坏响应均失败，不返回伪造空结果。字段类型支持text（精确keyword）、number、boolean、date；运算符按类型受控，`ne`要求字段存在。

索引名称只含可信范围/构建SHA256摘要，mapping为strict；模型工件revision与维度随generation固定。切换模型须从PG原件/解析事实重建，不能复制不兼容向量。冻结索引不可改写；迟到worker即使仍持旧内存对象，也会被后端写屏障拒绝。生产服务账号应按部署角色最小授权，不能允许请求用户直接访问OpenSearch或修改索引设置。

生产宿主注入固定BGE权重revision对应的原生`tokenizers.Tokenizer`，由LlamaIndex `SentenceSplitter`执行切分。正文安全上限510为512扣除CLS/SEP，不把字符数或其他分词器计数当模型token数。切分配置与分词工件身份共同绑定处理版本；每个最终片段再次核对长度。查询计数包含BGE前缀与特殊token，超限明确拒绝。TEI客户端读取真实`/info`的条数/批token预算，按保守padding成本分批，始终请求`truncate=false`。工件、摘要和MIT许可材料随平台wheel分发，启动不下载分词器。

分数用于排序。用实际资料和人工标注问题评估 Recall@K、MRR、引用完整性及过滤正确率。

## 检索模块与扩展入口

分词是包内服务。`LexicalAnalyzer.analyze(text, query=...)` 接收片段或问题，返回预切词项和可选精确编号，也可以返回交给引擎分析的原文。`lexical_adapters.py` 按索引记录的版本选择实现。历史本地快照使用单字、双字规则，历史平台索引委托 OpenSearch `standard`，两个宿主的 `jieba-v1` 都使用 `lexical_jieba.py`。

```text
入库  解析、切分 → 原片段 ┬→ 分词 → 关键词索引
                        └→ 向量模型 → 向量索引

查询  校验问题和模式 ┬→ 按索引版本分词 → 关键词召回 ┐
                    └→ 原问题送向量模型 → 向量召回 ┴→ 混合模式融合
```

| 模块 | 职责 |
| --- | --- |
| `lexical.py`、`lexical_adapters.py`、`lexical_jieba.py` | 定义分词接口、选择版本、实现具体分词规则 |
| `sqlite_keyword.py`、`opensearch_keyword.py` | 用 SQLite FTS5 或 OpenSearch 消费词项，负责字段、匹配和后端评分 |
| `retrieval.py`、`local_retrieval.py`、`search.py` | 共享校验和纯编号路由，各宿主调用原生向量与融合；平台继续管理可信范围和冻结 |
| `rag.py`、`workspace_query.py` | 本地 SDK 绑定快照并记录查询，平台查询核心供已发布库 Python SDK 与 HTTP 调用 |

新增使用现有字段的词项算法时，实现 `LexicalAnalyzer` 并在 `LexicalProfile` 和 `lexical_analyzer()` 登记新版本，构建该版本的索引。已有版本继续指向原实现。增加引擎专属分析器或新索引字段时，扩展相应的索引适配器并使用新索引版本。平台仅在启用检索能力时用核心的版本定义校验配置，独立解析仍可无检索包运行。

## 可选的 Jieba 词项索引

平台默认 `INKRAG_LEXICAL_PROFILE=legacy`，使用 `standard` 正文字段。试用时在 API 与 Worker 的环境设置 `INKRAG_LEXICAL_PROFILE=jieba-v1`，然后从现有解析版本构建候选。候选记录分词版本。查询读取目标索引的版本，因此旧发布、候选试查和回滚可各自执行对应规则。分词规则改变时首次完整构建，现行 Worker 从冻结的解析结果重新切分及嵌入。同版本后续构建可按既有规则复制未变向量。

- `inkrag.lexical_jieba` 统一用固定的 Jieba 0.42.1 自带词典，并规范化大小写、全角字符和带数字的字母编号。OpenSearch 用 `whitespace` 读词项，用精确字段存编号。原片段仍用于向量化、展示和引用。
- 纯编号在关键词和混合模式使用精确编号路径。编号带正文时，关键词路匹配词项或编号并对精确编号加分，向量仍接收原问题。OpenSearch RRF 和同次诊断继续服务混合查询。
- `documents.prepare_result_nodes` 统一 SDK、Worker 和分块预览的片段准备，两种存储各自写入同一核心产出的词项和来源。
- [对照证据](evidence/jieba-retrieval.json)使用真实 OpenSearch 3.3.2。13 道公开中文题的前五证据组覆盖为旧规则 12/13、Jieba 11/13。单独的 7 道合成编号及权限用例为旧规则 5/7、Jieba 加编号 7/7。对照仅覆盖关键词路和小样延迟。默认保留旧规则，方便在具体知识库候选上复验。

## 验证

`tests/test_search_backend.py`包含类型过滤与范围反例，以及显式启用的真实OpenSearch集成。2026-11-09在本机专用Docker OpenSearch上实际通过：写入、三种查询模式、前置过滤、冻结后拒写、原生reindex、增量替换和旧发布保持不变。该测试使用确定性MockEmbedding隔离索引一致性，不证明真实模型召回效果、容量、HA、灾备或生产部署准入。

同日另在真实TEI容器验证固定分词器的中文、emoji和英文token ID一致；长三类正文生成9个512维向量、片段长度均合规且尾部标记保持，33条批输入完整返回，超长前缀查询明确拒绝。此结果证明该调用边界和样本完整性，不代表召回质量或容量验收。`tests/test_platform_embedding.py`另覆盖上游坏结构/缺项/错维度/全零向量的依赖错误语义。

```sh
INKRAG_SEARCH_INTEGRATION=1 INKRAG_OPENSEARCH_TEST_URL=http://127.0.0.1:9200 \
  uv run pytest tests/test_search_backend.py
```

参考官方机制：[写屏障](https://docs.opensearch.org/latest/api-reference/index-apis/blocks/)、[Reindex](https://docs.opensearch.org/latest/api-reference/document-apis/reindex/)、[RRF](https://docs.opensearch.org/latest/search-plugins/search-pipelines/score-ranker-processor/)。本机开发可单节点且replicas=0；生产副本、认证TLS、容量和可用区布局由部署配置及实测验收决定。
