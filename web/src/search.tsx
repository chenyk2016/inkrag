import { Button, Input, Select, Collapse, Segmented } from "antd";
/** 对真实已入库知识发起查询，命中可回到资料及指定解析版本。 */
import { useEffect, useState, type FormEvent } from "react";
import {
  ArrowUpRight,
  FileSearch,
  FileText,
  LoaderCircle,
  Search,
  Sparkles,
  Plus,
  X,
} from "lucide-react";
import {
  ApiError,
  object,
  type AnswerResult,
  type Client,
  type Hit,
  type PublishedSearchResult,
  type RetrievalMode,
} from "./api";
import { AssetImage } from "./assets";

/** 读取来源中的可选文字字段，不对未知 metadata 执行渲染或脚本。 */
function metadataString(hit: Hit, name: string): string | null {
  const value = hit.metadata[name];
  return typeof value === "string" ? value : null;
}
/** 读取可选的解析来源，对缺失或不同版本保持可解释的展示。 */
function parseOrigin(hit: Hit): Record<string, unknown> {
  try {
    return object(hit.metadata.parse_origin);
  } catch {
    return {};
  }
}

/** 展示单次命中的原文及稳定资源关联，分数不包装成正确率。 */
function SearchHit({
  hit,
  index,
  api,
  onOpen,
}: {
  hit: Hit;
  index: number;
  api: Client;
  onOpen: (id: string, resultId?: string, elementRef?: string) => void;
}) {
  const origin = parseOrigin(hit);
  const documentId = metadataString(hit, "document_id");
  const resultId =
    typeof origin.result_id === "string" ? origin.result_id : undefined;
  const originalName =
    typeof origin.original_name === "string"
      ? origin.original_name
      : "资料原文";
  const page = typeof hit.metadata.page === "number" ? hit.metadata.page : null;
  const images = Array.isArray(origin.related_asset_ids)
    ? origin.related_asset_ids.filter(
        (value): value is string => typeof value === "string",
      )
    : [];
  const picture = origin.picture_origin;
  const pictureRef =
    picture &&
    typeof picture === "object" &&
    "element_ref" in picture &&
    typeof picture.element_ref === "string"
      ? picture.element_ref
      : undefined;
  return (
    <article className="search-hit">
      <div className="hit-header">
        <span className="hit-index">{index + 1}</span>
        <div className="hit-source">
          <FileText size={15} />
          <strong>{originalName}</strong>
          {page && <span>第 {page} 页</span>}
        </div>
        <span className="score">
          检索分数 {hit.score === null ? "—" : hit.score.toFixed(3)}
        </span>
      </div>
      <p className="hit-text">{hit.text}</p>
      {images.length > 0 && (
        <div className="hit-resources">
          <span>{pictureRef ? "经审校的图片证据" : "相关图片"}</span>
          {!pictureRef && <small>来源中的同页或绑定资源。</small>}
          <div className="hit-images">
            {images.slice(0, 3).map((id) => (
              <AssetImage
                key={id}
                asset={{
                  asset_id: id,
                  element_ref: "来源资源",
                  role: "image",
                  page,
                  media_type: "image/png",
                }}
                api={api}
              />
            ))}
          </div>
        </div>
      )}
      {documentId && (
        <Button
          type="link"
          onClick={() =>
            onOpen(
              documentId,
              resultId,
              pictureRef ??
                (typeof origin.element_ref === "string"
                  ? origin.element_ref
                  : undefined),
            )
          }
        >
          查看来源版本 <ArrowUpRight size={14} />
        </Button>
      )}
    </article>
  );
}

interface FilterRow {
  id: string;
  field: string;
  operator: string;
  value: string;
}
const MODES = [
  { value: "keyword", label: "关键词 BM25" },
  { value: "vector", label: "语义向量" },
  { value: "hybrid", label: "混合检索" },
] as const;

/** 用同一组界面名称标记请求和结果。 */
function modeLabel(mode: RetrievalMode): string {
  return MODES.find((option) => option.value === mode)?.label ?? mode;
}

/** 查询仅在用户提交后执行，保留加载、空结果与故障的不同反馈。 */
export function SearchView({
  api,
  canAnswer,
  onOpen,
  onError,
  onTrace,
}: {
  api: Client;
  canAnswer: boolean;
  onOpen: (id: string, resultId?: string, elementRef?: string) => void;
  onError: (error: unknown) => void;
  onTrace: (trace: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState<number>();
  const [mode, setMode] = useState<RetrievalMode>();
  const [filterText, setFilterText] = useState("");
  const [schema, setSchema] = useState<Record<string, string>>({}),
    [filters, setFilters] = useState<FilterRow[]>([]);
  useEffect(() => {
    let alive = true;
    void api
      .fields()
      .then((fields) => {
        if (alive) setSchema(fields);
      })
      .catch((e: unknown) => {
        if (
          alive &&
          (!(e instanceof ApiError) ||
            !["not_published", "authorization_pending"].includes(e.code))
        )
          onError(e);
      });
    return () => {
      alive = false;
    };
  }, [api, onError]);
  /** 可视条件只允许已发布声明字段，原生标量保留类型。 */
  function compiledFilters(): Record<string, unknown>[] {
    return filters.map((filter) => {
      let value: unknown = filter.value;
      if (filter.operator === "in") {
        value = JSON.parse(filter.value);
        if (!Array.isArray(value))
          throw new ApiError("invalid_filter", "包含集合需要JSON数组。");
      } else if (schema[filter.field] === "number") {
        value = Number(filter.value);
        if (!filter.value.trim() || !Number.isFinite(value))
          throw new ApiError("invalid_filter", "数值条件需要有限数字。");
      } else if (schema[filter.field] === "boolean")
        value = filter.value === "true";
      return { field: filter.field, operator: filter.operator, value };
    });
  }
  /** 修改可视条件不会立刻触发检索。 */
  function editFilter(id: string, change: Partial<FilterRow>): void {
    setFilters((before) =>
      before.map((filter) =>
        filter.id === id ? { ...filter, ...change } : filter,
      ),
    );
  }
  const [results, setResults] = useState<PublishedSearchResult[]>([]);
  const [activeMode, setActiveMode] = useState<RetrievalMode>("hybrid");
  const result = results.find(
    (item) => item.effective_request.mode === activeMode,
  );
  const [answer, setAnswer] = useState<AnswerResult | null>(null);
  const [pending, setPending] = useState<
    "search" | "answer" | "compare" | null
  >(null);
  const busy = pending !== null;
  const [searchedQuery, setSearchedQuery] = useState("");
  /** 对照按相同条件串行查询；任一次失败或发布变化都不会展示为完整对照。 */
  async function submit(
    action: "search" | "answer" | "compare",
  ): Promise<void> {
    if (busy || !query.trim()) return;
    setPending(action);
    setResults([]);
    setAnswer(null);
    try {
      const raw: unknown = filterText.trim()
        ? JSON.parse(filterText)
        : compiledFilters();
      if (!Array.isArray(raw))
        throw new ApiError("invalid_filter", "筛选条件必须是JSON数组。");
      const question = query.trim();
      const conditions = raw.map(object);
      if (action === "answer") {
        setAnswer(await api.answer(question, topK, mode, conditions));
      } else if (action === "compare") {
        const comparison: PublishedSearchResult[] = [];
        for (const option of MODES) {
          const entry = await api.retrieve(
            question,
            topK,
            option.value,
            conditions,
          );
          if (
            comparison.length &&
            entry.release_id !== comparison[0].release_id
          )
            throw new ApiError(
              "release_changed",
              "对照期间线上发布已切换，请重新对照。",
            );
          comparison.push(entry);
        }
        setActiveMode("hybrid");
        setResults(comparison);
      } else {
        const entry = await api.retrieve(question, topK, mode, conditions);
        setActiveMode(entry.effective_request.mode);
        setResults([entry]);
      }
      setSearchedQuery(question);
    } catch (error: unknown) {
      onError(
        error instanceof Error
          ? error
          : new ApiError("query_failed", "检索未能完成。"),
      );
    } finally {
      setPending(null);
    }
  }
  return (
    <div className="search-page">
      <div className="section-intro">
        <div className="section-icon">
          <FileSearch size={23} />
        </div>
        <div>
          <h2>试问当前线上知识</h2>
          <p>输入一个实际问题，查看知识库返回的依据。</p>
        </div>
      </div>
      <div className="retrieval-grid">
        <form
          className="search-form"
          onSubmit={(event: FormEvent<HTMLFormElement>) => {
            event.preventDefault();
            void submit("search");
          }}
        >
          <div className="query-input">
            <Search size={20} />
            <label className="sr-only" htmlFor="query">
              检索问题
            </label>
            <Input
              id="query"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="例如：E102 故障应该怎么处理？"
              maxLength={4000}
              required
              disabled={busy}
              autoComplete="off"
            />
          </div>
          <div className="inline-actions">
            <Button
              type="primary"
              htmlType="submit"
              loading={pending === "search"}
              icon={<Search size={16} />}
              disabled={busy || !query.trim()}
            >
              检索
            </Button>
            <Button
              loading={pending === "compare"}
              disabled={busy || !query.trim()}
              onClick={() => void submit("compare")}
            >
              对照三种方式
            </Button>
            {canAnswer && (
              <Button
                icon={<Sparkles size={16} />}
                loading={pending === "answer"}
                disabled={busy || !query.trim()}
                onClick={() => void submit("answer")}
              >
                生成带引用回答
              </Button>
            )}
          </div>
          <details className="filter-panel search-options">
            <summary>检索选项</summary>
            <div className="inline-actions">
              <label className="top-k">
                检索方式
                <Select
                  aria-label="检索方式"
                  value={mode}
                  placeholder="使用线上设置"
                  allowClear
                  disabled={busy}
                  options={[...MODES]}
                  onChange={(value: string | undefined) => {
                    if (
                      value === undefined ||
                      value === "hybrid" ||
                      value === "vector" ||
                      value === "keyword"
                    )
                      setMode(value);
                  }}
                />
              </label>
              <label className="top-k">
                结果数
                <Select
                  aria-label="结果数"
                  value={topK}
                  placeholder="使用线上设置"
                  allowClear
                  onChange={(value: number) => setTopK(value)}
                  options={[3, 5, 10].map((value) => ({
                    value,
                    label: String(value),
                  }))}
                  disabled={busy}
                />
              </label>
            </div>
          </details>
          <Collapse
            className="filter-panel"
            items={[
              {
                key: "filters",
                label: `业务字段筛选 · ${filters.length} 个条件（全部满足）`,
                children: (
                  <>
                    {!Object.keys(schema).length ? (
                      <p>
                        当前发布没有可筛选的业务字段。请先定义字段、填写业务字段并发布。
                      </p>
                    ) : (
                      <>
                        {filters.map((filter) => (
                          <div className="filter-row" key={filter.id}>
                            <Select
                              aria-label="筛选字段"
                              disabled={busy}
                              value={filter.field}
                              options={Object.entries(schema).map(
                                ([name, type]) => ({
                                  value: name,
                                  label: `${name} · ${type}`,
                                }),
                              )}
                              onChange={(value: string) =>
                                editFilter(filter.id, {
                                  field: value,
                                  operator: "eq",
                                  value:
                                    schema[value] === "boolean" ? "true" : "",
                                })
                              }
                            />
                            <Select
                              aria-label="筛选运算"
                              disabled={busy}
                              value={filter.operator}
                              options={[
                                { value: "eq", label: "等于" },
                                { value: "ne", label: "不等于" },
                                { value: "in", label: "包含于集合" },
                                ...(schema[filter.field] === "text"
                                  ? [{ value: "contains", label: "包含文字" }]
                                  : schema[filter.field] !== "boolean"
                                    ? [
                                        { value: "gt", label: "大于" },
                                        { value: "gte", label: "大于等于" },
                                        { value: "lt", label: "小于" },
                                        { value: "lte", label: "小于等于" },
                                      ]
                                    : []),
                              ]}
                              onChange={(value: string) =>
                                editFilter(filter.id, { operator: value })
                              }
                            />
                            {schema[filter.field] === "boolean" &&
                            filter.operator !== "in" ? (
                              <Select
                                aria-label="筛选值"
                                disabled={busy}
                                value={filter.value}
                                options={[
                                  { value: "true", label: "是" },
                                  { value: "false", label: "否" },
                                ]}
                                onChange={(value: string) =>
                                  editFilter(filter.id, { value })
                                }
                              />
                            ) : (
                              <Input
                                aria-label="筛选值"
                                disabled={busy}
                                value={filter.value}
                                placeholder={
                                  filter.operator === "in"
                                    ? '["财务","研发"]'
                                    : "条件值"
                                }
                                onChange={(event) =>
                                  editFilter(filter.id, {
                                    value: event.target.value,
                                  })
                                }
                              />
                            )}
                            <Button
                              type="text"
                              aria-label="移除筛选条件"
                              disabled={busy}
                              icon={<X size={14} />}
                              onClick={() =>
                                setFilters((before) =>
                                  before.filter((row) => row.id !== filter.id),
                                )
                              }
                            />
                          </div>
                        ))}
                        <Button
                          type="link"
                          icon={<Plus size={14} />}
                          disabled={busy || filters.length >= 20}
                          onClick={() => {
                            const field = Object.keys(schema)[0];
                            setFilters((before) => [
                              ...before,
                              {
                                id: crypto.randomUUID(),
                                field,
                                operator: "eq",
                                value:
                                  schema[field] === "boolean" ? "true" : "",
                              },
                            ]);
                          }}
                        >
                          添加筛选条件
                        </Button>
                      </>
                    )}
                    <Collapse
                      ghost
                      items={[
                        {
                          key: "json",
                          label: "高级 JSON 条件（填写时替代以上条件）",
                          children: (
                            <Input.TextArea
                              aria-label="元数据筛选JSON"
                              value={filterText}
                              onChange={(event) =>
                                setFilterText(event.target.value)
                              }
                              placeholder='[{"field":"department","operator":"eq","value":"财务"}]'
                              disabled={busy}
                              autoSize={{ minRows: 2, maxRows: 6 }}
                            />
                          ),
                        },
                      ]}
                    />
                  </>
                ),
              },
            ]}
          />
        </form>
        <section className="retrieval-results" aria-label="检索结果">
          {busy ? (
            <div className="empty search-empty">
              <LoaderCircle size={30} className="spin" />
              <h3>
                {pending === "answer"
                  ? "正在检索并核对引用"
                  : pending === "compare"
                    ? "正在对照关键词、向量与混合结果"
                    : "正在查询知识索引"}
              </h3>
              <p>
                {pending === "answer"
                  ? "回答将显示引用原文和来源位置。"
                  : "查询完成后将显示原文证据。"}
              </p>
            </div>
          ) : answer ? (
            <div className="search-results" aria-live="polite">
              <div className="results-summary">
                <strong>“{searchedQuery}”</strong>
                <Button onClick={() => onTrace(answer.trace_id)}>
                  查看本次诊断
                </Button>
              </div>
              {answer.status === "insufficient" ? (
                <div className="empty search-empty">
                  <FileSearch size={34} />
                  <h3>当前资料的证据不足</h3>
                  <p>可以检索原文，或调整问题及筛选条件。</p>
                </div>
              ) : (
                answer.statements.map((statement, index) => (
                  <article className="search-hit" key={index}>
                    <p className="hit-text">{statement.text}</p>
                    {statement.citations.map((citation, citationIndex) => {
                      const source = answer.sources.find(
                        (item) => item.source_id === citation.source_id,
                      );
                      return (
                        source && (
                          <details
                            className="request-snapshot"
                            key={citationIndex}
                          >
                            <summary>
                              {source.name}
                              {source.page ? ` · 第 ${source.page} 页` : ""}
                            </summary>
                            <blockquote className="hit-text">
                              {citation.quote}
                            </blockquote>
                            <Button
                              type="link"
                              onClick={() =>
                                onOpen(
                                  source.document_id,
                                  source.result_id,
                                  source.picture_ref ?? source.element_ref,
                                )
                              }
                            >
                              {source.picture_ref ? "查看原图" : "查看来源版本"}{" "}
                              <ArrowUpRight size={14} />
                            </Button>
                          </details>
                        )
                      );
                    })}
                  </article>
                ))
              )}
            </div>
          ) : result ? (
            <>
              {results.length > 1 && (
                <Segmented
                  aria-label="检索方式对照"
                  block
                  value={activeMode}
                  onChange={setActiveMode}
                  options={results.map((item) => ({
                    value: item.effective_request.mode,
                    label: `${modeLabel(item.effective_request.mode)} · ${item.hits.length}`,
                  }))}
                />
              )}
              <div className="results-summary">
                <span>
                  “{searchedQuery}” <strong>{result.hits.length} 条结果</strong>
                </span>
                <Button onClick={() => onTrace(result.trace_id)}>
                  查看本次诊断
                </Button>
                <span title={`trace_id: ${result.trace_id}`}>
                  快照 {result.snapshot_id?.slice(0, 10) ?? "尚无知识"}
                </span>
              </div>
              <details className="request-snapshot">
                <summary>
                  {modeLabel(result.effective_request.mode)} · 最多{" "}
                  {result.effective_request.top_k} 条
                </summary>
                <p>结果属于提交时的请求，修改左侧参数后需重新检索。</p>
                <pre>{JSON.stringify(result.effective_request, null, 2)}</pre>
              </details>
              {result.hits.length ? (
                <div className="search-results">
                  {result.hits.map((hit, index) => (
                    <SearchHit
                      hit={hit}
                      index={index}
                      api={api}
                      onOpen={onOpen}
                      key={`${result.trace_id}-${index}`}
                    />
                  ))}
                </div>
              ) : (
                <div className="empty search-empty">
                  <FileSearch size={34} />
                  <h3>没有检索到资料</h3>
                  <p>先在资料工作台入库文档，再尝试具体的问题或关键词。</p>
                </div>
              )}
            </>
          ) : (
            <div className="empty search-empty">
              <div className="empty-search-icon">
                <Sparkles size={26} />
              </div>
              <h3>从一个你关心的问题开始</h3>
              <p>先解析、检查并入库资料，再在这里核对检索证据。</p>
              <div className="query-suggestions">
                {[
                  "E102 温度传感器故障怎么处理？",
                  "七天无理由退货需要满足哪些条件？",
                ].map((example) => (
                  <Button key={example} onClick={() => setQuery(example)}>
                    {example}
                    <ArrowUpRight size={13} />
                  </Button>
                ))}
              </div>
            </div>
          )}
        </section>
      </div>
      <div className="search-footnote">
        检索分数来自所选通道，不能视为准确率。这里展示来源原文，不生成回答。
      </div>
    </div>
  );
}
