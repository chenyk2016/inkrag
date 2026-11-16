/** 展示持久异步任务、条件发布和可撤销的调用凭证。 */
import { Button, Input, App, Table, Tag, Alert } from "antd";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { RefreshCw, KeyRound } from "lucide-react";
import { PublicationDialog } from "./publication-dialog";
import { EvaluationPane } from "./evaluation";
import type { Audit, Build, Caller, Client, Job, Release } from "./api";
/** 并行读取无依赖事实；失败保留错误且不把未知当作空状态。 */
export function Management({
  api,
  kbId,
  canEdit,
  canAdmin,
  parseOnly,
  onOpen,
  onError,
  section = "all",
}: {
  section?: "all" | "operations" | "integrations" | "evaluation";
  api: Client;
  kbId: string;
  canEdit: boolean;
  canAdmin: boolean;
  parseOnly: boolean;
  onOpen: (id: string) => void;
  onError: (e: unknown) => void;
}) {
  const { modal } = App.useApp();
  const [jobs, setJobs] = useState<Job[]>([]),
    [builds, setBuilds] = useState<Build[]>([]),
    [releases, setReleases] = useState<Release[]>([]),
    [callers, setCallers] = useState<Caller[]>([]),
    [audit, setAudit] = useState<Audit[]>([]),
    [currentRelease, setCurrentRelease] = useState<string | null>(null),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(true),
    [name, setName] = useState(""),
    [issued, setIssued] = useState("");
  const visibleJobs = parseOnly
    ? jobs.filter((job) => job.kind === "parse")
    : jobs;
  const refresh = useCallback(async () => {
    if (parseOnly) {
      setJobs(await api.jobs());
      return;
    }
    const [j, b, r, k, a, kbs] = await Promise.all([
      api.jobs(),
      api.builds(),
      api.releases(),
      canAdmin ? api.callers() : Promise.resolve([]),
      canAdmin ? api.audit() : Promise.resolve([]),
      api.knowledgeBases(),
    ]);
    setJobs(j);
    setBuilds(b);
    setReleases(r);
    setCallers(k);
    setAudit(a);
    setCurrentRelease(
      kbs.find((kb) => kb.kb_id === kbId)?.active_release_id ?? null,
    );
  }, [api, canAdmin, kbId, parseOnly]);
  useEffect(() => {
    let active = true;
    const run = () => {
      void refresh()
        .catch((e: unknown) => {
          if (active) onError(e);
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    };
    run();
    const timer = window.setInterval(run, 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [refresh, onError]);
  /** 动作均等待服务响应；发布失败不会预先更改当前版本。 */
  async function action(work?: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    try {
      if (work) await work();
      await refresh();
    } catch (e: unknown) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }
  /** 确认框等待服务结果，失败时保持打开，避免把失败呈现为已完成。 */
  function confirmAction({
    title,
    content,
    okText,
    work,
    danger = false,
  }: {
    title: string;
    content: string;
    okText: string;
    work: () => Promise<unknown>;
    danger?: boolean;
  }): void {
    const dialog = modal.confirm({
      title,
      content,
      okText,
      cancelText: "取消",
      okButtonProps: { danger },
      onOk: async () => {
        setBusy(true);
        let mutationSucceeded = false;
        try {
          await work();
          mutationSucceeded = true;
          await refresh();
        } catch (reason: unknown) {
          onError(reason);
          // 服务已确认写入后，列表刷新失败不能引导用户重复执行写入。
          if (mutationSucceeded) return;
          dialog.update({
            content: (
              <div>
                <p>{content}</p>
                <Alert
                  type="error"
                  showIcon
                  title={
                    reason instanceof Error
                      ? reason.message
                      : "操作未完成，请重试。"
                  }
                />
              </div>
            ),
          });
          throw reason;
        } finally {
          setBusy(false);
        }
      },
    });
  }
  /** 密钥仅本次展示，后续列表无法重新读取。 */
  async function createKey(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy || !name.trim()) return;
    await action(async () => {
      setIssued(await api.issueCaller(name.trim()));
      setName("");
    });
  }
  return (
    <div className="management">
      <div className="section-intro">
        <div>
          <h2>
            {parseOnly
              ? "解析任务"
              : section === "integrations"
                ? "接口与调用方"
                : section === "evaluation"
                  ? "线上版本评测"
                  : "版本与发布"}
          </h2>
          <p>
            {parseOnly
              ? "关闭页面不会停止解析。可查看真实阶段、取消任务，或在检查失败原因后重试。"
              : section === "integrations"
                ? "凭证仅允许读取本库已发布服务，创建后请妥善保存。"
                : section === "evaluation"
                  ? "维护线上评测样本，按实际发布版本查看历史结果。"
                  : "自动生效的资料由后台更新；手动构建的候选需审阅后发布。"}
          </p>
        </div>
        <Button
          loading={busy}
          icon={<RefreshCw size={15} />}
          onClick={() => void action()}
        >
          刷新
        </Button>
      </div>
      {(section === "all" || section === "operations") && (
        <section className="settings-card">
          <h3>{parseOnly ? "解析任务记录" : "后台任务"}</h3>
          <div className="management-table">
            <Table<Job>
              rowKey="job_id"
              dataSource={visibleJobs}
              loading={loading}
              size="small"
              pagination={false}
              scroll={{ x: 760 }}
              locale={{
                emptyText: "还没有任务。导入资料后将在这里显示真实状态。",
              }}
              columns={[
                {
                  title: "任务",
                  key: "kind",
                  render: (_value: unknown, job: Job) => (
                    <div>
                      <strong>{parseOnly ? "文档解析" : job.kind}</strong>
                      <small title={job.job_id}>
                        {job.job_id.slice(0, 12)}
                      </small>
                    </div>
                  ),
                },
                {
                  title: "状态 / 阶段",
                  key: "status",
                  render: (_value: unknown, job: Job) => (
                    <div>
                      <Tag
                        color={
                          job.status === "failed"
                            ? "error"
                            : job.status === "succeeded"
                              ? "success"
                              : ["queued", "running", "retry_wait"].includes(
                                    job.status,
                                  )
                                ? "processing"
                                : "default"
                        }
                      >
                        {job.status}
                      </Tag>
                      <small>
                        {typeof job.error_diagnostic?.message === "string"
                          ? job.error_diagnostic.message
                          : job.error_code === "build_budget_exceeded"
                            ? "构建超过预算，请拆分知识库或评估扩容"
                            : (job.error_code ?? job.stage)}
                      </small>
                    </div>
                  ),
                },
                {
                  title: "尝试",
                  dataIndex: "attempt",
                  key: "attempt",
                  width: 60,
                },
                {
                  title: "最近更新",
                  key: "updated_at",
                  render: (_value: unknown, job: Job) =>
                    new Date(job.updated_at).toLocaleString(),
                },
                {
                  title: "操作",
                  key: "actions",
                  render: (_value: unknown, job: Job) => (
                    <div>
                      {job.document_id && (
                        <Button
                          type="link"
                          size="small"
                          onClick={() => onOpen(job.document_id ?? "")}
                        >
                          资料
                        </Button>
                      )}
                      {canEdit &&
                        ["queued", "running", "retry_wait"].includes(
                          job.status,
                        ) && (
                          <Button
                            type="link"
                            size="small"
                            disabled={busy}
                            onClick={() =>
                              void action(() =>
                                api.jobAction(job.job_id, "cancel"),
                              )
                            }
                          >
                            取消
                          </Button>
                        )}
                      {canEdit &&
                        job.status === "failed" &&
                        job.error_diagnostic?.retryable === true && (
                          <Button
                            type="link"
                            size="small"
                            disabled={busy}
                            onClick={() =>
                              void action(() =>
                                api.jobAction(job.job_id, "retry"),
                              )
                            }
                          >
                            重试
                          </Button>
                        )}
                    </div>
                  ),
                },
              ]}
            />
          </div>
        </section>
      )}
      {!parseOnly && (section === "all" || section === "operations") && (
        <section className="settings-card">
          <h3>构建与发布</h3>
          {canEdit && (
            <Button
              disabled={busy}
              onClick={() => void action(() => api.rebuild())}
            >
              重新构建知识库
            </Button>
          )}
          <p>
            当前发布：<code>{currentRelease?.slice(0, 16) ?? "尚未发布"}</code>
          </p>
          {builds.map((b) => (
            <div className="management-row" key={b.build_id}>
              <div>
                <strong>{b.build_id.slice(0, 16)}</strong>
                <small>
                  {b.status} · {new Date(b.created_at).toLocaleString()}
                </small>
              </div>
              {canEdit && (
                <PublicationDialog
                  api={api}
                  buildId={b.build_id}
                  disabled={busy || b.status !== "ready"}
                  onPublished={refresh}
                />
              )}
            </div>
          ))}
          {!builds.length && (
            <p className="muted">
              上传资料后可自动生效；也可使用上方按钮构建手动候选。
            </p>
          )}
          {releases.map((r) => (
            <div className="management-row" key={r.release_id}>
              <div>
                <strong>
                  发布版本 v{r.version}
                  {r.release_id === currentRelease ? " · 当前" : ""}
                </strong>
                <small>{new Date(r.created_at).toLocaleString()}</small>
              </div>
              {canEdit && (
                <Button
                  disabled={busy || r.release_id === currentRelease}
                  onClick={() => {
                    const expectedRelease = currentRelease;
                    confirmAction({
                      title: `回滚到发布版本 v${r.version}？`,
                      content: "确认后将创建新的发布记录，并切换在线检索版本。",
                      okText: "确认回滚",
                      work: () => api.rollback(r.release_id, expectedRelease),
                    });
                  }}
                >
                  回滚到此版本
                </Button>
              )}
            </div>
          ))}
        </section>
      )}
      {!parseOnly && (section === "all" || section === "evaluation") && (
        <EvaluationPane
          api={api}
          activeReleaseId={currentRelease}
          canEdit={canEdit}
          onError={onError}
        />
      )}
      {canAdmin &&
        !parseOnly &&
        (section === "all" || section === "integrations") && (
          <section className="settings-card">
            <h3>
              <KeyRound size={17} />
              知识库调用凭证
            </h3>
            <p>凭证只能读取本知识库，原文只在创建时显示一次。</p>
            <form
              className="management-form"
              onSubmit={(e) => void createKey(e)}
            >
              <Input
                aria-label="调用方名称"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="调用方名称"
                maxLength={128}
                required
              />
              <Button
                type="primary"
                htmlType="submit"
                loading={busy}
                disabled={busy || !name.trim()}
              >
                签发凭证
              </Button>
            </form>
            {issued && (
              <div className="issued-key">
                <Alert
                  type="warning"
                  showIcon
                  title="请立即复制，此后无法回显。"
                />
                <Input.TextArea
                  readOnly
                  value={issued}
                  aria-label="一次性密钥"
                />
                <Button type="link" onClick={() => setIssued("")}>
                  已保存，隐藏
                </Button>
              </div>
            )}
            {callers.map((c) => (
              <div className="management-row" key={c.key_id}>
                <div>
                  <strong>{c.name}</strong>
                  <small>{c.revoked ? "已撤销" : "有效 · 本库只读"}</small>
                </div>
                <Button
                  danger
                  disabled={busy || c.revoked}
                  onClick={() =>
                    confirmAction({
                      title: "撤销调用凭证？",
                      content: `撤销后，“${c.name}”将无法再使用此凭证读取知识库。`,
                      okText: "撤销凭证",
                      danger: true,
                      work: () => api.revokeCaller(c.key_id),
                    })
                  }
                >
                  撤销
                </Button>
              </div>
            ))}
          </section>
        )}
      {canAdmin &&
        !parseOnly &&
        (section === "all" || section === "operations") && (
          <section className="settings-card">
            <h3>操作审计</h3>
            <div className="management-table">
              <Table<Audit>
                rowKey="event_id"
                dataSource={audit}
                size="small"
                pagination={false}
                scroll={{ x: 640 }}
                locale={{ emptyText: "暂无操作记录" }}
                columns={[
                  {
                    title: "时间",
                    key: "created_at",
                    render: (_value: unknown, event: Audit) =>
                      new Date(event.created_at).toLocaleString(),
                  },
                  { title: "操作者", dataIndex: "subject", key: "subject" },
                  { title: "动作", dataIndex: "action", key: "action" },
                  {
                    title: "目标",
                    key: "target_id",
                    render: (_value: unknown, event: Audit) => (
                      <span title={event.target_id}>
                        {event.target_id.slice(0, 20)}
                      </span>
                    ),
                  },
                ]}
              />
            </div>
          </section>
        )}
    </div>
  );
}
