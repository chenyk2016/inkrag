# 正式工作台与解析交接协议

本协议对应 `workspace_http.py`、`workspace.py`、`quality.py`、`workspace_query.py` 与 PG 迁移 `0004_workspace` / `0005_quality_samples`。API 仍使用现有可信身份、租户/知识库范围和任务租约；请求不能自行声明租户。

## 集合与全局入口

`POST /api/v2/knowledge-bases` 在既有 `name` 外接受 `kind: "knowledge" | "parse"`，默认 `knowledge`。`parse` 是独立解析工作集合，不是可发布知识库；用户不必先创建检索知识库。集合内部仍有稳定身份，任务和资料不会成为无法授权的无主记录。

| 路径 | 行为 |
|---|---|
| `GET /api/v2/workspace` | 全部可见集合的真实总计，不从前一百项推算 |
| `GET /api/v2/workspace/knowledge-bases` | `kind=all|knowledge|parse`、`search`、`limit`、`offset`；返回集合与资料/选用/任务/未解决任务计数 |
| `GET /api/v2/workspace/jobs` | 可选 `kb_id`、`collection_kind`、`status`、`kind`、`search`、分页；每项保留原 KB 名称、集合类型与资料名称 |
| `GET /api/v2/workspace/knowledge-bases/{id}/documents` | `search`、`selected`、分页；返回完整匹配总数 |

分页响应为 `{items,total,limit,offset}`，`limit` 为 1–100。计数与同页使用同一个只读数据库快照，按创建时间和唯一身份稳定排序。知识库计数仅对当前页集合做一次聚合，不循环为每个卡片发起多个查询。并发新增后再次换页可能看到新快照，分页不是长事务快照游标。

## 审核、选用与采用

这些是不同动作：保存内容产生解析结果；审核记录人工结论；选用决定下次 RAG 构建使用什么；发布决定在线服务使用什么。

- `PUT /api/v2/documents/{id}/reviews/{result_id}`：`{expected_document_revision,status:"pending"|"approved"}`。审核人、时间、状态绑定不可变结果身份，既有版本接口返回 `review_status` / `reviewed_by` / `reviewed_at`。撤回审核同时撤回该版本的下次构建选用，当前已发布快照保持原有内容。
- `PUT /api/v2/documents/{id}/selection`：`{expected_document_revision,result_id}`，空结果表示撤回选用。选用只冻结结果身份、资料版本及元数据，不批准或覆盖人工审核。修改资料属性后可再次选用同一结果以更新冻结属性；相同结果及属性重复选用保持幂等。保存新的解析修订不会暗中改变已选版本。只解析 B、选用 A 时，下一次构建不会包含 B。
- `POST /api/v2/knowledge-bases/{id}/adoptions`：`{source_document_id,result_id,metadata:{}}`。先检查来源和目标授权，再读取明确保存结果的原件身份，目标资料独立管理；复用不可变原件、图片及解析结果，不重新解析或复制文件。同库重复采用同结果及属性返回既有资料；属性不同则明确冲突。新目标保持待审核、审核人和时间为空；来源审核事实保持原状。

资料 `revision` 是聚合比较版本，包含内容、属性与人工操作；解析 JSON 内部 `revision` 是该产物自己的修订链；历史列表 `ResultVersion.revision` 是 PG 的追加顺序。这三者不能相互替代。重新解析新原件可以得到内部修订 1，PG 历史仍递增。相同内容重解析会复用相同 `result_id`，不会重复插入不可变结果或覆盖历史。

重新解析通过 `Repository.submit_reparse` 冻结资料版本、原件及名称。新原件已入资产服务后才能接收任务；成功提交前原件指针保持旧值。成功后同一事务提交新指针、历史、任务结果。旧执行租约、被取消任务或资料已经变化的任务不能提交。

## 服务草稿、候选与发布调用

`GET/PUT /api/v2/knowledge-bases/{id}/service` 读取/保存服务草稿。第一次保存用 `expected_revision:0`；其后使用读取到的草稿版本。配置为：

```json
{
  "expected_revision": 0,
  "config": {
    "name": "内部制度检索",
    "mode": "hybrid",
    "top_k": 5,
    "fixed_filters": [],
    "allowed_fields": ["mode", "top_k", "filters"],
    "allowed_callers": []
  }
}
```

`fixed_filters` 只能使用知识库声明的字段，保存时校验操作符和值类型。动态 `filters` 与固定条件取交集，不能覆盖固定范围。`allowed_fields` 是允许调用者传入的动态字段；明确传入未开放字段即拒绝。`allowed_callers` 为空表示允许已获该 KB 授权的主体，非空时仅允许指定可信 `subject`；业务密钥主体是 `api-key:{key_id}`，仍受现有绑定、启停和撤销约束。

草稿变动推进 KB 业务版本；构建冻结当时配置与内容清单。线上调用采用发布所属构建的配置，不读当前草稿。发布旧候选需要满足当前 KB 版本、权限版本及当前发布比较条件。重复发布当前同一构建返回当前发布，不制造空版本。

| 路径 | 输入/结果 |
|---|---|
| `POST /api/v2/builds/{build}/retrieve` | 管理员/编辑者可试查就绪候选，无需先发布 |
| `POST /api/v2/knowledge-bases/{kb}/service/retrieve` | 固定当前发布并执行其冻结策略 |
| 上述两个查询输入 | `{query,mode?,top_k?,filters?}` |
| 查询附加输出 | `trace_id,build_id,target_kind,target_id,snapshot_id,effective_request,authorization_epoch` |
| `GET /api/v2/knowledge-bases/{kb}/traces` | 分页查询；管理员看本库，其他主体仅看自己 |
| `GET /api/v2/traces/{id}` | 同租户、KB、调用者授权后的单条记录 |

轨迹保存实际固定的候选/发布、有效条件、耗时及返回的资料身份，不保存凭据或命中片段全文。返回的 `trace_id` 已经成功持久化；不能把随机 ID 冒充可查轨迹。记录读取失败与零命中是不同结果。

删除投影由受信服务读取 `(authz_epoch,deleted_document_ids)`，在关键词和向量两路过滤，返回前再次核对本次 epoch。历史发布可以继续检索未撤销内容；授权发生并发变化则整次请求拒绝返回。仅实现此双通道过滤的调用方才能使用 `get_active_release(...,allow_deleted_projection=True)`。发布和回滚仍拒绝旧权限版本。

## 人工质量样本与逐题评测

`POST /api/v2/knowledge-bases/{kb}/quality-samples`：

```json
{
  "query": "第六条规定的交付日期是什么？",
  "expected_document_ids": [],
  "expected_sources": [
    {"document_id": "32位资料身份", "result_id": "64位保存结果身份", "element_ref": "#/texts/6"}
  ],
  "no_answer": false,
  "supersedes_id": null
}
```

`expected_sources` 可指定元素或页码，多个条件必须同时满足；保存时验证结果、元素和页码存在。未传结果身份时冻结当时保存结果。`no_answer=true` 必须没有正例资料或证据。正例必须有文档或具体证据。修改人工标注用 `supersedes_id`，追加新样本身份及 `annotation_revision`，不覆盖已有报告；同一旧标注被并发替换时只接受一份新版本。

`GET` 同路径读取集合，与旧 `samples` 接口共享数据。`POST /api/v2/builds/{build}/evaluations` 接收 `{name,sample_ids,top_k}` 并要求 `Idempotency-Key`，返回持久任务 202。Worker 固定构建、标注版本和授权 epoch 逐题执行检索，提交前再次核对租约；取消或失权不留下成功报告。可以对当前发布对应的构建与待发布候选分别运行相同样本，不改变在线发布。

报告在 `GET /api/v2/knowledge-bases/{kb}/candidate-evaluations` 查询。保留原生 LlamaIndex 文档级 Recall/Precision/MRR，并单列 `evidence_recall`、`no_answer_accuracy`、各类样本数量。没有该类样本时指标为 `null`。逐题包括明确保存的标注、实际来源、命中/遗漏证据与真实轨迹。命中同一文档错误条款时，文档 Recall 可以为 1，但证据 Recall 必须为 0。

无答案检查当前定义为“是否没有返回证据”，不代表生成答案正确率或模型拒答能力。原发布评测接口继续保留文档级能力；细粒度/无答案样本明确引导使用固定构建评测，不能静默忽略标注。

## 验证边界

当前 PG 回归默认在独立 `inkrag_test` 库，以受限运行账号和隔离 `PM-MIGRATION-*` 租户执行，覆盖跨百项分页、授权归属、A/B 选用、资产复用、CAS 审核、原件替换、服务冻结、轨迹隔离、人工标注校验和评测租约。策略/指标测试中注入固定搜索响应，证明协议和指标反例，不替代真实 OpenSearch 运行及产品经理体验验收。可通过 `scripts/check-platform-pg.py` 复跑本地受限应用账号用例；脚本不输出凭据。

## 2026-11-10 审查补充

服务增加 `allowed_filter_fields: string[] | null`。`null` 保持旧冻结配置语义：允许该物理版本声明的全部动态筛选字段；`[]` 关闭所有动态筛选字段；非空白名单必须唯一且属于 KB 声明。它与 `allowed_fields` 中的 `filters` 总开关同时生效。固定条件始终保留，动态条件只能追加取交集。字段声明变更也验证该白名单，不能留下失效的服务策略。

发布查询在读取当前发布时一次固定 `release_id` / `release_version`，与 `target_id`、物理索引和轨迹一起返回。查询期间 A 切换到 B，已执行的 A 请求仍标注 A；接入层不能查询结束后重新读取当前发布给旧命中贴标。

撤销投影仅接受本次冻结清单的资料身份，进入数据库前限制最多 5000 项，并在 SQL 中求清单与已删除资料的交集。只需要权限 epoch 时显式传空清单，不扫描或返回历年全部墓碑。查询返回前继续核对本次 epoch，发布和回滚的权限屏障保持不变。

`GET /api/v2/builds/{id}/preview?limit&offset` 提供发布审阅：

- `expected_release_id` 是本次只读快照的当前发布指针，确认发布必须携带它。
- `summary` 包含全部 `added` / `changed` / `removed` / `unchanged` 数量，`items` 仅包含本页对应的资料身份、当前名称及冻结的结果/版本。
- `removed` 表示当前线上有、候选没有的资料；其版本来自线上冻结清单，不取该资料的当前编辑版本。
- `service_config` 是候选冻结策略，`strategy_changed` 比较策略语义、模型修订和字段声明。
- `publishable` 与 `stale_reasons` 说明构建状态、KB 版本或授权版本是否使候选过期。

差异在 PG 的物化 CTE 中计算，汇总和分页一次返回；Python 不读取两份完整清单。双方清单均校验 5000 资料/16MiB 预算。分页按新增、更新、移除、不变及唯一资料身份稳定排序。

迁移 `0006_audit_scope` 为审计增加可信 `kb_id`。所有现有业务写入在同一事务显式记录归属；历史仅在目标身份可唯一解释时回填，歧义和全租户事件保留空归属。本库审计严格 `WHERE kb_id=...`，不会把 job、key、sample 或结果审核等非资料目标混入另一知识库。限定 KB 的管理员读全局审计时仍只能看到其绑定范围。

### 回归环境与 KB 锁

独立 PG 回归入口现默认初始化 `inkrag_test`，不再与正式开发 `inkrag` 库共享任务/outbox；两者仍在同一开发 PG 实例。测试始终使用受限运行账号。显式 `INKRAG_TEST_DATABASE_DSN` 可指向已初始化的独立测试库；可选 `INKRAG_TEST_MIGRATION_DSN` 只允许同库迁移，正式库和系统库一律拒绝作为测试目标。

KB 的不可变主键/租户键允许使用 `FOR NO KEY UPDATE` 串行化业务写入。该锁仍排斥其他业务写锁，同时兼容审计 KB 外键的 `KEY SHARE`，避免取消持有 Job 锁时因审计外键检查反向等待 KB。取消原执行时序、旧租约提交拒绝与发布 CAS 均经真实 PG 复验。

## 显式详细诊断

候选 `POST /api/v2/builds/{build}/retrieve` 可额外传 `diagnostics:true`，仅管理员/编辑者可用；当前发布服务拒绝此选项。它不是服务开放字段，不能被调用者借机扩大固定范围。问题最多1000字、Top K最多20；正常请求不启用 explain 或额外通道搜索。

使用 OpenSearch 原生 `hybrid_score_explanation` 与同次内联 RRF 流水线，响应的 `diagnostics` 原样写入该请求轨迹 `outcome.diagnostics`。仅保存最终授权命中的文档/结果/元素身份、最终排名与分数、白名单通道及数值贡献，不保存正文、向量、原始解释词项树。原生 RRF 未直接给通道排名时，只有贡献与 `1/(60+rank)` 的整数排名相互一致才展示反推结果并标明来源；未知解释保留 `unrecognized`，不猜通道。

这是本次返回 Top K 的解释，不是全部通道候选清单。过滤前数量、所有通道候选总量保持 `null`，零命中也不能据此断言在哪一阶段被过滤。历史普通轨迹不会补造解释。实现与真实负例见 诊断证据及PG轨迹关联。
