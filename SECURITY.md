# 安全问题报告

请在 GitHub 登录后提交[私密漏洞报告](https://github.com/chenyk2016/inkrag/security/advisories/new)。若入口暂不可用，请向发行方索取私密渠道，并先保留复现材料。其他缺陷可在 [GitHub Issues](https://github.com/chenyk2016/inkrag/issues/new/choose) 报告。

报告请附受影响的版本、角色或前置条件、最小复现、影响及可能的修复方向。请使用合成资料和专用测试栈，移除令牌、真实文档、连接串和个人信息。部署问题请同时说明使用的是本地开发拓扑还是独立的生产拓扑。

维护者在确认影响后确定修复、验证和披露节奏。当前预发行阶段优先修复最新源码；长期维护与受支持版本将随正式发行政策公布。

本地 Compose 只应绑定环回地址。公网部署需使用独立 TLS、OIDC、最小权限和隔离解析，详见 [生产部署](docs/deployment.md)。
