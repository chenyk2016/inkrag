/** 正式工作空间控制接口；统计、分页、版本和目标均以服务器事实为准。 */
import {
  array,
  client,
  job,
  knowledgeBase,
  number,
  object,
  parsed,
  string,
  summary,
  type DocumentSummary,
  type Job,
  type JsonObject,
  type KnowledgeBase,
  type SearchResult,
} from "./api";
import type { StructureOperation } from "./review-types";
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
export interface Collection {
  knowledge_base: KnowledgeBase;
  document_count: number;
  selected_count: number;
  job_count: number;
  unresolved_count: number;
}
export interface WorkspaceJob {
  job: Job;
  kb_name: string;
  collection_kind: string;
  document_name: string | null;
}
export interface Overview {
  knowledge_base_count: number;
  parse_collection_count: number;
  document_count: number;
  selected_count: number;
  task_count: number;
  running_count: number;
  queued_count: number;
  failed_count: number;
  published_count: number;
}
export interface ServiceConfig {
  name: string;
  mode: "hybrid" | "vector" | "keyword";
  top_k: number;
  fixed_filters: JsonObject[];
  allowed_fields: ("mode" | "top_k" | "filters")[];
  allowed_callers: string[];
  allowed_filter_fields: string[] | null;
}
export interface ServiceDraft {
  kb_id: string;
  revision: number;
  config: ServiceConfig;
}
export interface Trace {
  trace_id: string;
  subject: string;
  target_kind: string;
  target_id: string;
  request: JsonObject;
  outcome: JsonObject;
  created_at: string;
}
/** 分页结构全量校验，不用当前页长度估计全量。 */
function page<T>(value: unknown, decode: (value: unknown) => T): Page<T> {
  const row = object(value);
  return {
    items: array(row.items).map(decode),
    total: number(row.total),
    limit: number(row.limit),
    offset: number(row.offset),
  };
}
/** 保留参数选择，空值不变成字符串 undefined。 */
function query(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values))
    if (value !== undefined && value !== "") params.set(key, String(value));
  return params.toString();
}
/** 服务输入只收窄真实枚举，未知模式拒绝展示为默认模式。 */
function service(value: unknown): ServiceDraft {
  const row = object(value),
    c = object(row.config);
  if (c.mode !== "hybrid" && c.mode !== "vector" && c.mode !== "keyword")
    throw new Error("未知检索模式");
  const fields = array(c.allowed_fields).map(string);
  const allowed: ServiceConfig["allowed_fields"] = [];
  for (const value of fields) {
    if (value !== "mode" && value !== "top_k" && value !== "filters")
      throw new Error("未知开放参数");
    allowed.push(value);
  }
  return {
    kb_id: string(row.kb_id),
    revision: number(row.revision),
    config: {
      name: string(c.name),
      mode: c.mode,
      top_k: number(c.top_k),
      fixed_filters: array(c.fixed_filters).map(object),
      allowed_fields: allowed,
      allowed_callers: array(c.allowed_callers).map(string),
      allowed_filter_fields:
        c.allowed_filter_fields == null
          ? null
          : array(c.allowed_filter_fields).map(string),
    },
  };
}
/** 工作空间 API 与既有客户端共享身份、错误、取消与幂等传输。 */
export type DocumentListStatus =
  | "review"
  | "ready"
  | "failed"
  | "processing"
  | "activating"
  | "live"
  | "saved";
export type DocumentListItem = ReturnType<typeof summary> & {
  listStatus: DocumentListStatus;
  activityAt: string;
};

export function platformClient(token: () => string, kbId = "") {
  const base = client(token, kbId);
  const kb = encodeURIComponent(kbId);
  /** 读取 JSON 并允许路由卸载取消。 */
  async function get(path: string, signal?: AbortSignal): Promise<unknown> {
    return (await base.request(path, { signal })).json();
  }
  return {
    ...base,
    scopeId: kbId,
    async qualitySamples(): Promise<JsonObject[]> {
      return array(
        object(await get(`/knowledge-bases/${kb}/quality-samples`)).items,
      ).map(object);
    },
    async saveQualitySample(value: JsonObject): Promise<void> {
      await base.json(`/knowledge-bases/${kb}/quality-samples`, "POST", value);
    },
    async overview(signal?: AbortSignal): Promise<Overview> {
      const r = object(await get("/workspace", signal));
      return {
        knowledge_base_count: number(r.knowledge_base_count),
        parse_collection_count: number(r.parse_collection_count),
        document_count: number(r.document_count),
        selected_count: number(r.selected_count),
        task_count: number(r.task_count),
        running_count: number(r.running_count),
        queued_count: number(r.queued_count),
        failed_count: number(r.failed_count),
        published_count: number(r.published_count),
      };
    },
    async collections(
      kind = "knowledge",
      search = "",
      offset = 0,
      signal?: AbortSignal,
    ): Promise<Page<Collection>> {
      return page(
        await get(
          "/workspace/knowledge-bases?" +
            query({ kind, search, offset, limit: 30 }),
          signal,
        ),
        (value) => {
          const r = object(value);
          return {
            knowledge_base: knowledgeBase(r.knowledge_base),
            document_count: number(r.document_count),
            selected_count: number(r.selected_count),
            job_count: number(r.job_count),
            unresolved_count: number(r.unresolved_count),
          };
        },
      );
    },
    async taskPage(
      filters: Record<string, string | number | undefined>,
      signal?: AbortSignal,
    ): Promise<Page<WorkspaceJob>> {
      return page(
        await get(
          "/workspace/jobs?" + query({ limit: 30, ...filters }),
          signal,
        ),
        (value) => {
          const r = object(value);
          return {
            job: job(r.job),
            kb_name: string(r.kb_name),
            collection_kind: string(r.collection_kind),
            document_name:
              r.document_name == null ? null : string(r.document_name),
          };
        },
      );
    },
    async documentPage(
      search = "",
      offset = 0,
      signal?: AbortSignal,
      state?: DocumentListStatus,
    ): Promise<
      Page<DocumentListItem> & {
        counts: Partial<Record<DocumentListStatus, number>>;
      }
    > {
      const data = object(
        await get(
          `/workspace/knowledge-bases/${kb}/documents?` +
            query({ search, offset, state, limit: 100 }),
          signal,
        ),
      );
      const result = page(data, (value): DocumentListItem => {
        const row = object(value);
        const listStatus = row.list_status;
        if (
          listStatus !== "review" &&
          listStatus !== "ready" &&
          listStatus !== "failed" &&
          listStatus !== "processing" &&
          listStatus !== "activating" &&
          listStatus !== "live" &&
          listStatus !== "saved"
        )
          throw new Error("服务返回了未知的资料状态。");
        return {
          ...summary(row),
          listStatus,
          activityAt: string(row.activity_at),
        };
      });
      return {
        ...result,
        counts: Object.fromEntries(
          Object.entries(object(data.counts)).map(([key, count]) => [
            key,
            number(count),
          ]),
        ),
      };
    },
    async selectResult(
      document: DocumentSummary,
      resultId: string | null,
      confirmContent = false,
      activate = false,
    ): Promise<DocumentSummary> {
      return summary(
        await base.json(`/documents/${document.document_id}/selection`, "PUT", {
          expected_document_revision: document.revision,
          result_id: resultId,
          confirm_content: confirmContent,
          activate,
        }),
      );
    },
    async reviewResult(
      document: DocumentSummary,
      resultId: string,
      status: "pending" | "approved",
    ): Promise<DocumentSummary> {
      return summary(
        await base.json(
          `/documents/${document.document_id}/reviews/${resultId}`,
          "PUT",
          { expected_document_revision: document.revision, status },
        ),
      );
    },
    async sourcePreview(id: string, resultId: string, signal: AbortSignal) {
      const r = object(
        await get(
          `/documents/${id}/preview?result_id=${encodeURIComponent(resultId)}`,
          signal,
        ),
      );
      return parsed(r.result);
    },
    async bundle(id: string, resultId: string): Promise<Blob> {
      return (
        await base.request(
          `/documents/${id}/bundle?result_id=${encodeURIComponent(resultId)}&include_original=true`,
        )
      ).blob();
    },
    async structure(
      id: string,
      resultId: string,
      operations: readonly StructureOperation[],
      activateRevision?: number,
    ) {
      return base.json(`/documents/${id}/structure`, "PATCH", {
        expected_result_id: resultId,
        operations,
        ...(activateRevision === undefined
          ? {}
          : { activate: true, expected_document_revision: activateRevision }),
      });
    },
    async setAutoActivate(enabled: boolean) {
      return knowledgeBase(
        await base.json(`/knowledge-bases/${kb}/auto-activation`, "PUT", {
          enabled,
        }),
      );
    },
    async replaceSource(
      id: string,
      revision: number,
      file: File,
    ): Promise<Job> {
      return job(
        await (
          await base.request(
            `/documents/${id}/source?filename=${encodeURIComponent(file.name)}&expected_document_revision=${revision}`,
            {
              method: "PUT",
              headers: {
                "Content-Type": "application/octet-stream",
                "Idempotency-Key": crypto.randomUUID(),
              },
              body: file,
            },
          )
        ).json(),
      );
    },
    async reparse(id: string, revision: number): Promise<Job> {
      return job(
        await base.json(`/documents/${id}/reparse`, "POST", {
          expected_document_revision: revision,
        }),
      );
    },
    async service(): Promise<ServiceDraft> {
      return service(await get(`/knowledge-bases/${kb}/service`));
    },
    async saveService(draft: ServiceDraft): Promise<ServiceDraft> {
      return service(
        await base.json(`/knowledge-bases/${kb}/service`, "PUT", {
          expected_revision: draft.revision,
          config: draft.config,
        }),
      );
    },
    async trial(
      buildId: string,
      query: string,
      common?: { mode: "hybrid" | "vector" | "keyword"; top_k: number },
      diagnostics = false,
    ): Promise<SearchResult> {
      const r = object(
        await base.json(`/builds/${buildId}/retrieve`, "POST", {
          query,
          ...common,
          ...(diagnostics ? { diagnostics: true } : {}),
        }),
      );
      return {
        trace_id: string(r.trace_id),
        snapshot_id: r.snapshot_id == null ? null : string(r.snapshot_id),
        hits: array(r.hits).map((value) => {
          const hit = object(value);
          return {
            text: string(hit.text),
            score: typeof hit.score === "number" ? hit.score : null,
            metadata: object(hit.metadata),
          };
        }),
      };
    },
    async traces(): Promise<Page<Trace>> {
      return page(
        await get(`/knowledge-bases/${kb}/traces?limit=30&offset=0`),
        (value) => {
          const r = object(value);
          return {
            trace_id: string(r.trace_id),
            subject: string(r.subject),
            target_kind: string(r.target_kind),
            target_id: string(r.target_id),
            request: object(r.request),
            outcome: object(r.outcome),
            created_at: string(r.created_at),
          };
        },
      );
    },
    async candidateEvaluations(): Promise<JsonObject[]> {
      return array(
        object(await get(`/knowledge-bases/${kb}/candidate-evaluations`)).items,
      ).map(object);
    },
    async evaluateCandidate(
      buildId: string,
      sampleIds: string[],
      name: string,
    ): Promise<Job> {
      return job(
        await base.json(`/builds/${buildId}/evaluations`, "POST", {
          name,
          sample_ids: sampleIds,
          top_k: 5,
        }),
      );
    },
  };
}
export type PlatformClient = ReturnType<typeof platformClient>;
