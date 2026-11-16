# 发行 InkRAG

## 从源码开始

先运行 [贡献指南](../CONTRIBUTING.md#运行检查) 中的快速回归，再执行真实依赖门禁。

```sh
sh scripts/check-services.sh tests/test_platform_repository.py tests/test_search_backend.py tests/test_jieba_platform.py
```

此命令在本机启动隔离 PostgreSQL 与 OpenSearch，运行受限数据库角色和真实搜索测试。完整栈另启动带独立状态目录的应用、身份、队列、对象存储和模型。将这次开发栈的状态目录传给测试，避免写入现有资料库。

Linux 上的 OpenSearch 需要宿主的 `vm.max_map_count` 至少为 `262144`。先用 `sysctl vm.max_map_count` 检查当前值。[OpenSearch Docker 文档](https://docs.opensearch.org/latest/install-and-configure/install-opensearch/docker/)

```sh
export INKRAG_STATE_DIR="$(mktemp -d)"
uv run --locked --package inkrag-platform --extra rag inkrag up --rebuild
INKRAG_STACK_INTEGRATION=1 INKRAG_STACK_STATE_DIR="$INKRAG_STATE_DIR" \
  uv run --locked --all-packages --all-extras pytest tests/test_platform_stack.py
uv run --locked --package inkrag-platform --extra rag inkrag down
```

测试会创建合成资料库。停止后持久卷保留，可在确认属于本次临时环境时清理。Docker 宿主需要足够的内存以运行两个模型实例、搜索和工作台。

## 构建分发物

```sh
npm ci --prefix web
npm run build --prefix web
uv build --all-packages --sdist --wheel
python3 scripts/export-public-source.py --ref HEAD --output dist/inkrag-source.tar
```

检查四个 Python 包的 wheel 和 sdist 的版本、许可和资源。平台 wheel 应包含前端资源、第三方许可、迁移、Compose 和模型分词器的声明。根 Python 包的 sdist 仅收录 Python 源码和构建所需的文件。逐项审查 [公开文件清单](public-source-files.txt)。源码导出从已提交 Git 树读取清单及文件，拒绝意外符号链接、已知密钥形态，排除工作区历史和私用评测资产。首次公开仓库由该导出初始化。每次发行前，从干净目录解包，检查 README 命令和文档链接。

## 发布前确认

当前公开发行以 [GitHub 预发行版](https://github.com/chenyk2016/inkrag/releases) 为准。项目采用 [Apache-2.0](../LICENSE)，许可文本及 [NOTICE](../NOTICE) 随源码包和四个 Python 分发包交付。可从源码运行，或使用 GitHub Release 提供的一组匹配 wheel。PyPI 上的同名 `inkparse` 属于另一个项目。

- 核对源码包和 wheel 内的许可证、声明与第三方材料。[第三方说明](../THIRD_PARTY_NOTICES.md)
- 在实际发行的平台和架构上核对安装、OCI digest、软件物料清单、镜像与依赖漏洞、签名和来源。将可复核的证据与制品一起留存。
- 更新变更记录并写清兼容性、旧索引、数据库迁移、回滚及未验证范围。版本号与分发物分别核对。
- 核对 GitHub 的质量检查、私密漏洞报告入口，以及发行标签和源码包的对应关系。分别记录 PyPI 和容器镜像的发布状态。[GitHub 私密漏洞报告](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)
