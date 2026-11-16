/** 服务草稿、候选验证、评测和发布共用同一冻结目标，保留旧版核心能力。 */
import {
  Activity,
  useCallback,
  useEffect,
  useState,
  type FormEvent,
} from "react";
import { useSearchParams } from "react-router-dom";
import { Button, Checkbox, Input, InputNumber, Select, Tag } from "antd";
import { QualitySamples } from "./quality-samples";
import { EvaluationReport } from "./evaluation-results";
import { RequestDiagnostics } from "./request-diagnostics";
import { Management } from "./management";
import { ReviewScroll } from "./review-scroll";
import { Failure } from "./application";
import {
  object,
  type Build,
  type JsonObject,
  type SearchResult,
  type MetadataField,
} from "./api";
import type { PlatformClient, ServiceDraft, Trace } from "./platform-api";
interface Attempt {
  buildId: string;
  query: string;
  result: SearchResult;
}
/** 候选与在线版本区别可见；未发布也能试查和评测。 */
export function ServiceStudio({
  api,
  view = "trial",
  kbId,
  canEdit,
  canAdmin,
  onOpen,
  onError,
  onDirtyChange,
}: {
  view?:
    | "trial"
    | "draft"
    | "evaluation"
    | "operations"
    | "trace"
    | "integrations";
  onDirtyChange: (dirty: boolean) => void;
  api: PlatformClient;
  kbId: string;
  canEdit: boolean;
  canAdmin: boolean;
  onOpen: (id: string, resultId?: string, ref?: string) => void;
  onError: (e: unknown) => void;
}) {
  const [routeParams] = useSearchParams();
  const [diagnostics, setDiagnostics] = useState(false);
  const [traceId, setTraceId] = useState<string | null>(
    routeParams.get("trace"),
  );
  useEffect(() => setTraceId(routeParams.get("trace")), [routeParams]);
  const [availableFields, setAvailableFields] = useState<MetadataField[]>([]);
  const [trialMode, setTrialMode] = useState<"hybrid" | "vector" | "keyword">(
      "hybrid",
    ),
    [trialTopK, setTrialTopK] = useState(5);
  const [draft, setDraft] = useState<ServiceDraft | null>(null),
    [dirty, setDirty] = useState(false),
    [builds, setBuilds] = useState<Build[]>([]),
    [target, setTarget] = useState(""),
    [compareTarget, setCompareTarget] = useState(""),
    [query, setQuery] = useState(""),
    [attempts, setAttempts] = useState<Attempt[]>([]),
    [traces, setTraces] = useState<Trace[]>([]),
    [evaluations, setEvaluations] = useState<JsonObject[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState(""),
    [filterText, setFilterText] = useState("[]"),
    [callerText, setCallerText] = useState("");
  useEffect(() => {
    onDirtyChange(dirty);
    return () => onDirtyChange(false);
  }, [dirty, onDirtyChange]);
  /** 周期读取不覆盖服务草稿，动作成功才更新当前配置。 */
  const refresh = useCallback(async () => {
    const [b, t, e] = await Promise.all([
      api.builds(),
      api.traces(),
      api.candidateEvaluations(),
    ]);
    setBuilds(b);
    setTraces(t.items);
    setEvaluations(e);
    setTarget(
      (previous) =>
        previous || b.find((row) => row.status === "ready")?.build_id || "",
    );
  }, [api]);
  useEffect(() => {
    let alive = true;
    void api
      .schema()
      .then((value) => {
        if (alive) setAvailableFields(value.fields);
      })
      .catch(setError);
    void api
      .service()
      .then((value) => {
        if (alive) {
          setDraft(value);
          setFilterText(JSON.stringify(value.config.fixed_filters, null, 2));
          setCallerText(value.config.allowed_callers.join("\n"));
        }
      })
      .catch(setError);
    void refresh().catch(setError);
    const timer = window.setInterval(
      () => void refresh().catch(setError),
      5000,
    );
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [api, refresh]);
  useEffect(() => {
    /** 刷新页面同样保护尚未保存的服务策略。 */ function leave(
      event: BeforeUnloadEvent,
    ): void {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", leave);
    return () => window.removeEventListener("beforeunload", leave);
  }, [dirty]);
  /** 写操作等待结果；冲突/后台失败保留输入和旧在线服务。 */
  async function act(work: () => Promise<void>): Promise<void> {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      await work();
      await refresh();
    } catch (reason: unknown) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }
  /** JSON 编辑仅在边界解析，字段合法性与可信范围仍由后端严格判定。 */
  async function save(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!draft) return;
    await act(async () => {
      const value: unknown = JSON.parse(filterText);
      if (!Array.isArray(value)) throw new Error("固定条件应是条件数组");
      const next = await api.saveService({
        ...draft,
        config: {
          ...draft.config,
          fixed_filters: value.map(object),
          allowed_callers: callerText
            .split("\n")
            .map((item) => item.trim())
            .filter(Boolean),
        },
      });
      setDraft(next);
      setDirty(false);
      setNotice("服务草稿已保存。新策略在构建并发布后生效。");
    });
  }
  /** A/B 在同一次提交使用同一问题，目标构建身份与实际结果一并保留。 */
  async function trial(event: FormEvent): Promise<void> {
    event.preventDefault();
    await act(async () => {
      const targets = [
        target,
        ...(compareTarget && compareTarget !== target ? [compareTarget] : []),
      ];
      const results = await Promise.all(
        targets.map(async (buildId) => ({
          buildId,
          query,
          result: await api.trial(
            buildId,
            query,
            compareTarget ? { mode: trialMode, top_k: trialTopK } : undefined,
            diagnostics,
          ),
        })),
      );
      setAttempts(results);
    });
  }
  const ready = builds.filter((build) => build.status === "ready");
  return (
    <div className="service-studio">
      {error !== null && <Failure error={error} />}
      {notice && (
        <p role="status" className="message-banner">
          {notice}
        </p>
      )}
      {[
        {
          key: "trial",
          label: "候选验证",
          children: (
            <ReviewScroll label="候选验证内容">
              <>
                <div className="studio-form">
                  <h2>先验证，再发布</h2>
                  <p>只查询已冻结的候选构建。试查不会更改当前发布。</p>
                  {canEdit && (
                    <Button
                      disabled={busy || dirty}
                      onClick={() =>
                        void act(async () => {
                          const accepted = await api.rebuild();
                          setNotice("候选构建任务已提交：" + accepted.job_id);
                        })
                      }
                    >
                      用已选资料构建候选
                    </Button>
                  )}
                  {dirty && <p>请先保存服务草稿，再构建候选。</p>}
                  <form
                    onSubmit={(event) => void trial(event)}
                    className="stack-form"
                  >
                    <div className="form-columns">
                      <label>
                        A · 验证目标
                        <Select
                          aria-label="A · 验证目标"
                          value={target}
                          onChange={setTarget}
                          options={[
                            { value: "", label: "选择已完成构建" },
                            ...ready.map((build) => ({
                              value: build.build_id,
                              label: `${new Date(build.created_at).toLocaleString()} · ${build.build_id.slice(0, 8)}`,
                            })),
                          ]}
                        />
                      </label>
                      <label>
                        B · 对比目标
                        <Select
                          aria-label="B · 对比目标"
                          value={compareTarget}
                          onChange={setCompareTarget}
                          options={[
                            { value: "", label: "不比较" },
                            ...ready.map((build) => ({
                              value: build.build_id,
                              label: `${new Date(build.created_at).toLocaleString()} · ${build.build_id.slice(0, 8)}`,
                            })),
                          ]}
                        />
                      </label>
                    </div>
                    {compareTarget && (
                      <fieldset>
                        <legend>A/B 共用动态参数</legend>
                        <div className="form-columns">
                          <label>
                            检索模式
                            <Select
                              aria-label="A/B 共用检索模式"
                              value={trialMode}
                              onChange={(value) => {
                                if (
                                  value === "hybrid" ||
                                  value === "vector" ||
                                  value === "keyword"
                                )
                                  setTrialMode(value);
                              }}
                              options={[
                                { value: "hybrid", label: "混合" },
                                { value: "vector", label: "向量" },
                                { value: "keyword", label: "关键词" },
                              ]}
                            />
                          </label>
                          <label>
                            返回条数
                            <InputNumber
                              aria-label="A/B 共用返回条数"
                              min={1}
                              max={100}
                              precision={0}
                              value={trialTopK}
                              onChange={(value) => {
                                if (value !== null) setTrialTopK(value);
                              }}
                            />
                          </label>
                        </div>
                        <p className="help-copy">
                          同一问题、模式和返回条数同时提交两个目标。各候选的冻结范围继续生效；未开放参数的候选会明确拒绝对比。
                        </p>
                      </fieldset>
                    )}
                    <label>
                      检索问题
                      <Input.TextArea
                        required
                        value={query}
                        maxLength={4000}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder="输入实际业务问题"
                      />
                    </label>
                    {canEdit && (
                      <Checkbox
                        className="checkbox-label"
                        checked={diagnostics}
                        onChange={(event) =>
                          setDiagnostics(event.target.checked)
                        }
                      >
                        详细诊断（同次请求采集通道排名与融合贡献）
                      </Checkbox>
                    )}
                    {diagnostics && (
                      <p className="help-copy">
                        仅对本次候选试查启用，正常在线调用不增加解释开销。最多返回20条、问题最多1000字；只记录最终授权命中的数值解释，不采集过滤前候选。
                      </p>
                    )}
                    <Button
                      type="primary"
                      htmlType="submit"
                      loading={busy}
                      disabled={!target || !query.trim()}
                    >
                      验证{compareTarget ? "并对比" : ""}
                    </Button>
                  </form>
                  <div className="trial-results">
                    {attempts.map((attempt, index) => (
                      <section className="surface" key={attempt.buildId}>
                        <Tag>
                          {index === 0 ? "A" : "B"} ·{" "}
                          {attempt.buildId.slice(0, 8)}
                        </Tag>
                        <p>{attempt.query}</p>
                        <Button
                          size="small"
                          type="default"
                          onClick={() => setTraceId(attempt.result.trace_id)}
                        >
                          诊断此次试查 · {attempt.result.trace_id.slice(0, 12)}
                        </Button>
                        {attempt.result.hits.length ? (
                          attempt.result.hits.map((hit, index) => (
                            <article className="trial-hit" key={index}>
                              <strong>
                                #{index + 1} · 分数{" "}
                                {hit.score?.toFixed(4) ?? "—"}
                              </strong>
                              <p>{hit.text}</p>
                              {typeof hit.metadata.document_id === "string" && (
                                <Button
                                  size="small"
                                  type="default"
                                  onClick={() => {
                                    const origin = hit.metadata.parse_origin;
                                    const r =
                                      typeof origin === "object" &&
                                      origin !== null &&
                                      !Array.isArray(origin)
                                        ? object(origin)
                                        : {};
                                    const id = hit.metadata.document_id;
                                    if (typeof id === "string")
                                      onOpen(
                                        id,
                                        typeof r.result_id === "string"
                                          ? r.result_id
                                          : undefined,
                                        typeof r.element_ref === "string"
                                          ? r.element_ref
                                          : undefined,
                                      );
                                  }}
                                >
                                  回到原文
                                </Button>
                              )}
                            </article>
                          ))
                        ) : (
                          <p className="empty-copy">
                            没有命中。此结果保留，可用于排查或加入无答案评测。
                          </p>
                        )}
                      </section>
                    ))}
                  </div>
                </div>
              </>
            </ReviewScroll>
          ),
        },
        {
          key: "draft",
          label: "服务草稿",
          children: (
            <ReviewScroll label="服务草稿内容">
              <>
                <form
                  className="studio-form"
                  onSubmit={(event) => void save(event)}
                >
                  <h2>检索服务策略</h2>
                  <p>
                    固定条件始终生效。构建冻结此策略，保存草稿不会直接改变线上服务。
                  </p>
                  {draft ? (
                    <>
                      <label>
                        服务名称
                        <Input
                          value={draft.config.name}
                          maxLength={255}
                          disabled={!canEdit || busy}
                          onChange={(event) => {
                            setDraft({
                              ...draft,
                              config: {
                                ...draft.config,
                                name: event.target.value,
                              },
                            });
                            setDirty(true);
                          }}
                        />
                      </label>
                      <div className="form-columns">
                        <label>
                          默认检索模式
                          <Select
                            aria-label="默认检索模式"
                            value={draft.config.mode}
                            disabled={!canEdit || busy}
                            onChange={(value) => {
                              if (
                                value === "hybrid" ||
                                value === "vector" ||
                                value === "keyword"
                              ) {
                                setDraft({
                                  ...draft,
                                  config: { ...draft.config, mode: value },
                                });
                                setDirty(true);
                              }
                            }}
                            options={[
                              { value: "hybrid", label: "混合检索" },
                              { value: "vector", label: "向量检索" },
                              { value: "keyword", label: "关键词检索" },
                            ]}
                          />
                        </label>
                        <label>
                          默认返回条数
                          <InputNumber
                            aria-label="默认返回条数"
                            min={1}
                            max={100}
                            precision={0}
                            value={draft.config.top_k}
                            disabled={!canEdit || busy}
                            onChange={(value) => {
                              if (value === null) return;
                              setDraft({
                                ...draft,
                                config: { ...draft.config, top_k: value },
                              });
                              setDirty(true);
                            }}
                          />
                        </label>
                      </div>
                      <fieldset disabled={!canEdit || busy}>
                        <legend>允许调用方覆盖的参数</legend>
                        <div className="inline-actions">
                          {(["mode", "top_k", "filters"] as const).map(
                            (field) => (
                              <Checkbox
                                className="checkbox-label"
                                key={field}
                                disabled={!canEdit || busy}
                                checked={draft.config.allowed_fields.includes(
                                  field,
                                )}
                                onChange={(event) => {
                                  const checked = event.target.checked;
                                  setDraft({
                                    ...draft,
                                    config: {
                                      ...draft.config,
                                      allowed_fields:
                                        checked === true
                                          ? [
                                              ...draft.config.allowed_fields,
                                              field,
                                            ]
                                          : draft.config.allowed_fields.filter(
                                              (item) => item !== field,
                                            ),
                                    },
                                  });
                                  setDirty(true);
                                }}
                              >
                                {field === "mode"
                                  ? "检索模式"
                                  : field === "top_k"
                                    ? "返回条数"
                                    : "动态过滤条件"}
                              </Checkbox>
                            ),
                          )}
                        </div>
                      </fieldset>
                      <fieldset
                        disabled={
                          !canEdit ||
                          busy ||
                          !draft.config.allowed_fields.includes("filters")
                        }
                      >
                        <legend>开放给调用方的动态过滤字段</legend>
                        <p className="help-copy">
                          只开放业务需要的字段。固定范围独立生效。
                        </p>
                        <div className="inline-actions">
                          {availableFields.map((field) => (
                            <Checkbox
                              className="checkbox-label"
                              key={field.name}
                              disabled={
                                !canEdit ||
                                busy ||
                                !draft.config.allowed_fields.includes("filters")
                              }
                              checked={
                                draft.config.allowed_filter_fields === null ||
                                draft.config.allowed_filter_fields.includes(
                                  field.name,
                                )
                              }
                              onChange={(event) => {
                                const checked = event.target.checked;
                                const fields =
                                  draft.config.allowed_filter_fields ??
                                  availableFields.map((value) => value.name);
                                setDraft({
                                  ...draft,
                                  config: {
                                    ...draft.config,
                                    allowed_filter_fields:
                                      checked === true
                                        ? [
                                            ...fields.filter(
                                              (value) => value !== field.name,
                                            ),
                                            field.name,
                                          ]
                                        : fields.filter(
                                            (value) => value !== field.name,
                                          ),
                                  },
                                });
                                setDirty(true);
                              }}
                            >
                              {field.label}
                            </Checkbox>
                          ))}
                        </div>
                        {!availableFields.length && (
                          <p className="help-copy">
                            本库尚无自定义字段，可在业务字段中声明字段。
                          </p>
                        )}
                      </fieldset>
                      <label>
                        固定范围条件（JSON）
                        <Input.TextArea
                          value={filterText}
                          disabled={!canEdit || busy}
                          rows={5}
                          onChange={(e) => {
                            setFilterText(e.target.value);
                            setDirty(true);
                          }}
                        />
                      </label>
                      <p className="help-copy">
                        示例：[
                        {JSON.stringify({
                          field: "department",
                          operator: "eq",
                          value: "售后",
                        })}
                        ]。字段需先在本库声明。租户与知识库范围由服务端强制绑定。
                      </p>
                      <label>
                        允许的调用主体（每行一个，留空表示本库已授权调用方）
                        <Input.TextArea
                          value={callerText}
                          rows={3}
                          disabled={!canEdit || busy}
                          onChange={(e) => {
                            setCallerText(e.target.value);
                            setDirty(true);
                          }}
                        />
                      </label>
                      {canEdit && (
                        <Button
                          type="primary"
                          htmlType="submit"
                          loading={busy}
                          disabled={!dirty}
                        >
                          保存服务草稿
                        </Button>
                      )}
                    </>
                  ) : (
                    <p role="status">正在读取草稿…</p>
                  )}
                </form>
              </>
            </ReviewScroll>
          ),
        },
        {
          key: "evaluation",
          label: "质量评测",
          children: (
            <ReviewScroll label="质量评测内容">
              <>
                <div className="studio-form">
                  <h2>逐题质量验收</h2>
                  <QualitySamples api={api} canEdit={canEdit} />
                  <p>
                    先在上方维护预期来源，再对候选运行评测。候选评测在发布前运行，零命中保留在分母。
                  </p>
                  <label>
                    评测候选
                    <Select
                      aria-label="评测候选"
                      value={target}
                      onChange={setTarget}
                      options={[
                        { value: "", label: "选择候选" },
                        ...ready.map((build) => ({
                          value: build.build_id,
                          label: build.build_id.slice(0, 8),
                        })),
                      ]}
                    />
                  </label>
                  {canEdit && (
                    <Button
                      disabled={!target || busy}
                      onClick={() =>
                        void act(async () => {
                          const samples = await api.samples();
                          if (!samples.length)
                            throw new Error("请先创建至少一道评测样本");
                          await api.evaluateCandidate(
                            target,
                            samples.map((sample) => sample.sample_id),
                            "候选质量验收",
                          );
                          setNotice("逐题评测已提交，后台完成后出现在此处。");
                        })
                      }
                    >
                      运行候选评测
                    </Button>
                  )}
                  {evaluations.map((row) => (
                    <EvaluationReport
                      key={String(row.evaluation_id)}
                      api={api}
                      row={row}
                      onOpen={onOpen}
                      onTrace={setTraceId}
                    />
                  ))}
                  <Management
                    api={api}
                    kbId={kbId}
                    canEdit={canEdit}
                    canAdmin={canAdmin}
                    parseOnly={false}
                    section="evaluation"
                    onOpen={onOpen}
                    onError={onError}
                  />
                  {!evaluations.length && (
                    <p className="empty-copy">
                      尚无已完成的候选评测。保存样本并运行后，完成的逐题报告会显示在这里。
                    </p>
                  )}
                </div>
              </>
            </ReviewScroll>
          ),
        },
        {
          key: "operations",
          label: "构建、发布与调用",
          children: (
            <ReviewScroll label="构建、发布与调用内容">
              <>
                <Management
                  api={api}
                  kbId={kbId}
                  canEdit={canEdit}
                  canAdmin={canAdmin}
                  parseOnly={false}
                  section="operations"
                  onOpen={onOpen}
                  onError={onError}
                />
              </>
            </ReviewScroll>
          ),
        },
        {
          key: "integrations",
          children: (
            <ReviewScroll label="接口与调用方内容">
              <Management
                api={api}
                kbId={kbId}
                canEdit={canEdit}
                canAdmin={canAdmin}
                parseOnly={false}
                section="integrations"
                onOpen={onOpen}
                onError={onError}
              />{" "}
              <section className="surface">
                <h3>代码接入</h3>
                <p>
                  在已发布知识库上调用服务，使用本库调用凭证，令牌从运行环境注入。
                </p>
                <pre>{`POST /api/v2/knowledge-bases/${kbId}/service/retrieve\nAuthorization: Bearer <调用凭证>\nContent-Type: application/json\n\n{"query":"你的业务问题"}`}</pre>
              </section>
            </ReviewScroll>
          ),
        },
        {
          key: "trace",
          label: "请求诊断",
          children: (
            <ReviewScroll label="请求诊断内容">
              <>
                <div className="studio-form">
                  <h2>请求诊断</h2>
                  <p>
                    检查当次实际目标、参数与结果。新草稿不会覆盖历史调用条件。
                  </p>
                  {traces.map((trace) => (
                    <details className="surface" key={trace.trace_id}>
                      <summary>
                        {new Date(trace.created_at).toLocaleString()} ·{" "}
                        {trace.target_kind === "build"
                          ? "候选试查"
                          : "发布调用"}{" "}
                        · {trace.trace_id.slice(0, 12)}
                      </summary>
                      <p>
                        目标 {trace.target_id} · 调用者 {trace.subject}
                      </p>
                      <Button
                        type="default"
                        onClick={() => setTraceId(trace.trace_id)}
                      >
                        打开此次诊断
                      </Button>
                      <pre>
                        {JSON.stringify(
                          { request: trace.request, outcome: trace.outcome },
                          null,
                          2,
                        )}
                      </pre>
                    </details>
                  ))}
                  {!traces.length && <p>尚无请求。先执行一次候选验证。</p>}
                </div>
              </>
            </ReviewScroll>
          ),
        },
      ].map((item) => (
        <Activity
          key={item.key}
          mode={item.key === view ? "visible" : "hidden"}
        >
          {item.children}
        </Activity>
      ))}
      <RequestDiagnostics
        api={api}
        kbId={kbId}
        traceId={traceId}
        onClose={() => setTraceId(null)}
        onOpen={onOpen}
      />
    </div>
  );
}
