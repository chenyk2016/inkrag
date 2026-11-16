# 参与 InkRAG

欢迎提交能复现的问题、测试用例和小范围改动。维护者优先审查有明确使用场景和回归证据的改动。

## 运行检查

在 Python 3.12、Node.js 24 和 uv 环境下运行：

```sh
npm ci --prefix web
uv sync --locked --all-packages --all-extras
sh scripts/check.sh
```

从 [模块导览](docs/platform-implementation.md#9-从哪里开始读代码) 找变更位置。解析规则和检索规则放在相应核心包，环境和连接由宿主装配。保留旧索引、旧解析结果和公开接口的行为。改变持久化语义时，应说明版本、迁移和回滚办法。

## 提交问题或补丁

- 描述使用入口、版本、最小复现、预期和实际行为。有任务或请求 ID 时只提供脱敏的关联信息。
- 用自行编写的示例或明确允许再分发的素材。分享前移除原件中的个人数据、凭据、企业内部内容和真实账号截图。
- 将行为改动和必要回归放在同一补丁中。涉及 PostgreSQL、搜索或后台时，附隔离集成结果及未覆盖项。
- 提交使用 Conventional Commits，例如 `fix(search): reject incomplete generations`。

安全问题走 [安全报告流程](SECURITY.md)。完整栈复测和发行步骤见 [维护者指南](docs/releasing.md)。
