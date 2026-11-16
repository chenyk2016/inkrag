# 第三方组件与素材

项目通过 Python 与 npm 锁文件记录依赖的名称和版本。分发依赖包、OCI 镜像或模型时，应保留上游许可证和适用通知，并记录实际构建出的工件清单。项目许可证只覆盖本项目有权授权的内容。

- 平台包内的 BAAI BGE 分词器工件保留 [来源和版本](packages/inkrag-platform/src/inkrag_platform/resources/bge-small-zh-v1.5/MODEL_CARD.md) 以及 [上游 MIT 许可](packages/inkrag-platform/src/inkrag_platform/resources/bge-small-zh-v1.5/LICENSE)。模型卡按上游原样保存，卡片中的相对链接可在上游仓库查看。TEI 在本地开发启动时从上游取得固定模型权重，公开源码包不附带权重。
- 构建工作台时，前端打包器生成随静态文件分发的 `third-party-licenses.md`，列出打入前端的组件及许可证。
- 工作台分发的 DOMPurify 按上游提供的 Apache-2.0 选项使用。平台 Python 包的许可证元数据包含随包分发的 BGE 工件和工作台依赖的许可。
- 内置示例由本项目用于展示与回归。[示例说明](assets/samples/README.md)
- 公开源码包通过路径白名单生成。历史本地 OCR 对比使用的论文、产品图、政务附件及其派生文本，和包含本机记录的截图、历史原型均保留在原工作区。公开包不携带这些文件或原 Git 历史。新增非自制资产前请确认可再分发权利。
