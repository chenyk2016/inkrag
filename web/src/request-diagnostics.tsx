/** 以单次 trace 身份读取受权诊断，历史不在最近列表也能打开。 */
import { useQuery } from "@tanstack/react-query";
import { Button, Modal, Tag } from "antd";
import { Link } from "react-router-dom";
import { array, number, object, string, type JsonObject } from "./api";
import { Failure } from "./application";
import type { PlatformClient } from "./platform-api";

interface ChannelExplanation {
  channel: "keyword" | "vector";
  rank: number;
  rawScore: number;
  contribution: number | null;
}
interface HitExplanation {
  rank: number;
  documentId: string;
  resultId: string;
  elementRef?: string;
  score: number;
  status: string;
  rankSource: string | undefined;
  channels: ChannelExplanation[];
}
type OpenSource = (id: string, resultId?: string, ref?: string) => void;

/** 只解码后端摘取的白名单数值事实，原生解释树不进入产品或持久化。 */
function explanation(value: unknown): HitExplanation {
  const row = object(value);
  return {
    rank: number(row.rank),
    documentId: string(row.document_id),
    resultId: string(row.result_id),
    elementRef: row.element_ref == null ? undefined : string(row.element_ref),
    score: number(row.score),
    status: string(row.status),
    rankSource: row.rank_source == null ? undefined : string(row.rank_source),
    channels: array(row.channels).map((item) => {
      const channel = object(item);
      if (channel.channel !== "keyword" && channel.channel !== "vector")
        throw new Error("诊断返回未知通道，未猜测通道身份。");
      return {
        channel: channel.channel,
        rank: number(channel.rank),
        rawScore: number(channel.raw_score),
        contribution:
          channel.rrf_contribution == null
            ? null
            : number(channel.rrf_contribution),
      };
    }),
  };
}
/** 只展示本次实际返回的最终命中，未记录的过滤前候选不补查或编造。 */
function DetailedResults({
  value,
  onOpen,
}: {
  value: unknown;
  onOpen: OpenSource;
}) {
  let hits: HitExplanation[] = [],
    error: unknown = null;
  try {
    const diagnostic = object(value);
    if (
      diagnostic.kind !== "opensearch_explain" ||
      diagnostic.same_response !== true ||
      diagnostic.scope !== "authorized_final_hits_only"
    )
      throw new Error("诊断不是同次授权结果解释，无法按详细诊断展示。");
    hits = array(diagnostic.hits).map(explanation);
  } catch (reason: unknown) {
    error = reason;
  }
  if (error !== null) return <Failure error={error} />;
  return (
    <section className="diagnostic-results">
      <h3>同次请求的通道解释</h3>
      <p>
        下方每项是本次最终返回的内容块，只包含通过可信范围检查后的来源与数值解释。
      </p>
      {hits.length ? (
        hits.map((hit) => (
          <article
            className="diagnostic-hit"
            key={`${hit.rank}-${hit.documentId}-${hit.elementRef ?? ""}`}
          >
            <strong>
              最终排名 #{hit.rank} · 分数 {hit.score.toFixed(6)}
            </strong>
            <small>
              保存版本 {hit.resultId.slice(0, 12)}
              {hit.elementRef ? ` · ${hit.elementRef}` : ""}
            </small>
            {hit.status === "captured" ? (
              <div className="diagnostic-channel-grid">
                {["keyword", "vector"].map((name) => {
                  const channel = hit.channels.find(
                    (item) => item.channel === name,
                  );
                  return (
                    <section key={name}>
                      <h4>{name === "keyword" ? "关键词 BM25" : "语义向量"}</h4>
                      {channel ? (
                        <dl>
                          <dt>通道排名</dt>
                          <dd>#{channel.rank}</dd>
                          <dt>原始分数</dt>
                          <dd>{channel.rawScore.toFixed(6)}</dd>
                          <dt>RRF融合贡献</dt>
                          <dd>
                            {channel.contribution === null
                              ? "单通道，无融合"
                              : channel.contribution.toFixed(6)}
                          </dd>
                        </dl>
                      ) : (
                        <p>本次解释中没有此通道贡献。</p>
                      )}
                    </section>
                  );
                })}
              </div>
            ) : (
              <p>
                后端返回的解释形态未被当前版本识别，未猜测通道与排名。结果排序仍是本次后端实际返回的排序。
              </p>
            )}
            {hit.rankSource === "inverse_native_rrf_contribution" && (
              <p className="help-copy">
                融合使用原生 RRF，常数为60。通道排名由该命中的原生贡献 1 / (60 +
                排名) 反推，已校验一致；原始分数不直接相加。
              </p>
            )}
            <Button
              size="small"
              type="default"
              onClick={() =>
                onOpen(hit.documentId, hit.resultId, hit.elementRef)
              }
            >
              打开此命中的保存版本原文
            </Button>
          </article>
        ))
      ) : (
        <p className="empty-copy">
          本次详细诊断已启用，最终返回0条授权命中，没有可展示的通道贡献。零命中不能确定是哪个阶段造成。
        </p>
      )}
      <p className="help-copy">
        这些是最终返回 Top K
        的通道贡献，并非全部候选。未采集过滤前候选数量、各通道候选总数与被排除内容；没有某通道贡献也不等同原始文本完全不匹配。
      </p>
    </section>
  );
}

/** 固定请求范围转成可读条件；保留原值，不猜造未记录的筛选过程。 */
function conditions(value: unknown): string[] {
  const operators: Record<string, string> = {
    eq: "等于",
    ne: "不等于",
    in: "属于",
    gt: "大于",
    gte: "大于等于",
    lt: "小于",
    lte: "小于等于",
    contains: "包含",
  };
  return array(value).map((item) => {
    const row = object(item);
    const operator = string(row.operator);
    return `${string(row.field)} ${operators[operator] ?? operator} ${JSON.stringify(row.value)}`;
  });
}
/** 原因只翻译服务真实给出的代码，未知代码保持未知。 */
function failureReason(code: string): string {
  const reasons: Record<string, string> = {
    forbidden: "当前身份或请求参数没有获得服务许可，请核对服务开放范围。",
    authorization_pending: "查询期间资料权限发生变化，请重新查询。",
    capability_unavailable: "当前服务未启用检索能力，请检查宿主配置。",
    scope_violation:
      "检索返回了已撤销资料，服务已拒绝本次结果，请检查权限投影。",
    retrieval_failed: "检索执行失败，请检查检索服务状态后重试。",
    not_ready: "候选构建尚未就绪，请等待构建完成后重试。",
  };
  return reasons[code] ?? "服务返回了此错误，当前记录没有更具体的原因说明。";
}
/** 命中记录只有文档身份时，入口明确打开当前资料，不伪造历史块定位。 */
function MatchedDocument({
  api,
  id,
  onOpen,
}: {
  api: PlatformClient;
  id: string;
  onOpen: OpenSource;
}) {
  const document = useQuery({
    queryKey: ["trace-document", api.scopeId, id],
    queryFn: ({ signal }) => api.documentInfo(id, signal),
    retry: false,
  });
  return (
    <li>
      <Button size="small" type="default" onClick={() => onOpen(id)}>
        {document.data?.name ?? `资料 ${id.slice(0, 12)}`}
      </Button>
      {document.isError && (
        <small>（当前资料不可读，可能已撤销或无权访问）</small>
      )}
    </li>
  );
}
/** 对话框在自己的视口内滚动，关闭后由组件库返回触发前的焦点。 */
export function RequestDiagnostics({
  api,
  kbId,
  traceId,
  onClose,
  onOpen,
}: {
  api: PlatformClient;
  kbId: string;
  traceId: string | null;
  onClose: () => void;
  onOpen: OpenSource;
}) {
  const trace = useQuery({
    queryKey: ["request-diagnostics", api.scopeId, traceId],
    queryFn: async ({ signal }) => {
      if (!traceId) throw new Error("缺少请求身份");
      const row = object(
        await (
          await api.request(`/traces/${encodeURIComponent(traceId)}`, {
            signal,
          })
        ).json(),
      );
      if (string(row.trace_id) !== traceId || string(row.kb_id) !== kbId)
        throw new Error("请求记录与当前知识库或请求身份不一致。");
      if (row.target_kind !== "build" && row.target_kind !== "release")
        throw new Error("请求记录包含未知目标类型。");
      string(row.target_id);
      string(row.subject);
      string(row.created_at);
      return row;
    },
    enabled: traceId !== null,
    retry: false,
  });
  let request: JsonObject = {},
    outcome: JsonObject = {},
    filters: string[] = [],
    documentIds: string[] = [],
    decodeError: unknown = null;
  if (trace.data) {
    try {
      request = object(trace.data.request);
      outcome = object(trace.data.outcome);
      filters = conditions(request.filters);
      documentIds =
        outcome.document_ids == null
          ? []
          : array(outcome.document_ids).map(string);
    } catch (reason: unknown) {
      decodeError = reason;
    }
  }
  const mode =
    request.mode === "hybrid"
      ? "混合检索"
      : request.mode === "vector"
        ? "语义向量"
        : request.mode === "keyword"
          ? "关键词 BM25"
          : "未提供";
  return (
    <Modal
      open={traceId !== null}
      onCancel={onClose}
      title="此次检索诊断"
      width={860}
      className="request-diagnostics-dialog"
      footer={<Button onClick={onClose}>关闭诊断</Button>}
    >
      <p>核对评测当次使用的候选、条件与结果。新草稿不会改写历史记录。</p>
      {trace.isPending && <p role="status">正在读取请求记录…</p>}
      {trace.isError && <Failure error={trace.error} />}
      {decodeError !== null && <Failure error={decodeError} />}
      {trace.data && decodeError === null && (
        <div className="request-diagnostics">
          <Tag
            color={
              outcome.status === "succeeded"
                ? "green"
                : outcome.status === "failed"
                  ? "red"
                  : "default"
            }
          >
            {outcome.status === "succeeded"
              ? documentIds.length
                ? "请求成功 · 有命中"
                : "请求成功 · 零命中"
              : outcome.status === "failed"
                ? "检索失败"
                : "状态未提供"}
          </Tag>
          <dl>
            <dt>问题</dt>
            <dd>
              {typeof request.query === "string" ? request.query : "未提供"}
            </dd>
            <dt>实际目标</dt>
            <dd>
              {trace.data.target_kind === "build" ? "候选构建" : "发布版本"} ·{" "}
              {String(trace.data.target_id)}
            </dd>
            <dt>物理索引版本</dt>
            <dd>
              {typeof outcome.index_name === "string"
                ? outcome.index_name
                : "未提供"}
            </dd>
            <dt>检索参数</dt>
            <dd>
              {mode} · Top K {String(request.top_k)}
            </dd>
            <dt>实际筛选条件</dt>
            <dd>
              {filters.length ? (
                <ul>
                  {filters.map((filter, index) => (
                    <li key={index}>{filter}</li>
                  ))}
                </ul>
              ) : (
                "没有业务字段筛选条件（可信租户与权限范围仍由服务强制执行）"
              )}
            </dd>
            <dt>命中资料数</dt>
            <dd>
              {outcome.status === "succeeded" ? documentIds.length : "未提供"}
            </dd>
            <dt>检索耗时</dt>
            <dd>
              {typeof outcome.duration_ms === "number"
                ? `${outcome.duration_ms.toFixed(1)} 毫秒`
                : "未提供"}
            </dd>
            <dt>调用身份</dt>
            <dd>{String(trace.data.subject)}</dd>
            <dt>请求时间</dt>
            <dd>
              {typeof trace.data.created_at === "string"
                ? new Date(trace.data.created_at).toLocaleString()
                : "未提供"}
            </dd>
            <dt>请求编号</dt>
            <dd>{traceId}</dd>
            {typeof outcome.error_code === "string" && (
              <>
                <dt>失败原因码</dt>
                <dd>
                  {failureReason(outcome.error_code)}（{outcome.error_code}）
                </dd>
              </>
            )}
          </dl>
          {outcome.status === "succeeded" && !documentIds.length && (
            <p className="message-banner">
              {filters.length
                ? "本次有生效筛选条件且没有命中。请返回资料，核对预期资料的业务字段是否满足上方条件；编辑业务字段并生效后，再创建新构建，用同一问题和条件重试。"
                : "本次查询没有命中。请返回资料，核对候选中是否选用了预期的解析版本与业务字段，再创建新构建，用同一问题和参数重试。"}
            </p>
          )}
          {!!documentIds.length && (
            <section>
              <h3>实际命中的资料</h3>
              <ul className="trace-document-list">
                {documentIds.map((id) => (
                  <MatchedDocument
                    key={id}
                    api={api}
                    id={id}
                    onOpen={(documentId) => {
                      onClose();
                      onOpen(documentId);
                    }}
                  />
                ))}
              </ul>
              <p className="help-copy">
                本记录没有保存命中块与解析版本，以上入口打开当前资料参考。精确历史来源请从逐题评测的“实际命中来源”打开。
              </p>
            </section>
          )}
          {outcome.diagnostics == null ? (
            <p className="help-copy">
              本次请求未启用详细诊断，未采集通道排名与融合贡献，也未采集过滤前候选。可在候选验证勾选“详细诊断”后重新发起，新请求会单独保存诊断编号；不会补写本条历史。
            </p>
          ) : (
            <DetailedResults
              value={outcome.diagnostics}
              onOpen={(id, resultId, ref) => {
                onClose();
                onOpen(id, resultId, ref);
              }}
            />
          )}
          <Link
            to={`/knowledge/${encodeURIComponent(kbId)}/documents`}
            onClick={onClose}
          >
            返回资料，核对版本与选用的业务字段
          </Link>
          <a
            href={`/knowledge/${encodeURIComponent(kbId)}/diagnostics?trace=${encodeURIComponent(traceId ?? "")}`}
          >
            此请求的固定诊断链接
          </a>
          <details>
            <summary>原始请求与结果记录（JSON）</summary>
            <pre>{JSON.stringify(trace.data, null, 2)}</pre>
          </details>
        </div>
      )}
    </Modal>
  );
}
