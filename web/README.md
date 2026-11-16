# InkRAG 正式工作空间

React 19 / TypeScript、Ant Design 6、React Router、TanStack Query 和 Vite。所有页面调用同源 `/api/v2`；不直连 PG、S3、搜索或模型。OIDC 授权码＋PKCE 登录，访问/刷新令牌只留内存，身份续期保留当前草稿。

Ant Design 是全站主组件库：按钮、输入、选择、页签、弹窗、提示、状态标签和管理表格直接使用其接口。`src/ui-theme.ts` 统一颜色、字号、圆角和控件高度，业务 CSS 负责工作台布局及文档内容。Radix Themes 已移除；保留独立 ScrollArea 滚动原语、react-resizable-panels 分栏与 PDF.js。文件选择、语义链接、原文/合并表格和只读诊断的 details 保留原生语义。

## 构建与运行

`npm ci && npm run check && npm run format:check && npm run build`。构建产物带内容摘要，递归写入 Python 分发包；运行不要求 Node。正式本地拓扑由仓库根 `inkrag up --rebuild` 构建并启动，变量可替换依赖；Docker 仅是单机开发拓扑。

### 上游类型声明修补

锁定的 `@rc-component/image@1.10.0` 将图片组的必填 `current` 回调直接继承单图的可选 `current` 回调，违反严格函数类型约束。`patches/@rc-component+image+1.10.0.patch` 仅对 ESM/CJS 两份声明使用 `Omit<InternalPreviewConfig, 'imageRender'>` 后保留图片组原回调签名；运行代码不变，没有使用 `skipLibCheck`、类型断言或忽略诊断。已核对上游图片组实际始终提供 `current`，较早发布版本也有同一声明问题。

`npm ci` 的 postinstall 使用 patch-package 自动应用，失败立即退出；Docker 安装前先复制补丁。升级 Ant Design 或 image 时必须重新检查上游声明：修复后删除该补丁及不再需要的工具，否则更新针对精确版本的最小补丁并重新执行完整严格检查。不要绕开安装脚本来隐藏冲突。官方依据：[图片组源码](https://github.com/react-component/image/blob/master/src/PreviewGroup.tsx)、[预览源码](https://github.com/react-component/image/blob/master/src/Preview/index.tsx)。

## 页面与数据

总工作台与任务中心覆盖所有有权知识库及独立解析；知识库内管理上传、同屏审校、版本审核/选用、候选验证、逐题质量、服务草稿、上线审阅、发布/回滚、诊断和调用凭证。独立解析无需模型，可导出 Markdown/JSON/含相对图片的 ZIP，再按保存版本复用于知识库。

名称筛选与分页在服务端执行；资料每页100条，集合/任务每页30条。原文、结果、目录拥有有界独立滚动，内嵌编辑框与表格可把边缘滚动交还所属视口。可调双栏、窄屏堆叠、专注审校与键盘操作由成熟组件提供。结果按原生阅读顺序每组40元素，表格按50行×12列分组可访问全部单元格，保留合并坐标。

PDF.js 真实绘制当前页；HTML 净化为无脚本/外链的安全快照；Office 当前为机器首版只读结构，明确不还原排版。内容修订不覆盖机器结果，审核绑定具体不可变版本。原件/图片通过带授权头的请求生成临时 Blob URL，用完释放。未保存的文字、结构、属性、字段定义和服务草稿都有离开保护。

## 验收

功能和浏览器回归范围见[发行验收](../docs/release-readiness.md)。

HTTP、字段、发布与任务协议见 [工作空间协议](../docs/workspace-http-contract.md) 和 [平台 HTTP](../docs/platform-http.md)。未验的生产 HA、灾备、长期容量与企业标注质量继续保留在总任务。
