# 初始化与生产运行

当前入口为`inkrag`和`Dockerfile.platform`；根目录Dockerfile/compose.yaml链接到同一正式定义。旧`inkrag-serve`同步宿主已移除，仅保留Git历史，避免两套HTTP业务规则。

## 本地完整启动

源码目录运行`uv run --locked --package inkrag-platform --extra rag inkrag up`。初始化器依次完成私有状态、缺省真实依赖、开发realm/桶、应用镜像、独立迁移、模型/API/worker/relay和readiness。重复运行复用密码、端口、卷与realm；失败保留检查点，不重置数据。

默认PG18、Rabbit quorum、MinIO、OpenSearch、Keycloak和两个固定BGE中文TEI实例。持久卷保存事实/资产/模型；应用非root、只读根文件系统、丢弃全部capabilities，没有Docker socket和旧资料目录。解析子进程限制文件、网络、派生进程、信号、内存/CPU和输出；Linux内核不支持必要隔离时失败关闭。

宿主端口仅127.0.0.1。开发HTTP、单节点、OpenSearch开发安全配置、Keycloak start-dev都是本地拓扑约定，不能直接开放公网。生产配置拒绝不安全连接，实际HA需要外部故障域和运维方案。

本地 Keycloak 的 InkRAG 登录主题位于 [keycloak-themes/inkrag](../packages/inkrag-platform/src/inkrag_platform/resources/keycloak-themes/inkrag/login/theme.properties)。Compose 以只读方式挂载，`inkrag up` 会为新建开发域和仍使用默认主题的现有开发域启用它。主题继承 Keycloak 登录模板，外部 OIDC 的登录界面由其身份服务配置。

`.inkrag`权限700，秘密文件600并忽略Git/构建。账号在`access.json`，密码不打印；不要提交或分享该文件。首次需下载镜像/模型，网络失败修复后重试，不切换不明镜像源。可在私有`compose.env`中设置Docker可达的`MODEL_HTTP_PROXY`，不改全局代理。

`inkrag status`看状态；`inkrag up --rebuild`构建最新代码；`inkrag down`停止并保留卷。逻辑删除不代表物理擦除；默认不自动GC或删除共享对象。

## 连接与预算变量

应用只读环境，不自动读`.env`。外部URL必须在目标进程/容器内可达，容器127.0.0.1不是宿主数据库。外部桶/IAM/身份客户端需部署方准备，初始化器不改外部账户或桶策略。

| 变量 | 用途 |
|---|---|
| `INKRAG_MODE` | development / production |
| `INKRAG_CAPABILITIES` | parse / rag / combined；parse不加载搜索/模型 |
| `INKRAG_DATABASE_URL` | 运行角色，`postgresql+psycopg://...` |
| `INKRAG_MIGRATION_URL` | 迁移身份，仅给migrate，不给其它进程 |
| `INKRAG_RABBIT_URL` | Rabbit/vhost，生产amqps验证证书和主机名 |
| `INKRAG_S3_ENDPOINT/BUCKET/PREFIX` | S3/MinIO普通桶和前缀 |
| `AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN` | 标准AWS凭证；直接进程也支持SDK凭证链 |
| `AWS_DEFAULT_REGION` | S3区域 |
| `INKRAG_OPENSEARCH_URL/USER/PASSWORD` | 搜索连接，按进程提供读/构建权限 |
| `INKRAG_SEARCH_REPLICAS` | 本地0，生产至少1，跨故障域另外验收 |
| `INKRAG_EMBEDDING_URL/BATCH_EMBEDDING_URL` | 在线/批量TEI独立资源池 |
| `INKRAG_MODEL_REVISION` | 固定工件revision，必须匹配服务实际身份 |
| `INKRAG_OIDC_ISSUER/AUDIENCE/CLIENT_ID` | OIDC组织/SPA；需可信tenant_id和角色 |
| `INKRAG_OIDC_JWKS_URL` | 可选内网JWKS，否则从可信issuer标准发现 |
| `INKRAG_PARSE_MEMORY_MB/PARSE_SECONDS` | 单解析默认512MiB/60s |
| `INKRAG_WORKER_CONCURRENCY/JOB_SECONDS` | 默认1并发/3600s，须与内存/CPU一起调 |
| `INKRAG_MAX_BUILD_DOCUMENTS/MAX_MANIFEST_BYTES/MAX_BUILD_INPUT_BYTES` | 文档数最多5000，清单最多16MiB，构建读取的解析JSON默认256MiB。超额拒绝且不自动重试 |
| `INKRAG_API_MEMORY/API_CPUS` | 本地Compose默认1024m/2CPU，按语料和并发重测 |
| `INKRAG_WORKER_MEMORY/WORKER_CPUS` | 本地Compose默认3g/1CPU，需容纳宿主和解析子进程 |
| `INKRAG_APP_IMAGE` | 预构建应用镜像；生产建议固定digest并预拉取 |
| `INKRAG_STATE_DIR` | 初始化私有目录，默认当前目录.inkrag |
| `INKRAG_BIND/PORT` | 直接API默认127.0.0.1:8010，Compose内部8010 |

生产使用HTTPS、PG `sslmode=verify-full`和AMQPS。运行PG身份不能是表owner、superuser、BYPASSRLS或可建库/角色身份。需要内部CA时将可信证书正确注入各原生客户端的信任存储，不能关闭校验。

受限解析要求Linux Landlock ABI至少3，并在单线程子进程施加路径规则后再拒绝网络/进程/io_uring等内核入口；不支持时明确失败，macOS显式开发运行不算同等级隔离。推理端点须固定不可变权重/分词器工件，换模型使用新端点和新构建，不能对同一在线端点静默替换权重。

S3需支持条件写和一致性，禁止匿名读/覆盖/任意删除。平台按租户独立前缀保存资产；HTTP再核对KB授权、资料删除和版本归属。引用不能代替桶策略、版本化或备份。

## 生产进程与部署

同一镜像分别运行：

```sh
inkrag migrate  # 唯一拥有迁移凭据的发布任务
inkrag api      # 无状态，可多副本，不建表
inkrag worker   # 有界后台执行，可分池扩容
inkrag relay    # 领取outbox和回收失联任务，支持多实例
```

生产`inkrag up`要求完整外部配置、迁移URL及已拉取镜像，不创建开发依赖或临时构建。实际编排需设置副本、滚动升级、网络策略、密钥挂载和资源预算。默认Compose只用于单机开发，不能代替生产集群。

按进程注入不同最小权限：API搜索只读；构建worker拥有候选/冻结权限；迁移身份独立。本地Compose共用开发业务凭据，不冒充生产最小权限拓扑。OIDC生产使用授权码+PKCE S256和精确回调地址；开发realm的密码授权仅供本地自动验收。

## 失败和恢复

原件持久化且PG资料/job/outbox同事务提交后才返回202。S3成功而PG失败可留下未引用对象，需要对账回收；不能盲删共享资源。相同幂等键同请求复用，异请求冲突。

Relay在真实路由与publisher confirm后标已投递，未知确认保留事件。重复消息由PG lease/epoch吸收；失联worker回收后旧epoch无法提交。确定性文档错误不无限重试，临时故障有次数上限；死信不代表成功。

候选generation独立，完整bulk/reindex、后端write block、清单核对后才ready；发布是PG条件事务。资料/字段/属性变化使旧构建拒绝，旧发布保持。删除递增撤销版本；查询按当前冻结清单取得有界撤销交集并在两检索通道前置过滤，返回前复核同次授权版本。其余资料继续可查；旧授权构建不能通过发布/回滚复活删除资料。

`/health/live`只看进程，`/health/ready`探测PG/S3/当前在线搜索和模型。批量池故障不摘掉在线API。受权`/api/v2/status`显示真实就绪与模型身份。请求ID和operation/job/result/build/release提供关联，不记录令牌/正文。上传和大产物有独立并发额度，满时429+Retry-After，避免挤占普通管理/检索。

若旧部署存在已上线的超限清单，先把构建文档数和清单上限恢复到支持值。已有可用且授权版本兼容的发布时可回滚。否则取消部分资料的构建选用，使用 `POST /api/v2/knowledge-bases/{kb}/builds?full_rebuild=true` 创建不复用旧索引的候选，成功后从发布列表取当前 ID，按 `expected_release_id` 条件发布。旧清单超限时发布对比预览可能被拒绝，可走直接发布 API。取消选用保留原件。

Worker健康用Celery定向ping本容器节点，不能用同broker其它节点的回复代替；relay健康检查最近30秒的控制循环心跳。它们不继承API的HTTP探测，健康也不等于某个任务或投递已经成功。

备份覆盖PG、S3和密钥引用。搜索可从固定解析版本重建，恢复时先暂停后台、恢复事实/资产、核对迁移/摘要/清单/撤销版本，再恢复任务和搜索，最后开放流量。实际RPO/RTO须在全新环境演练，重开原卷不算灾备。

## 验证

快速层`sh scripts/check.sh`。PG/HTTP/队列故障测试必须用专用`INKRAG_TEST_DATABASE_DSN`，避免与live relay竞争。真实搜索`INKRAG_SEARCH_INTEGRATION=1`；完整本地栈`INKRAG_STACK_INTEGRATION=1`运行`tests/test_platform_stack.py`。最终Linux镜像验证隔离，macOS开发不作同等证据。

短测`uv run --locked --all-packages --all-extras python examples/benchmark_platform.py --seed 100`记录全部响应、语料、模型、并发和延迟；合成小样不是峰值容量/业务质量。长期容量、故障域、灾备和标注验收见[生产部署](deployment.md#上线前的验收)。

## 可选Docling离线解析

基础SDK只安装inkparse即可使用内置；需要第二引擎时安装inkparse[docling]，平台分发同时包含两种适配。仓库锁定Linux CPU PyTorch来源，避免普通部署安装CUDA库；通过pip安装的外部分发应按PyTorch官方CPU安装规则准备环境。

运行`uv run python scripts/prepare-docling-models.py --output .inkrag/docling-models`显式下载公开布局/表格/中文OCR工件并生成manifest摘要。不要在解析任务里下载模型。SDK设置INKPARSE_MODELS_PATH；平台设置INKRAG_PARSE_MODELS_PATH，Docker Compose把INKRAG_DOCLING_MODELS_DIR只读挂载到/opt/inkrag/docling-models。项目CLI默认使用当前state目录/docling-models；手动调用Compose时显式指定宿主模型目录绝对路径。

内置预算仍默认512MiB/60秒，Docling使用独立INKRAG_DOCLING_MEMORY_MB（默认4096）和INKRAG_DOCLING_SECONDS（默认180）。这是开发起点，不是生产容量保证。Linux隔离继续禁止网络、fork/exec和越界文件访问，允许继承隔离策略的线程型clone；模型只读。数值运行库只额外访问随机源、空设备与CPU能力信息，不开放任意proc/sys。模型缺失、解析失败或超时均不自动切换内置引擎。
