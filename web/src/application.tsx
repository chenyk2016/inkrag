/** 总工作台是全局入口；路由明确区分所有集合与单个知识库。 */
import {
  createContext,
  useEffect,
  lazy,
  Suspense,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  createBrowserRouter,
  Link,
  NavLink,
  Outlet,
  RouterProvider,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Alert, Button, Input, Modal, Select, Tag } from "antd";
import {
  Blocks,
  BookOpen,
  FileStack,
  House,
  ListTodo,
  LogOut,
  Plus,
  Search,
  Settings2,
} from "lucide-react";
import type { Capabilities } from "./api";
import { platformClient, type PlatformClient, type Page } from "./platform-api";
import { ReviewScroll } from "./review-scroll";
import { Settings } from "./settings";
const Library = lazy(() => import("./library-route"));
interface Context {
  api: PlatformClient;
  capabilities: Capabilities;
  token: () => string;
  disconnect: () => void;
}
const WorkspaceContext = createContext<Context | null>(null);
/** 只有已鉴权应用提供控制客户端，身份变化时整棵缓存隔离。 */
export function useWorkspace(): Context {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("工作空间尚未连接");
  return value;
}
/** 所有错误保留失败语义和可重试入口，未知不会当成零数据。 */
export function Failure({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => void;
}) {
  return (
    <Alert
      type="error"
      showIcon
      title={error instanceof Error ? error.message : "操作未完成"}
      action={
        retry ? (
          <Button danger size="small" onClick={retry}>
            重试
          </Button>
        ) : undefined
      }
    />
  );
}
/** 全量总数与页内数量分开显示，前后翻页均保留当前筛选。 */
export function Pagination({
  page,
  offset,
  onOffset,
}: {
  page: Page<unknown>;
  offset: number;
  onOffset: (value: number) => void;
}) {
  return (
    <footer className="pagination">
      <span>
        共 {page.total} 项 · 显示 {page.total ? offset + 1 : 0}–
        {Math.min(offset + page.items.length, page.total)}
      </span>
      <Button
        type="default"
        disabled={offset === 0}
        onClick={() => onOffset(Math.max(0, offset - page.limit))}
      >
        上一页
      </Button>
      <Button
        type="default"
        disabled={offset + page.items.length >= page.total}
        onClick={() => onOffset(offset + page.limit)}
      >
        下一页
      </Button>
    </footer>
  );
}
/** 系统级外壳只有一个；内容视口与侧栏各自有界，不让整页跟随长内容滚动。 */
function Shell() {
  const navigate = useNavigate();
  const { capabilities } = useWorkspace();
  const parseOnly = capabilities.mode === "parse";
  return (
    <div className="product-shell">
      <aside className="product-sidebar">
        <Link to="/" className="product-brand">
          <span>
            <Blocks size={22} />
          </span>
          <div>
            {parseOnly ? "InkParse" : "InkRAG"}
            <small>资料与知识工作空间</small>
          </div>
        </Link>
        <div className="scope-label">工作空间 · 全局</div>
        <nav aria-label="工作空间导航">
          <NavLink to="/" end>
            <House size={18} />
            总工作台
          </NavLink>
          {parseOnly ? (
            <NavLink to="/parsing">
              <FileStack size={18} />
              解析集合
            </NavLink>
          ) : (
            <NavLink to="/knowledge">
              <BookOpen size={18} />
              知识库
            </NavLink>
          )}
          <NavLink to="/tasks">
            <ListTodo size={18} />
            任务中心
          </NavLink>
          <NavLink to="/settings">
            <Settings2 size={18} />
            连接与能力
          </NavLink>
        </nav>
        <div className="sidebar-foot">
          <Tag color="green">服务已连接</Tag>
          <small>当前工作空间内所有有权资料</small>
          <Button
            type="text"
            icon={<LogOut size={15} />}
            onClick={() => navigate("/logout")}
          >
            退出登录
          </Button>
        </div>
      </aside>
      <main className="product-main" id="main-content">
        <Outlet />
      </main>
    </div>
  );
}
/** 页面主体按容器高度滚动，标题、导航及分页始终可到达。 */
export function PageFrame({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="route-page">
      <header className="route-heading">
        <div>
          <h1>{title}</h1>
          <p>{description}</p>
        </div>
        <div className="heading-actions">{actions}</div>
      </header>
      {children}
    </section>
  );
}
/** 用户新建集合即进入该集合，解析集合不会被误当成在线服务。 */
function CreateCollection({ kind }: { kind: "knowledge" | "parse" }) {
  const { api, capabilities } = useWorkspace(),
    navigate = useNavigate();
  const cache = useQueryClient();
  const [open, setOpen] = useState(false),
    [name, setName] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  if (!capabilities.roles.some((r) => r === "admin" || r === "editor"))
    return null;
  const label = kind === "knowledge" ? "知识库" : "解析集合";
  return (
    <>
      <Button
        type="primary"
        icon={<Plus size={16} />}
        onClick={() => setOpen(true)}
      >
        新建{label}
      </Button>
      <Modal
        open={open}
        title={`新建${label}`}
        width={450}
        footer={null}
        onCancel={() => {
          if (!busy) setOpen(false);
        }}
        closable={!busy}
        mask={{ closable: !busy }}
        keyboard={!busy}
      >
        <p>
          {kind === "knowledge"
            ? "先确定资料用途，再在库内导入、审校与发布。"
            : "独立保存原件与解析版本，可导出或复用到知识库。"}
        </p>
        <form
          className="stack-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (busy) return;
            setBusy(true);
            setError(null);
            void api
              .createKnowledgeBase(name.trim(), kind)
              .then(async (kb) => {
                await cache.invalidateQueries({ queryKey: ["overview"] });
                await cache.invalidateQueries({ queryKey: ["collections"] });
                setOpen(false);
                navigate(`/knowledge/${kb.kb_id}/documents`);
              })
              .catch(setError)
              .finally(() => setBusy(false));
          }}
        >
          <label>
            名称
            <Input
              autoFocus
              disabled={busy}
              required
              maxLength={128}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={`例如：${kind === "knowledge" ? "售后服务手册" : "合同解析"}`}
            />
          </label>
          {error !== null && <Failure error={error} />}
          <div className="dialog-actions">
            <Button disabled={busy} onClick={() => setOpen(false)}>
              取消
            </Button>
            <Button
              type="primary"
              htmlType="submit"
              loading={busy}
              disabled={!name.trim()}
            >
              创建并进入
            </Button>
          </div>
        </form>
      </Modal>
    </>
  );
}
/** 工作台优先提示需要处理的事项，再提供知识库入口。 */
function Dashboard() {
  const { api, capabilities } = useWorkspace();
  const parseOnly = capabilities.mode === "parse";
  const kind = parseOnly ? "parse" : "knowledge";
  const collectionPath = parseOnly ? "/parsing" : "/knowledge";
  const overview = useQuery({
    queryKey: ["overview"],
    queryFn: ({ signal }) => api.overview(signal),
    refetchInterval: 10000,
  });
  const collections = useQuery({
    queryKey: ["collections", kind, "", 0],
    queryFn: ({ signal }) => api.collections(kind, "", 0, signal),
  });
  const next = collections.data?.items.find(
    (item) => item.unresolved_count > 0,
  );
  return (
    <PageFrame
      title="总工作台"
      description={
        parseOnly
          ? "检查资料并导出解析结果。"
          : "管理资料，让本次变化在知识库中生效。"
      }
      actions={<CreateCollection kind={kind} />}
    >
      <ReviewScroll label="总工作台内容">
        <div className="dashboard-body task-dashboard">
          {overview.isError && (
            <Failure
              error={overview.error}
              retry={() => void overview.refetch()}
            />
          )}
          <section className="task-dashboard-next">
            <div>
              <h2>
                {overview.data?.failed_count
                  ? "有任务需要处理"
                  : next
                    ? "有资料待检查"
                    : parseOnly
                      ? "开始处理资料"
                      : "继续维护知识库"}
              </h2>
              <p>
                {overview.data?.failed_count
                  ? `${overview.data.failed_count} 项任务失败，可查看原因并恢复。`
                  : next
                    ? next.knowledge_base.name
                    : parseOnly
                      ? "进入解析集合，添加文件或继续审校。"
                      : "进入知识库，添加资料或试问已生效的知识。"}
              </p>
            </div>
            <Link
              className="ant-btn ant-btn-primary"
              to={
                overview.data?.failed_count
                  ? "/tasks?status=failed"
                  : next
                    ? `/knowledge/${next.knowledge_base.kb_id}/documents`
                    : collectionPath
              }
            >
              {overview.data?.failed_count
                ? "查看失败任务"
                : next
                  ? "检查资料"
                  : parseOnly
                    ? "进入解析集合"
                    : "进入知识库"}
            </Link>
          </section>
          <section aria-label={parseOnly ? "解析集合" : "知识库"}>
            <div className="task-dashboard-heading">
              <h2>{parseOnly ? "解析集合" : "知识库"}</h2>
              <Link to={collectionPath}>查看全部</Link>
            </div>
            {collections.isPending ? (
              <p role="status">正在读取…</p>
            ) : collections.isError ? (
              <Failure
                error={collections.error}
                retry={() => void collections.refetch()}
              />
            ) : collections.data.items.length === 0 ? (
              <p>还没有集合。新建后即可添加资料。</p>
            ) : (
              <div className="collection-grid">
                {collections.data.items
                  .slice(0, 6)
                  .map(({ knowledge_base: kb, document_count }) => (
                    <Link
                      className="collection-card"
                      key={kb.kb_id}
                      to={`/knowledge/${kb.kb_id}/documents`}
                    >
                      <h3>{kb.name}</h3>
                      <span>
                        {document_count} 份资料
                        {parseOnly
                          ? ""
                          : kb.active_release_id
                            ? " · 已生效"
                            : " · 尚未生效"}
                      </span>
                    </Link>
                  ))}
              </div>
            )}
          </section>
          <RecentTasks />
        </div>
      </ReviewScroll>
    </PageFrame>
  );
}
/** 首页任务摘要仍保留明确归属，不混用当前库的名称。 */
function RecentTasks() {
  const { api } = useWorkspace();
  const rows = useQuery({
    queryKey: ["recent-tasks"],
    queryFn: ({ signal }) => api.taskPage({ limit: 6 }, signal),
    refetchInterval: 5000,
  });
  return (
    <section className="surface">
      <h2>最近任务</h2>
      {rows.isError ? (
        <Failure error={rows.error} />
      ) : rows.data ? (
        <TaskTable items={rows.data.items} />
      ) : (
        <p role="status">正在读取任务…</p>
      )}
    </section>
  );
}
/** 两种集合共享原件与解析协议，导航和后续能力按 kind 明确区分。 */
function Collections({ kind }: { kind: "knowledge" | "parse" }) {
  const { api } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const search = params.get("q") ?? "",
    offset = Number(params.get("offset") ?? 0) || 0;
  const [input, setInput] = useState(search);
  const result = useQuery({
    queryKey: ["collections", kind, search, offset],
    queryFn: ({ signal }) => api.collections(kind, search, offset, signal),
    refetchInterval: 15000,
  });
  return (
    <PageFrame
      title={kind === "knowledge" ? "知识库" : "独立解析"}
      description={
        kind === "knowledge"
          ? "在知识库内管理资料、审校版本和检索服务。"
          : "无需知识库或模型，独立解析、对照修订并导出；已保存结果可复用。"
      }
      actions={<CreateCollection kind={kind} />}
    >
      <form
        className="filter-bar"
        onSubmit={(e) => {
          e.preventDefault();
          setParams({ q: input });
        }}
      >
        <Input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="按名称搜索全部集合"
          aria-label="搜索集合"
          prefix={<Search size={16} />}
        />
        <Button htmlType="submit" type="default">
          搜索
        </Button>
      </form>
      {result.isError ? (
        <Failure error={result.error} retry={() => void result.refetch()} />
      ) : result.isPending ? (
        <p role="status">正在读取…</p>
      ) : (
        <>
          <ReviewScroll
            label={kind === "knowledge" ? "全部知识库" : "全部解析集合"}
          >
            <div className="collection-grid">
              {result.data.items.map(
                ({
                  knowledge_base: kb,
                  document_count,
                  selected_count,
                  unresolved_count,
                }) => (
                  <Link
                    to={`/knowledge/${kb.kb_id}/documents`}
                    className="collection-card"
                    key={kb.kb_id}
                  >
                    <div className="collection-icon">
                      {kind === "knowledge" ? <BookOpen /> : <FileStack />}
                    </div>
                    <h2>{kb.name}</h2>
                    <Tag color={kb.active_release_id ? "green" : "default"}>
                      {kind === "parse"
                        ? "独立解析"
                        : kb.active_release_id
                          ? "已发布"
                          : "尚未发布"}
                    </Tag>
                    <div className="collection-counts">
                      <span>{document_count} 份资料</span>
                      {kind === "knowledge" && (
                        <span>{selected_count} 份已选用</span>
                      )}
                      <span>{unresolved_count} 项待处理</span>
                    </div>
                    <span className="card-link">
                      打开{kind === "knowledge" ? "知识库" : "解析集合"} →
                    </span>
                  </Link>
                ),
              )}
            </div>
            {!result.data.total && (
              <div className="route-empty">
                <h2>{search ? "没有匹配的集合" : "从第一份资料开始"}</h2>
                <p>
                  {search
                    ? "调整名称后重新搜索。"
                    : "新建集合后导入文件，后台解析完成后可以对照审校。"}
                </p>
                <CreateCollection kind={kind} />
              </div>
            )}
          </ReviewScroll>
          <Pagination
            page={result.data}
            offset={offset}
            onOffset={(value) =>
              setParams({ q: search, offset: String(value) })
            }
          />
        </>
      )}
    </PageFrame>
  );
}
/** 任务终态与真实阶段分别展示，失败可读且能回到正确资料。 */
function TaskTable({
  items,
}: {
  items: Awaited<ReturnType<PlatformClient["taskPage"]>>["items"];
}) {
  const navigate = useNavigate();
  return items.length ? (
    <table className="product-table">
      <thead>
        <tr>
          <th>任务 / 资料</th>
          <th>归属</th>
          <th>状态与阶段</th>
          <th>更新时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        {items.map(({ job, kb_name, collection_kind, document_name }) => (
          <tr key={job.job_id}>
            <td>
              <strong>
                {document_name ??
                  (job.kind === "build" ? "构建检索候选" : "质量评测")}
              </strong>
              <small>
                {job.kind} · 尝试 {job.attempt}
              </small>
              {job.error_code && (
                <span className="task-error">
                  {typeof job.error_diagnostic?.message === "string"
                    ? job.error_diagnostic.message
                    : job.error_code}
                </span>
              )}
            </td>
            <td>
              <Tag color={collection_kind === "parse" ? "purple" : "blue"}>
                {collection_kind === "parse" ? "独立解析" : "知识库"}
              </Tag>
              <p>{kb_name}</p>
            </td>
            <td>
              <Tag
                color={
                  job.status === "failed"
                    ? "red"
                    : job.status === "succeeded"
                      ? "green"
                      : "default"
                }
              >
                {statusLabel(job.status)}
              </Tag>
              <small>{job.stage}</small>
            </td>
            <td>{new Date(job.updated_at).toLocaleString()}</td>
            <td>
              <Button
                size="small"
                onClick={() =>
                  navigate(
                    `/knowledge/${job.kb_id}/${job.document_id ? "documents?doc=" + job.document_id : "operations"}`,
                  )
                }
              >
                {job.document_id ? "打开资料" : "查看任务"}
              </Button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  ) : (
    <p className="empty-copy">没有匹配的任务。</p>
  );
}
/** 队列状态来自后台事实，不用虚构进度百分比。 */
export function statusLabel(status: string): string {
  return (
    {
      queued: "排队中",
      running: "执行中",
      succeeded: "已完成",
      failed: "失败",
      cancelled: "已取消",
    }[status] ?? status
  );
}
/** 大量知识库也能按名称分页选择，不能只把前30库当成所有范围。 */
function TaskCollectionFilter({
  onChoose,
}: {
  onChoose: (id: string) => void;
}) {
  const { api } = useWorkspace();
  const [open, setOpen] = useState(false),
    [search, setSearch] = useState(""),
    [offset, setOffset] = useState(0);
  const rows = useQuery({
    queryKey: ["task-collection-filter", search, offset],
    queryFn: ({ signal }) => api.collections("all", search, offset, signal),
    enabled: open,
  });
  return (
    <>
      <Button onClick={() => setOpen(true)}>按知识库或集合筛选</Button>
      <Modal
        open={open}
        onCancel={() => setOpen(false)}
        title="选择任务归属"
        width={520}
        footer={null}
        styles={{
          body: {
            maxHeight: "calc(100dvh - 200px)",
            overflowY: "auto",
            overscrollBehavior: "contain",
          },
        }}
      >
        <p>搜索全部有权知识库与独立解析集合。</p>
        <Input
          aria-label="搜索任务所属集合"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setOffset(0);
          }}
        />
        {rows.isError ? (
          <Failure error={rows.error} />
        ) : rows.data ? (
          <>
            <div className="task-collection-options">
              {rows.data.items.map((row) => (
                <Button
                  key={row.knowledge_base.kb_id}
                  type="default"
                  onClick={() => {
                    onChoose(row.knowledge_base.kb_id);
                    setOpen(false);
                  }}
                >
                  {row.knowledge_base.name}
                  <Tag>
                    {row.knowledge_base.kind === "parse"
                      ? "独立解析"
                      : "知识库"}
                  </Tag>
                </Button>
              ))}
            </div>
            <Pagination page={rows.data} offset={offset} onOffset={setOffset} />
          </>
        ) : (
          <p role="status">正在读取…</p>
        )}
      </Modal>
    </>
  );
}
/** 全局任务筛选完全传给服务端；返回总入口会自然取消 KB 限定。 */
function Tasks() {
  const { api } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "all",
    kind = params.get("kind") ?? "all",
    kbId = params.get("kb_id") ?? "",
    taskType = params.get("task_type") ?? "all",
    offset = Number(params.get("offset") ?? 0) || 0;
  const rows = useQuery({
    queryKey: ["tasks", status, kind, kbId, taskType, offset],
    queryFn: ({ signal }) =>
      api.taskPage(
        {
          status: status === "all" ? undefined : status,
          collection_kind: kind,
          kb_id: kbId,
          kind: taskType === "all" ? undefined : taskType,
          offset,
        },
        signal,
      ),
    refetchInterval: 5000,
  });
  return (
    <PageFrame
      title="任务中心"
      description={
        kbId
          ? "当前限定知识库，可清除筛选返回全部任务。"
          : "覆盖全部有权知识库与独立解析任务，关闭页面不影响后台执行。"
      }
    >
      <div className="filter-bar">
        <TaskCollectionFilter
          onChoose={(id) =>
            setParams({ status, kind: "all", task_type: taskType, kb_id: id })
          }
        />
        <Select
          value={taskType}
          onChange={(value) =>
            setParams({
              ...Object.fromEntries(params),
              task_type: value,
              offset: "0",
            })
          }
          aria-label="任务类型"
          options={[
            ["all", "所有任务类型"],
            ["parse", "文档解析"],
            ["build", "候选构建"],
            ["ingest", "版本选用"],
            ["evaluate", "质量评测"],
            ["delete", "资料撤销"],
          ].map(([value, label]) => ({ value, label }))}
        />
        <Select
          value={status}
          onChange={(value) =>
            setParams({
              ...Object.fromEntries(params),
              status: value,
              offset: "0",
            })
          }
          aria-label="任务状态"
          options={[
            ["all", "全部状态"],
            ["queued", "排队中"],
            ["running", "执行中"],
            ["failed", "失败"],
            ["succeeded", "已完成"],
            ["cancelled", "已取消"],
          ].map(([value, label]) => ({ value, label }))}
        />
        <Select
          value={kind}
          aria-label="任务归属"
          onChange={(value) =>
            setParams({ status, kind: value, task_type: taskType })
          }
          options={[
            { value: "all", label: "所有归属" },
            { value: "knowledge", label: "知识库" },
            { value: "parse", label: "独立解析" },
          ]}
        />
        {kbId && (
          <Button type="default" onClick={() => setParams({})}>
            清除知识库筛选
          </Button>
        )}
      </div>
      {rows.isError ? (
        <Failure error={rows.error} retry={() => void rows.refetch()} />
      ) : rows.isPending ? (
        <p role="status">正在读取…</p>
      ) : (
        <>
          <ReviewScroll label="全部任务列表">
            <TaskTable items={rows.data.items} />
          </ReviewScroll>
          <Pagination
            page={rows.data}
            offset={offset}
            onOffset={(value) =>
              setParams({
                ...Object.fromEntries(params),
                offset: String(value),
              })
            }
          />
        </>
      )}
    </PageFrame>
  );
}
/** 连接能力页复用真实状态查询，仅外壳不同。 */
function Connection() {
  const { api, capabilities } = useWorkspace();
  return (
    <PageFrame title="连接与能力" description="服务报告的当前能力与限制。">
      <ReviewScroll label="连接与能力内容">
        <Settings api={api} capabilities={capabilities} />
      </ReviewScroll>
    </PageFrame>
  );
}
/** 未知路由给恢复入口，不静默跳到另一个库。 */
function Missing() {
  const navigate = useNavigate();
  return (
    <PageFrame title="页面不存在" description="链接可能已失效。">
      <Button onClick={() => navigate("/")}>返回总工作台</Button>
    </PageFrame>
  );
}
/** 库路由以稳定 ID 取事实，深链不要求资料已经加载在第一页。 */
function LibraryRoute() {
  const { kbId } = useParams();
  return (
    <Suspense fallback={<p role="status">正在打开知识库…</p>}>
      <Library key={kbId} />
    </Suspense>
  );
}
/** 退出也先经过路由离开保护，避免直接卸载内存草稿。 */
function Logout() {
  const { disconnect } = useWorkspace();
  useEffect(() => disconnect(), [disconnect]);
  return <p role="status">正在退出…</p>;
}
/** 登录回调完成后创建路由，避免捕获认证前 callback 路径。 */
function createAppRouter() {
  return createBrowserRouter([
    {
      element: <Shell />,
      errorElement: <Missing />,
      children: [
        { index: true, element: <Dashboard /> },
        { path: "knowledge", element: <Collections kind="knowledge" /> },
        { path: "parsing", element: <Collections kind="parse" /> },
        { path: "tasks", element: <Tasks /> },
        { path: "settings", element: <Connection /> },
        { path: "logout", element: <Logout /> },
        { path: "knowledge/:kbId/:view?", element: <LibraryRoute /> },
        { path: "*", element: <Missing /> },
      ],
    },
  ]);
}
/** Query 缓存与路由仅服务当前可信身份；token 续期不重挂载正在编辑的文档。 */
export function Application({
  token,
  capabilities,
  onDisconnect,
}: {
  token: string;
  capabilities: Capabilities;
  onDisconnect: () => void;
}) {
  const [router] = useState(createAppRouter);
  const tokenRef = useRef(token);
  tokenRef.current = token;
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: 5000, retry: 1, refetchOnWindowFocus: false },
        },
      }),
  );
  const value = useMemo(
    () => ({
      api: platformClient(() => tokenRef.current),
      token: () => tokenRef.current,
      capabilities,
      disconnect: onDisconnect,
    }),
    [capabilities, onDisconnect],
  );
  return (
    <QueryClientProvider client={queryClient}>
      <WorkspaceContext value={value}>
        <RouterProvider router={router} />
      </WorkspaceContext>
    </QueryClientProvider>
  );
}
