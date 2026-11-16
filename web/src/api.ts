/** HTTP 边界：只接受经过结构校验的数据，令牌由调用者保存在内存中。 */
import { createAssetReader } from "./asset-reader";

export type JsonObject = { [key: string]: unknown };
export interface DocumentSummary {
  enabled: boolean;
  document_id: string;
  kb_id: string;
  name: string;
  source_asset_id: string;
  created_at: string;
  result_id: string;
  revision: number;
  indexed_result_id: string | null;
  indexed_revision: number | null;
  metadata: JsonObject;
  selected_result_id: string | null;
  selected_revision: number | null;
  selected_metadata: JsonObject | null;
  parser_engine: string;
  review_status: string;
}
export interface TextElement {
  self_ref: string;
  text: string;
  page: number | null;
  label: string;
  heading_level: number | null;
}
export interface NativeTable {
  self_ref: string;
  rows: number;
  cols: number;
  page: number | null;
  cells: {
    text: string;
    row: number;
    col: number;
    rowSpan: number;
    colSpan: number;
    header: boolean;
  }[];
}
export interface Asset {
  asset_id: string;
  element_ref: string;
  role: string;
  page: number | null;
  media_type: string;
}
export interface ParsedResult {
  options: { engine: string | null; ocr_enabled: boolean | null };
  result_id: string;
  revision: number;
  source: {
    name: string;
    asset_id: string | null;
    size_bytes: number;
    format: string;
  };
  texts: TextElement[];
  tables: NativeTable[];
  assets: Asset[];
  order: {
    self_ref: string;
    kind: "text" | "table" | "picture";
    label: string;
    page: number | null;
  }[];
  origins: { element_ref: string; locator: string; resource_uri?: string }[];
  pictures: { self_ref: string; caption: string; page: number | null }[];
  editor: {
    excluded_refs: string[];
    picture_descriptions: Record<string, string>;
  };
  warnings: { code: string; message: string; page: number | null }[];
}
export interface DocumentDetail extends DocumentSummary {
  result: ParsedResult;
  reviewed_by: string | null;
  reviewed_at: string | null;
}
export interface Version {
  result_id: string;
  revision: number;
  review_status: "pending" | "approved";
  created_at: string;
}
export interface Hit {
  text: string;
  score: number | null;
  metadata: JsonObject;
}
export type RetrievalMode = "hybrid" | "vector" | "keyword";

export interface SearchResult {
  hits: Hit[];
  snapshot_id: string | null;
  trace_id: string;
}
export interface PublishedSearchResult extends SearchResult {
  release_id: string;
  effective_request: {
    query: string;
    mode: RetrievalMode;
    top_k: number;
    filters: JsonObject[];
  };
}
/** 服务声明的解析工具及实际可用格式；界面不根据工具名称猜测能力。 */
export interface ParseEngine {
  ocr_formats: string[];
  ocr_default: boolean;
  id: string;
  name: string;
  formats: string[];
  available: boolean;
  pending_formats: string[];
  reason: string | null;
}
export interface ProcessingOptions {
  chunking: "auto" | "sentence" | "markdown";
  chunk_size: number;
  chunk_overlap: number;
  include_title: boolean;
}
export interface ProcessingCapabilities {
  available: boolean;
  reason: string | null;
  max_tokens: number | null;
  defaults: ProcessingOptions | null;
  chunking: string[];
  qa_pairs: { available: boolean; reason: string | null };
  supplemental_index: { available: boolean; reason: string | null };
  image_semantics: { available: boolean; reason: string | null };
}
/** 校验服务声明的分块配置，不从页面默认值推断实际运行配置。 */
function processingOptions(value: unknown): ProcessingOptions {
  const row = object(value);
  const chunking = string(row.chunking);
  if (
    !["auto", "sentence", "markdown"].includes(chunking) ||
    typeof row.include_title !== "boolean"
  )
    throw new ApiError("invalid_response", "数据处理配置响应无效");
  return {
    chunking:
      chunking === "auto"
        ? "auto"
        : chunking === "sentence"
          ? "sentence"
          : "markdown",
    chunk_size: number(row.chunk_size),
    chunk_overlap: number(row.chunk_overlap),
    include_title: row.include_title,
  };
}
/** 保持不支持能力的原因，缺失声明时不开放可写选项。 */
function processingCapabilities(value: unknown): ProcessingCapabilities | null {
  if (value == null) return null;
  const row = object(value);
  function feature(value: unknown) {
    const item = object(value);
    if (typeof item.available !== "boolean")
      throw new ApiError("invalid_response", "处理能力响应无效");
    return { available: item.available, reason: nullableString(item.reason) };
  }
  return {
    ...feature(row),
    max_tokens: nullableNumber(row.max_tokens),
    defaults: row.defaults == null ? null : processingOptions(row.defaults),
    chunking: array(row.chunking).map(string),
    qa_pairs: feature(row.qa_pairs),
    supplemental_index: feature(row.supplemental_index),
    image_semantics: feature(row.image_semantics),
  };
}
export interface Capabilities {
  answer: ModelCapability;
  vision: ModelCapability;
  processing: ProcessingCapabilities | null;
  parse_engines: ParseEngine[];
  formats: string[];
  max_upload_bytes: number;
  storage: string;
  limitations: string[];
  roles: string[];
  mode: string;
  tenant_id: string;
}

interface ModelCapability {
  available: boolean;
  model: string | null;
  provider: string | null;
}

export interface AnswerResult {
  status: "answered" | "insufficient";
  model: string;
  trace_id: string;
  statements: {
    text: string;
    citations: { source_id: string; quote: string }[];
  }[];
  sources: {
    source_id: string;
    document_id: string;
    result_id: string;
    element_ref: string;
    picture_ref: string | null;
    page: number | null;
    name: string;
  }[];
}

export interface PictureSuggestion {
  model: string;
  description: string;
  relationships: string[];
  uncertainties: string[];
  suggested_text: string | null;
}

function modelCapability(value: unknown): ModelCapability {
  if (value == null) return { available: false, model: null, provider: null };
  const row = object(value);
  return {
    available: row.available === true,
    model: nullableString(row.model),
    provider: nullableString(row.provider ?? null),
  };
}

/** 服务错误携带安全说明与可追踪记录，不把非 JSON 响应当成成功。 */
export class ApiError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly recordId: string | null = null,
    public readonly details: JsonObject | null = null,
    public readonly retryAfterMs: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** 将外部值收窄为普通对象，拒绝数组或空值。 */
export function object(value: unknown): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new ApiError(
      "invalid_response",
      "服务返回的数据结构不完整，请检查服务版本。",
    );
  return Object.fromEntries(Object.entries(value));
}
/** 校验字符串字段，避免界面把错误结构默默显示为空。 */
export function string(value: unknown): string {
  if (typeof value !== "string")
    throw new ApiError("invalid_response", "服务返回了无效的文字字段。");
  return value;
}
/** 校验有限数字，NaN 或缺失不进入界面状态。 */
export function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    throw new ApiError("invalid_response", "服务返回了无效的数字字段。");
  return value;
}
/** 保留协议明确允许的空字符串引用。 */
export function nullableString(value: unknown): string | null {
  return value === null ? null : string(value);
}
/** 校验数组外层，元素交由对应协议解码器校验。 */
export function array(value: unknown): unknown[] {
  if (!Array.isArray(value))
    throw new ApiError("invalid_response", "服务返回了无效的列表。");
  return value;
}
/** 审核事实只接受协议枚举，缺失或未知状态不能伪装为待审核。 */
function reviewStatus(value: unknown): Version["review_status"] {
  if (value !== "pending" && value !== "approved")
    throw new ApiError("invalid_response", "服务返回了无效的版本审核状态。");
  return value;
}
/** 将原生物理页号保留为空或有限数字。 */
function nullableNumber(value: unknown): number | null {
  return value === null || value === undefined ? null : number(value);
}
/** 解析目录摘要，保留当前版本与已入库版本的差异。 */
export function summary(value: unknown): DocumentSummary {
  const row = object(value);
  if (typeof row.enabled !== "boolean")
    throw new ApiError(
      "invalid_response",
      "资料启用状态无效，请检查服务版本。",
    );
  return {
    enabled: row.enabled,
    document_id: string(row.document_id),
    kb_id: string(row.kb_id),
    name: string(row.name),
    source_asset_id: string(row.source_asset_id),
    created_at: string(row.created_at),
    result_id: row.result_id === null ? "" : string(row.result_id),
    revision: number(row.revision),
    indexed_result_id: nullableString(row.indexed_result_id),
    indexed_revision: nullableNumber(row.indexed_revision),
    metadata: object(row.metadata),
    selected_result_id: nullableString(row.selected_result_id ?? null),
    selected_revision: nullableNumber(row.selected_revision),
    selected_metadata:
      row.selected_metadata === null || row.selected_metadata === undefined
        ? null
        : object(row.selected_metadata),
    parser_engine:
      typeof row.parser_engine === "string" ? row.parser_engine : "builtin",
    review_status:
      typeof row.review_status === "string" ? row.review_status : "unknown",
  };
}
/** 只读取界面使用的原生解析字段，不重建一套文档业务模型。 */
export function parsed(value: unknown): ParsedResult {
  const result = object(value),
    source = object(result.source),
    document = object(result.document);
  const texts = array(document.texts).map(object);
  const tables = array(document.tables ?? []).map(object);
  const pictures = array(document.pictures ?? []).map(object);
  const groups = array(document.groups ?? []).map(object);
  const nodes = new Map(
    [...texts, ...tables, ...pictures, ...groups].map((row) => [
      string(row.self_ref),
      row,
    ]),
  );
  const textRefs = new Set(texts.map((row) => string(row.self_ref)));
  const tableRefs = new Set(tables.map((row) => string(row.self_ref)));
  const pictureRefs = new Set(pictures.map((row) => string(row.self_ref)));
  const order: ParsedResult["order"] = [];
  const seen = new Set<string>();
  /** 按 Docling body 原生引用递归，防止表图被挪到文末；环或重复引用不重复渲染。 */
  function walk(row: JsonObject): void {
    const ref = string(row.self_ref);
    if (seen.has(ref)) return;
    seen.add(ref);
    const kind = textRefs.has(ref)
      ? "text"
      : tableRefs.has(ref)
        ? "table"
        : pictureRefs.has(ref)
          ? "picture"
          : null;
    const provenance = array(row.prov ?? []);
    if (kind)
      order.push({
        self_ref: ref,
        kind,
        label: typeof row.label === "string" ? row.label : kind,
        page: provenance.length
          ? nullableNumber(object(provenance[0]).page_no)
          : null,
      });
    for (const child of array(row.children ?? [])) {
      const node = nodes.get(string(object(child).$ref));
      if (node) walk(node);
    }
  }
  walk(object(document.body));
  const editor = object(result.editor ?? {});
  const options = object(result.options ?? {});
  if (options.ocr_enabled != null && typeof options.ocr_enabled !== "boolean")
    throw new ApiError("invalid_response", "解析版本的 OCR 配置无效。");
  return {
    options: {
      engine: options.engine == null ? null : string(options.engine),
      ocr_enabled: options.ocr_enabled ?? null,
    },
    order,
    origins: array(result.origins ?? []).map((value) => {
      const row = object(value);
      return {
        element_ref: string(row.element_ref),
        locator: string(row.locator),
        ...(typeof row.resource_uri === "string"
          ? { resource_uri: row.resource_uri }
          : {}),
      };
    }),
    pictures: pictures.map((row) => {
      const prov = array(row.prov ?? []);
      const captions = array(row.captions ?? [])
        .map((value) => nodes.get(string(object(value).$ref)))
        .filter((value) => value !== undefined)
        .map((value) => (typeof value.text === "string" ? value.text : ""));
      return {
        self_ref: string(row.self_ref),
        caption: captions.join(" "),
        page: prov.length ? nullableNumber(object(prov[0]).page_no) : null,
      };
    }),
    editor: {
      excluded_refs: array(editor.excluded_refs ?? []).map(string),
      picture_descriptions: Object.fromEntries(
        Object.entries(object(editor.picture_descriptions ?? {})).map(
          ([key, value]) => [key, string(value)],
        ),
      ),
    },
    result_id: string(result.result_id),
    revision: number(result.revision),
    source: {
      name: string(source.name),
      asset_id: nullableString(source.asset_id),
      size_bytes: number(source.size_bytes),
      format: string(source.format),
    },
    texts: array(document.texts).map((item) => {
      const row = object(item),
        prov = array(row.prov ?? []);
      return {
        self_ref: string(row.self_ref),
        text: string(row.text),
        label: string(row.label),
        heading_level: nullableNumber(row.level),
        page: prov.length ? nullableNumber(object(prov[0]).page_no) : null,
      };
    }),
    tables: array(document.tables ?? []).map((value) => {
      const table = object(value),
        data = object(table.data);
      return {
        self_ref: string(table.self_ref),
        rows: number(data.num_rows),
        cols: number(data.num_cols),
        page: array(table.prov ?? []).length
          ? nullableNumber(object(array(table.prov)[0]).page_no)
          : null,
        cells: array(data.table_cells).map((value) => {
          const cell = object(value);
          return {
            text: string(cell.text),
            row: number(cell.start_row_offset_idx),
            col: number(cell.start_col_offset_idx),
            rowSpan: number(cell.row_span),
            colSpan: number(cell.col_span),
            header: cell.column_header === true || cell.row_header === true,
          };
        }),
      };
    }),
    assets: array(result.assets).map((item) => {
      const row = object(item);
      return {
        asset_id: string(row.asset_id),
        element_ref: string(row.element_ref),
        role: string(row.role),
        page: nullableNumber(row.page),
        media_type: string(row.media_type),
      };
    }),
    warnings: array(result.warnings).map((item) => {
      const row = object(item);
      return {
        code: string(row.code),
        message: string(row.message),
        page: nullableNumber(row.page),
      };
    }),
  };
}
/** 校验资料详情与所选解析版本。 */
function detail(value: unknown): DocumentDetail {
  const view = object(value);
  if (view.result === null)
    throw new ApiError(
      "not_ready",
      "解析任务尚未完成，可在任务与发布中查看进度。",
    );
  const result = parsed(view.result);
  const version = object(view.version);
  if (string(version.result_id) !== result.result_id)
    throw new ApiError(
      "invalid_response",
      "审核记录与当前展示的解析版本不一致。",
    );
  return {
    ...summary(view.document),
    result_id: result.result_id,
    review_status: reviewStatus(version.review_status),
    reviewed_by: nullableString(version.reviewed_by),
    reviewed_at: nullableString(version.reviewed_at),
    result,
  };
}

/** 非成功响应统一解码为可展示错误；HTML、网络错误不会被吞成空结果。 */
async function checked(response: Response): Promise<Response> {
  if (response.ok) return response;
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new ApiError(
      `http_${response.status}`,
      `服务暂时无法处理请求（HTTP ${response.status}）。`,
    );
  }
  const error = object(object(body).error);
  const retryAfter = response.headers.get("Retry-After");
  const retryAfterMs =
    retryAfter === null
      ? null
      : /^\d+$/.test(retryAfter)
        ? Number(retryAfter) * 1000
        : Math.max(0, Date.parse(retryAfter) - Date.now());
  throw new ApiError(
    string(error.code),
    string(error.message),
    error.request_id == null ? null : string(error.request_id),
    error.details == null ? null : object(error.details),
    retryAfterMs !== null && Number.isFinite(retryAfterMs)
      ? retryAfterMs
      : null,
  );
}

/** 请求使用同源地址及 Authorization 头；令牌不会进入 URL 或持久存储。 */
export function client(token: string | (() => string), kbId = "") {
  /** 发送真实 HTTP 请求并校验状态，调用者可取消过期的读取。 */
  async function request(
    path: string,
    options: RequestInit = {},
  ): Promise<Response> {
    const headers = new Headers(options.headers);
    headers.set(
      "Authorization",
      `Bearer ${typeof token === "string" ? token : token()}`,
    );
    try {
      return await checked(
        await fetch(`/api/v2${path}`, {
          ...options,
          headers,
          cache: "no-store",
        }),
      );
    } catch (error: unknown) {
      if (
        error instanceof ApiError ||
        (error instanceof DOMException && error.name === "AbortError")
      )
        throw error;
      if (error instanceof DOMException && error.name === "TimeoutError")
        throw new ApiError("request_timeout", "请求超时。请重试。");
      throw new ApiError(
        "network_error",
        "连接服务失败，请检查网络和服务运行状态。",
      );
    }
  }
  /** 发送严格 JSON 控制参数。 */
  async function json(
    path: string,
    method: string,
    value: unknown,
    operationKey?: string,
    signal?: AbortSignal,
  ): Promise<unknown> {
    const response = await request(path, {
      method,
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(value),
      ...(method === "POST"
        ? {
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": operationKey ?? crypto.randomUUID(),
            },
          }
        : {}),
    });
    const body: unknown = await response.json();
    return body;
  }
  const asset = createAssetReader(
    async (id, signal) =>
      (await request(`/assets/${encodeURIComponent(id)}`, { signal })).blob(),
    (error) => {
      if (!(error instanceof ApiError) || error.code !== "capacity_exceeded")
        return null;
      const delay = error.retryAfterMs ?? 2000;
      return delay <= 5000 ? delay : null;
    },
  );
  return {
    request,
    json,
    /** 知识库与任务事实始终从服务读取，不持久化业务占位。 */
    async knowledgeBases(): Promise<KnowledgeBase[]> {
      return array(
        object(await (await request("/knowledge-bases")).json()).items,
      ).map(knowledgeBase);
    },
    async createKnowledgeBase(
      name: string,
      kind: "knowledge" | "parse" = "knowledge",
    ): Promise<KnowledgeBase> {
      return knowledgeBase(
        await json("/knowledge-bases", "POST", { name, kind }),
      );
    },
    async jobs(): Promise<Job[]> {
      return array(
        object(
          await (
            await request(`/jobs?kb_id=${encodeURIComponent(kbId)}`)
          ).json(),
        ).items,
      ).map(job);
    },
    async jobAction(id: string, action: "cancel" | "retry"): Promise<Job> {
      return job(
        await json(`/jobs/${encodeURIComponent(id)}/${action}`, "POST", {}),
      );
    },
    async rebuild(incremental = false, key?: string): Promise<Job> {
      return job(
        await json(
          `/knowledge-bases/${encodeURIComponent(kbId)}/builds${incremental ? "?incremental=true" : ""}`,
          "POST",
          {},
          key,
        ),
      );
    },
    async getJob(id: string): Promise<Job> {
      return job(
        await (await request(`/jobs/${encodeURIComponent(id)}`)).json(),
      );
    },
    async builds(): Promise<Build[]> {
      return array(
        object(
          await (
            await request(`/knowledge-bases/${encodeURIComponent(kbId)}/builds`)
          ).json(),
        ).items,
      ).map(build);
    },
    async releases(): Promise<Release[]> {
      return array(
        object(
          await (
            await request(
              `/knowledge-bases/${encodeURIComponent(kbId)}/releases`,
            )
          ).json(),
        ).items,
      ).map(release);
    },
    async publish(id: string, expectedReleaseId: string | null): Promise<void> {
      await json(`/builds/${encodeURIComponent(id)}/publish`, "POST", {
        expected_release_id: expectedReleaseId,
      });
    },
    async rollback(
      id: string,
      expectedReleaseId: string | null,
    ): Promise<void> {
      await json(`/releases/${encodeURIComponent(id)}/rollback`, "POST", {
        expected_release_id: expectedReleaseId,
      });
    },
    async remove(document: DocumentSummary): Promise<void> {
      await request(
        `/documents/${encodeURIComponent(document.document_id)}?expected_revision=${document.revision}`,
        { method: "DELETE" },
      );
    },
    async callers(): Promise<Caller[]> {
      return array(
        object(
          await (
            await request(
              `/knowledge-bases/${encodeURIComponent(kbId)}/callers`,
            )
          ).json(),
        ).items,
      ).map((value) => {
        const r = object(value);
        return {
          key_id: string(r.key_id),
          name: string(r.name),
          revoked: r.revoked === true,
        };
      });
    },
    async issueCaller(name: string): Promise<string> {
      return string(
        object(
          object(
            await json(
              `/knowledge-bases/${encodeURIComponent(kbId)}/callers`,
              "POST",
              { name },
            ),
          ).key,
        ).token,
      );
    },
    async revokeCaller(id: string): Promise<void> {
      await request(`/callers/${encodeURIComponent(id)}`, { method: "DELETE" });
    },
    async audit(): Promise<Audit[]> {
      return array(
        object(
          await (
            await request(`/audit?kb_id=${encodeURIComponent(kbId)}`)
          ).json(),
        ).items,
      ).map((value) => {
        const r = object(value);
        return {
          event_id: string(r.event_id),
          subject: string(r.subject),
          action: string(r.action),
          target_id: string(r.target_id),
          created_at: string(r.created_at),
        };
      });
    },
    async samples(): Promise<Sample[]> {
      return array(
        object(
          await (
            await request(
              `/knowledge-bases/${encodeURIComponent(kbId)}/samples`,
            )
          ).json(),
        ).items,
      ).map((value) => {
        const r = object(value);
        return {
          sample_id: string(r.sample_id),
          query: string(r.query),
          document_level:
            r.no_answer !== true &&
            array(r.expected_sources ?? []).length === 0,
          expected_document_ids: array(r.expected_document_ids).map(string),
        };
      });
    },
    async createSample(
      query: string,
      expectedDocumentIds: string[],
    ): Promise<void> {
      await json(
        `/knowledge-bases/${encodeURIComponent(kbId)}/samples`,
        "POST",
        { query, expected_document_ids: expectedDocumentIds },
      );
    },
    async evaluations(): Promise<Evaluation[]> {
      return array(
        object(
          await (
            await request(
              `/knowledge-bases/${encodeURIComponent(kbId)}/evaluations`,
            )
          ).json(),
        ).items,
      ).map((value) => {
        const r = object(value),
          m = object(r.metrics);
        return {
          evaluation_id: string(r.evaluation_id),
          release_id: string(r.release_id),
          name: string(r.name),
          created_at: string(r.created_at),
          sample_count: number(m.sample_count),
          recall: number(m.recall),
          precision: number(m.precision),
          mrr: number(m.mrr),
        };
      });
    },
    async evaluate(
      releaseId: string,
      name: string,
      topK: number,
      mode: "hybrid" | "vector" | "keyword",
    ): Promise<Job> {
      return job(
        await json(
          `/knowledge-bases/${encodeURIComponent(kbId)}/evaluations`,
          "POST",
          { release_id: releaseId, name, top_k: topK, mode },
        ),
      );
    },
    async schema(): Promise<KnowledgeBase> {
      return knowledgeBase(
        await (
          await request(`/knowledge-bases/${encodeURIComponent(kbId)}/fields`)
        ).json(),
      );
    },
    async setSchema(
      kb: KnowledgeBase,
      fields: MetadataField[],
    ): Promise<KnowledgeBase> {
      return knowledgeBase(
        await json(
          `/knowledge-bases/${encodeURIComponent(kbId)}/fields`,
          "PUT",
          { expected_revision: kb.revision, fields },
        ),
      );
    },
    async serviceStatus(): Promise<ServiceStatus> {
      const row = object(await (await request("/status")).json());
      return {
        ready: row.ready === true,
        mode: string(row.mode),

        model_identity:
          row.model_identity === null ? null : object(row.model_identity),
      };
    },
    async fields(): Promise<Record<string, string>> {
      const row = object(
        await (
          await request(
            `/knowledge-bases/${encodeURIComponent(kbId)}/search-fields`,
          )
        ).json(),
      );
      return Object.fromEntries(
        Object.entries(object(row.fields)).map(([key, value]) => [
          key,
          string(value),
        ]),
      );
    },
    async metadata(
      document: DocumentSummary,
      value: JsonObject,
    ): Promise<DocumentSummary> {
      return summary(
        await json(
          `/documents/${encodeURIComponent(document.document_id)}/metadata`,
          "PATCH",
          { expected_document_revision: document.revision, metadata: value },
        ),
      );
    },
    /** 启用状态独立于解析版本，成功后所有新检索立即应用。 */
    async setDocumentEnabled(
      id: string,
      enabled: boolean,
    ): Promise<DocumentSummary> {
      return summary(
        await json(`/documents/${encodeURIComponent(id)}/enabled`, "PATCH", {
          enabled,
        }),
      );
    },
    /** 查询服务真实能力，供导入限制与设置展示使用。 */
    async capabilities(): Promise<Capabilities> {
      const row = object(await (await request("/capabilities")).json());
      return {
        answer: modelCapability(row.answer),
        vision: modelCapability(row.vision),
        processing: processingCapabilities(row.processing),
        formats: array(row.formats).map(string),
        parse_engines: array(row.parse_engines).map((value) => {
          const engine = object(value);
          if (
            typeof engine.available !== "boolean" ||
            typeof engine.ocr_default !== "boolean"
          )
            throw new ApiError("invalid_response", "解析工具能力响应无效");
          return {
            ocr_formats: array(engine.ocr_formats).map(string),
            ocr_default: engine.ocr_default,
            id: string(engine.id),
            name: string(engine.name),
            formats: array(engine.formats).map(string),
            available: engine.available,
            pending_formats: array(engine.pending_formats).map(string),
            reason: nullableString(engine.reason),
          };
        }),
        max_upload_bytes: number(row.max_upload_bytes),
        storage: string(row.storage),
        limitations: array(row.limitations).map(string),
        roles: array(row.roles).map(string),
        mode: string(row.mode),
        tenant_id: string(row.tenant_id),
      };
    },
    /** 读取持久化目录，不合成资料占位。 */
    async documents(offset = 0): Promise<DocumentSummary[]> {
      return array(
        object(
          await (
            await request(
              `/knowledge-bases/${encodeURIComponent(kbId)}/documents?limit=100&offset=${offset}`,
            )
          ).json(),
        ).items,
      ).map(summary);
    },
    /** 查询当前或历史不可变版本，支持取消已过期选择。 */
    async documentInfo(
      id: string,
      signal?: AbortSignal,
    ): Promise<DocumentSummary> {
      return summary(
        await (
          await request(`/documents/${encodeURIComponent(id)}/info`, { signal })
        ).json(),
      );
    },
    async document(
      id: string,
      resultId?: string,
      signal?: AbortSignal,
    ): Promise<DocumentDetail> {
      const query = resultId
        ? `?result_id=${encodeURIComponent(resultId)}`
        : "";
      return detail(
        await (
          await request(`/documents/${encodeURIComponent(id)}${query}`, {
            signal,
          })
        ).json(),
      );
    },
    /** 读取版本列表，恢复/修改仍由明确的服务动作决定。 */
    async versions(id: string, signal?: AbortSignal): Promise<Version[]> {
      return array(
        object(
          await (
            await request(`/documents/${encodeURIComponent(id)}/versions`, {
              signal,
            })
          ).json(),
        ).items,
      ).map((item) => {
        const row = object(item);
        return {
          result_id: string(row.result_id),
          revision: number(row.revision),
          review_status: reviewStatus(row.review_status),
          created_at: string(row.created_at),
        };
      });
    },
    /** 保存待导入原件，确认前不创建资料。 */
    async stageImport(file: File): Promise<string> {
      const path = `/knowledge-bases/${encodeURIComponent(kbId)}/imports/staged?filename=${encodeURIComponent(file.name)}`;
      const response = await request(path, { method: "POST", body: file });
      return string(object(await response.json()).stage_id);
    },
    /** 在后台用冻结规则处理原件，预览任务完成后才可以确认。 */
    async previewImport(
      stageId: string,
      rules: {
        engine: string;
        ocr_enabled?: boolean;
        ocr_mode?: "auto" | "full_page";
        table_mode?: "fast" | "accurate";
        table_structure?: boolean;
        processing?: ProcessingOptions;
        exclude_headers: boolean;
        exclude_images: boolean;
      },
      operationKey: string,
      signal: AbortSignal,
    ): Promise<string> {
      return job(
        await json(
          `/knowledge-bases/${encodeURIComponent(kbId)}/imports/previews`,
          "POST",
          { stage_id: stageId, rules },
          operationKey,
          AbortSignal.any([signal, AbortSignal.timeout(15000)]),
        ),
      ).job_id;
    },
    /** 读取预览状态与有效正文。 */
    async importPreview(id: string, signal: AbortSignal) {
      const path = `/knowledge-bases/${encodeURIComponent(kbId)}/imports/previews/${encodeURIComponent(id)}`;
      const data = object(
        await (
          await request(path, {
            signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]),
          })
        ).json(),
      );
      const error = data.error == null ? null : object(data.error);
      return {
        status: string(data.status),
        chunk_count: nullableNumber(data.chunk_count),
        max_tokens: nullableNumber(data.max_tokens),
        effective_processing:
          data.effective_processing == null
            ? null
            : processingOptions(data.effective_processing),
        chunks: Array.isArray(data.chunks)
          ? data.chunks.map((value) => {
              const chunk = object(value);
              return {
                text: string(chunk.text),
                token_count: number(chunk.token_count),
                element_ref: nullableString(chunk.element_ref),
                page: nullableNumber(chunk.page),
              };
            })
          : [],
        chunks_truncated: data.chunks_truncated === true,
        text: typeof data.text === "string" ? data.text : "",
        before: typeof data.before === "string" ? data.before : "",
        blocks: Array.isArray(data.blocks) ? data.blocks.map(object) : [],
        truncated: data.truncated === true,
        warnings: Array.isArray(data.warnings) ? data.warnings.map(object) : [],
        errorCode: nullableString(data.error_code ?? null),
        suggestion: nullableString(error?.suggestion ?? null),
        error:
          typeof error?.message === "string"
            ? error.message
            : nullableString(data.error_code ?? null),
      };
    },
    /** 整批确认复用预览结果，重复请求返回原任务。 */
    async confirmImport(
      items: { preview_id: string; metadata: JsonObject }[],
    ): Promise<Job[]> {
      return array(
        await json(
          `/knowledge-bases/${encodeURIComponent(kbId)}/imports/confirm`,
          "POST",
          { items },
        ),
      ).map(job);
    },
    /** 直接上传原件，服务负责有界读取和解析，无服务器任意路径。 */
    async upload(
      file: File,
      metadata: JsonObject = {},
      parserEngine = "builtin",
      ocrEnabled?: boolean,
    ): Promise<Job> {
      const bytes = new Uint8Array(
        await crypto.subtle.digest("SHA-256", await file.arrayBuffer()),
      );
      const digest = Array.from(bytes, (value) =>
        value.toString(16).padStart(2, "0"),
      ).join("");
      const identity = new Uint8Array(
        await crypto.subtle.digest(
          "SHA-256",
          new TextEncoder().encode(
            JSON.stringify([
              kbId,
              file.name,
              digest,
              metadata,
              parserEngine,
              ocrEnabled,
            ]),
          ),
        ),
      );
      const storageKey =
        "inkrag.upload.v1." +
        Array.from(identity, (value) =>
          value.toString(16).padStart(2, "0"),
        ).join("");
      const operationKey =
        sessionStorage.getItem(storageKey) ?? crypto.randomUUID();
      sessionStorage.setItem(storageKey, operationKey);
      const accepted = job(
        await (
          await request(
            `/knowledge-bases/${encodeURIComponent(kbId)}/documents?filename=${encodeURIComponent(file.name)}&parser_engine=${encodeURIComponent(parserEngine)}${ocrEnabled === undefined ? "" : `&ocr_enabled=${ocrEnabled}`}`,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/octet-stream",
                "X-Ink-Metadata": encodeURIComponent(JSON.stringify(metadata)),
                "Idempotency-Key": operationKey,
              },
              body: file,
            },
          )
        ).json(),
      );
      sessionStorage.removeItem(storageKey);
      return accepted;
    },
    /** 携带预期版本保存修订，冲突不覆盖他人的新版本。 */
    async revise(
      document: DocumentDetail,
      changes: Record<string, string>,
    ): Promise<DocumentDetail> {
      return detail(
        await json(
          `/documents/${encodeURIComponent(document.document_id)}`,
          "PATCH",
          {
            expected_result_id: document.result_id,
            changes: Object.entries(changes).map(([element_ref, text]) => ({
              element_ref,
              text,
            })),
          },
        ),
      );
    },
    /** 显式将所选版本提交知识库，返回成功后界面才更新入库状态。 */
    async ingest(document: DocumentDetail): Promise<Job> {
      return job(
        await json(
          `/documents/${encodeURIComponent(document.document_id)}/ingest`,
          "POST",
          {
            expected_result_id: document.result_id,
            expected_document_revision: document.revision,
          },
          `ingest:${document.document_id}:${document.revision}:${document.result_id}`,
        ),
      );
    },
    /** 查询真实索引，空结果与服务错误保持不同语义。 */
    async retrieve(
      query: string,
      topK?: number,
      mode?: RetrievalMode,
      filters: JsonObject[] = [],
    ): Promise<PublishedSearchResult> {
      const row = object(
        await json("/retrieve", "POST", {
          kb_id: kbId,
          query,
          top_k: topK,
          mode,
          filters: filters.length ? filters : undefined,
        }),
      );
      const effective = object(row.effective_request);
      const effectiveMode = effective.mode;
      if (
        effectiveMode !== "hybrid" &&
        effectiveMode !== "vector" &&
        effectiveMode !== "keyword"
      )
        throw new ApiError("invalid_response", "服务返回了未知的检索方式。");
      return {
        trace_id: string(row.trace_id),
        snapshot_id: nullableString(row.snapshot_id),
        release_id: string(row.release_id),
        effective_request: {
          query: string(effective.query),
          mode: effectiveMode,
          top_k: number(effective.top_k),
          filters: array(effective.filters).map(object),
        },
        hits: array(row.hits).map((item) => {
          const hit = object(item);
          return {
            text: string(hit.text),
            score: nullableNumber(hit.score),
            metadata: object(hit.metadata),
          };
        }),
      };
    },
    /** 显式生成带引用回答，沿用同一套检索请求参数。 */
    async answer(
      query: string,
      topK?: number,
      mode?: "hybrid" | "vector" | "keyword",
      filters: JsonObject[] = [],
    ): Promise<AnswerResult> {
      const row = object(
        await json(
          `/knowledge-bases/${encodeURIComponent(kbId)}/answers`,
          "POST",
          {
            query,
            top_k: topK,
            mode,
            filters,
          },
        ),
      );
      if (row.status !== "answered" && row.status !== "insufficient")
        throw new ApiError("invalid_response", "回答状态无效");
      return {
        status: row.status,
        model: string(row.model),
        trace_id: string(row.trace_id),
        statements: array(row.statements).map((value) => {
          const statement = object(value);
          return {
            text: string(statement.text),
            citations: array(statement.citations).map((value) => {
              const citation = object(value);
              return {
                source_id: string(citation.source_id),
                quote: string(citation.quote),
              };
            }),
          };
        }),
        sources: array(row.sources).map((value) => {
          const source = object(value);
          return {
            source_id: string(source.source_id),
            document_id: string(source.document_id),
            result_id: string(source.result_id),
            element_ref: string(source.element_ref),
            picture_ref:
              source.picture_origin == null
                ? null
                : string(object(source.picture_origin).element_ref),
            page: nullableNumber(source.page),
            name: string(source.name),
          };
        }),
      };
    },
    /** 图意生成只返回草稿，用户确认后才写入审校编辑。 */
    async inspectPicture(
      id: string,
      resultId: string,
      ref: string,
      signal: AbortSignal,
    ): Promise<PictureSuggestion> {
      const row = object(
        await json(
          `/documents/${encodeURIComponent(id)}/vision`,
          "POST",
          {
            result_id: resultId,
            element_ref: ref,
          },
          undefined,
          signal,
        ),
      );
      return {
        model: string(row.model),
        description: string(row.description),
        relationships: array(row.relationships).map(string),
        uncertainties: array(row.uncertainties).map(string),
        suggested_text: nullableString(row.suggested_text),
      };
    },
    /** 受保护资产通过带令牌的有界队列读取。 */
    asset,
    /** 导出所选版本对应的服务端文件。 */
    async export(
      id: string,
      format: "json" | "markdown",
      resultId: string,
    ): Promise<Blob> {
      return (
        await request(
          `/documents/${encodeURIComponent(id)}/export?format=${format}&result_id=${encodeURIComponent(resultId)}`,
        )
      ).blob();
    },
  };
}
export type Client = ReturnType<typeof client>;

export interface MetadataField {
  name: string;
  label: string;
  type: string;
  required: boolean;
  options: string[];
}
export interface KnowledgeBase {
  auto_activate: boolean;
  kind: "knowledge" | "parse";
  kb_id: string;
  name: string;
  revision: number;
  fields: MetadataField[];
  active_release_id: string | null;
}
export interface Job {
  result?: JsonObject | null;
  kb_id: string;
  error_diagnostic: JsonObject | null;
  job_id: string;
  document_id: string | null;
  kind: string;
  status: string;
  stage: string;
  attempt: number;
  error_code: string | null;
  updated_at: string;
}
export interface Build {
  build_id: string;
  status: string;
  created_at: string;
  index_name: string;
}
export interface Release {
  release_id: string;
  build_id: string;
  version: number;
  created_at: string;
}
export interface Caller {
  key_id: string;
  name: string;
  revoked: boolean;
}
export interface Audit {
  event_id: string;
  subject: string;
  action: string;
  target_id: string;
  created_at: string;
}
/** 解码知识库身份与当前已发布指针。 */
export function knowledgeBase(value: unknown): KnowledgeBase {
  const r = object(value);
  return {
    auto_activate: r.auto_activate !== false,
    kb_id: string(r.kb_id),
    kind: r.kind === "parse" ? "parse" : "knowledge",
    name: string(r.name),
    active_release_id: nullableString(r.active_release_id),
    revision: number(r.revision),
    fields: array(r.fields ?? []).map((value) => {
      const f = object(value);
      return {
        name: string(f.name),
        label: string(f.label),
        type: string(f.type),
        required: f.required === true,
        options: array(f.options).map(string),
      };
    }),
  };
}
/** 任务阶段由worker真实持久化，不生成虚构百分比。 */
export function job(value: unknown): Job {
  const r = object(value);
  return {
    result: r.result == null ? null : object(r.result),
    job_id: string(r.job_id),
    kb_id: string(r.kb_id),
    error_diagnostic:
      r.error_diagnostic == null ? null : object(r.error_diagnostic),
    document_id: nullableString(r.document_id),
    kind: string(r.kind),
    status: string(r.status),
    stage: string(r.stage),
    attempt: number(r.attempt),
    error_code: nullableString(r.error_code),
    updated_at: string(r.updated_at),
  };
}
/** 构建完成与发布可见是两种独立事实。 */
function build(value: unknown): Build {
  const r = object(value);
  return {
    build_id: string(r.build_id),
    status: string(r.status),
    created_at: string(r.created_at),
    index_name: string(r.index_name),
  };
}
/** 发布版本由服务端条件切换，客户端不能指定物理索引。 */
function release(value: unknown): Release {
  const r = object(value);
  return {
    release_id: string(r.release_id),
    build_id: string(r.build_id),
    version: number(r.version),
    created_at: string(r.created_at),
  };
}

export interface Sample {
  sample_id: string;
  document_level: boolean;
  query: string;
  expected_document_ids: string[];
}
export interface Evaluation {
  evaluation_id: string;
  release_id: string;
  name: string;
  created_at: string;
  sample_count: number;
  recall: number;
  precision: number;
  mrr: number;
}

export interface ServiceStatus {
  ready: boolean;
  mode: string;
  model_identity: JsonObject | null;
}
