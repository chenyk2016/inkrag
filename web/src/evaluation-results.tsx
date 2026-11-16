/** 逐题报告只解释服务保存的评测事实，正文按绑定的解析版本读取。 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Tag } from "antd";
import {
  array,
  number,
  object,
  string,
  type JsonObject,
  type ParsedResult,
} from "./api";
import { Failure } from "./application";
import type { PlatformClient } from "./platform-api";
import "./evaluation-results.css";

interface EvidenceSource {
  documentId: string;
  resultId?: string;
  elementRef?: string;
  page?: number;
}
interface QuestionResult {
  sampleId: string;
  query: string;
  annotationRevision: number;
  noAnswer: boolean;
  noAnswerPassed: boolean | null;
  recall: number | null;
  precision: number | null;
  mrr: number | null;
  evidenceRecall: number | null;
  expected: EvidenceSource[];
  actual: EvidenceSource[];
  missing: EvidenceSource[];
  traceId: string;
}
type OpenSource = (id: string, resultId?: string, ref?: string) => void;

/** 空指标表示未标注该维度，不将 null 伪装为零分或满分。 */
function metric(value: unknown): number | null {
  return value == null ? null : number(value);
}
/** 来源只接受后端提供的身份，不从标题或页码猜造块引用。 */
function source(value: unknown): EvidenceSource {
  const row = object(value);
  return {
    documentId: string(row.document_id),
    resultId: row.result_id == null ? undefined : string(row.result_id),
    elementRef: row.element_ref == null ? undefined : string(row.element_ref),
    page: row.page == null ? undefined : number(row.page),
  };
}
/** 文档级旧标注仍可阅读，但不假装绑定过未提供的历史版本。 */
function sources(values: unknown, documents: unknown): EvidenceSource[] {
  const detailed = array(values).map(source);
  return detailed.length
    ? detailed
    : array(documents).map((id) => ({ documentId: string(id) }));
}
/** 将逐题外部响应收窄，未知结构仅使该报告显示错误并保留原始记录。 */
function question(value: unknown): QuestionResult {
  const row = object(value);
  if (typeof row.no_answer !== "boolean")
    throw new Error("评测报告缺少样本类型，请查看原始记录。");
  if (row.no_answer_passed != null && typeof row.no_answer_passed !== "boolean")
    throw new Error("评测报告的无答案结论无效。");
  return {
    sampleId: string(row.sample_id),
    query: string(row.query),
    annotationRevision: number(row.annotation_revision),
    noAnswer: row.no_answer,
    noAnswerPassed: row.no_answer_passed == null ? null : row.no_answer_passed,
    recall: metric(row.recall),
    precision: metric(row.precision),
    mrr: metric(row.mrr),
    evidenceRecall: metric(row.evidence_recall),
    expected: sources(row.expected_sources, row.expected_document_ids),
    actual: sources(row.actual_sources, row.actual_document_ids),
    missing: sources(row.missing_sources ?? [], row.missing_document_ids ?? []),
    traceId: string(row.trace_id),
  };
}
/** 展示保存版本中的原文块；历史请求未存片段时不声称是当时的切片全文。 */
function sourceText(result: ParsedResult, item: EvidenceSource): string {
  const texts = result.texts
    .filter((value) =>
      item.elementRef
        ? value.self_ref === item.elementRef
        : item.page == null || value.page === item.page,
    )
    .map((value) => value.text);
  const tables = result.tables
    .filter((value) =>
      item.elementRef
        ? value.self_ref === item.elementRef
        : item.page == null || value.page === item.page,
    )
    .map((value) => value.cells.map((cell) => cell.text).join(" · "));
  const pictures = result.pictures
    .filter((value) =>
      item.elementRef
        ? value.self_ref === item.elementRef
        : item.page == null || value.page === item.page,
    )
    .map((value) =>
      [value.caption, result.editor.picture_descriptions[value.self_ref]]
        .filter(Boolean)
        .join("\n"),
    );
  const text = [...texts, ...tables, ...pictures].filter(Boolean).join("\n");
  if (!text)
    return item.elementRef
      ? "该保存版本的此内容块没有可展示的文字，请打开原文核对。"
      : "该保存版本没有可展示的文字，请打开原文核对。";
  return text.length > 1600
    ? `${text.slice(0, 1600)}…（打开原文查看完整内容）`
    : text;
}
/** 同一固定版本通过查询缓存共享；读取失败不回退到最新版本。 */
function SourceCard({
  api,
  item,
  onOpen,
}: {
  api: PlatformClient;
  item: EvidenceSource;
  onOpen: OpenSource;
}) {
  const document = useQuery({
    queryKey: [
      "evaluation-source",
      api.scopeId,
      item.documentId,
      item.resultId ?? "unbound",
    ],
    queryFn: ({ signal }) =>
      api.document(item.documentId, item.resultId, signal),
    // 正文版本不可变，读取权限仍会变化；挂载或重新聚焦必须重新核实。
    staleTime: 0,
    retry: false,
  });
  return (
    <article className="evaluation-source">
      <strong>
        {document.data?.name ?? `资料 ${item.documentId.slice(0, 12)}`}
      </strong>
      <small>
        {item.resultId
          ? `保存版本 ${item.resultId.slice(0, 12)}`
          : "文档级标注未绑定版本；下方为当前正文参考"}
        {item.elementRef ? ` · ${item.elementRef}` : ""}
        {item.page == null ? "" : ` · 第 ${item.page} 页`}
      </small>
      {document.isPending ? (
        <p role="status">正在读取来源正文…</p>
      ) : document.isError ? (
        <>
          <Failure error={document.error} />
          <p>来源无法读取，未改用其他版本。请检查资料权限或保留状态。</p>
        </>
      ) : (
        <p className="evaluation-source-body">
          {sourceText(document.data.result, item)}
        </p>
      )}
      <Button
        size="small"
        type="default"
        onClick={() => onOpen(item.documentId, item.resultId, item.elementRef)}
      >
        打开原文{item.elementRef ? "并定位此块" : ""}
      </Button>
    </article>
  );
}
/** 指标使用中文含义，文档级和条款级分别展示。 */
function Metric({ label, value }: { label: string; value: number | null }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value === null ? "未标注" : `${(value * 100).toFixed(1)}%`}</dd>
    </div>
  );
}
/** 失败题同时展示应找到、实际找到和遗漏来源，让整改有具体入口。 */
function QuestionCard({
  api,
  row,
  index,
  onOpen,
  onTrace,
}: {
  api: PlatformClient;
  row: QuestionResult;
  index: number;
  onOpen: OpenSource;
  onTrace: (traceId: string) => void;
}) {
  const score = row.evidenceRecall ?? row.recall;
  const passed = row.noAnswer
    ? row.noAnswerPassed
    : score === null
      ? null
      : score === 1;
  return (
    <article className="evaluation-question">
      <div className="evaluation-question-heading">
        <h4>
          第 {index + 1} 题 · {row.query}
        </h4>
        <Tag color={passed === null ? "default" : passed ? "green" : "orange"}>
          {passed === null
            ? "结论未提供"
            : row.noAnswer
              ? passed
                ? "无答案检查通过"
                : "发生误召回"
              : passed
                ? "预期来源已找全"
                : "存在遗漏"}
        </Tag>
      </div>
      <small>
        标注 v{row.annotationRevision} · 样本 {row.sampleId.slice(0, 12)}
      </small>
      {row.noAnswer ? (
        <p>
          {row.noAnswerPassed === true
            ? "本题预期无结果，实际未检索到资料。此结论单独计算，不计入正例 Recall。"
            : row.noAnswerPassed === false
              ? "本题预期无结果，实际返回了资料。请核对下方误召回来源。"
              : "服务未提供无答案检查结论。"}
        </p>
      ) : (
        <dl className="evaluation-metrics">
          <Metric label="文档召回 Recall" value={row.recall} />
          <Metric label="文档准确 Precision" value={row.precision} />
          <Metric label="首条相关 MRR" value={row.mrr} />
          <Metric label="条款召回" value={row.evidenceRecall} />
        </dl>
      )}
      <div className="evaluation-evidence-columns">
        {!row.noAnswer && (
          <section>
            <h5>应找到的来源（{row.expected.length}）</h5>
            {row.expected.map((item, i) => (
              <SourceCard key={i} api={api} item={item} onOpen={onOpen} />
            ))}
          </section>
        )}
        <section>
          <h5>
            {row.noAnswer ? "实际误召回" : "实际命中来源"}（{row.actual.length}
            ）
          </h5>
          {row.actual.length ? (
            row.actual.map((item, i) => (
              <SourceCard key={i} api={api} item={item} onOpen={onOpen} />
            ))
          ) : (
            <p className="empty-copy">
              实际返回 0 条，零命中已保留在本题结果中。
            </p>
          )}
        </section>
        {!row.noAnswer && (
          <section
            className={row.missing.length ? "evaluation-missing" : undefined}
          >
            <h5>遗漏来源（{row.missing.length}）</h5>
            {row.missing.length ? (
              row.missing.map((item, i) => (
                <SourceCard key={i} api={api} item={item} onOpen={onOpen} />
              ))
            ) : (
              <p>没有已记录的遗漏来源。</p>
            )}
          </section>
        )}
      </div>
      <p className="help-copy">
        正文按来源引用读取保存版本中的内容块，用于核对原文；不是历史检索切片的全文快照。结果与指标仍以本次评测记录为准。
      </p>
      <Button type="default" onClick={() => onTrace(row.traceId)}>
        诊断此次检索 · {row.traceId.slice(0, 12)}
      </Button>
    </article>
  );
}
/** 每份报告独立解码，坏记录不会遮住其余报告或丢失原始诊断资料。 */
export function EvaluationReport({
  api,
  row,
  onOpen,
  onTrace,
}: {
  api: PlatformClient;
  row: JsonObject;
  onOpen: OpenSource;
  onTrace: (traceId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  let questions: QuestionResult[] = [],
    metrics: JsonObject = {},
    error: unknown = null;
  try {
    questions = array(row.details).map(question);
    metrics = object(row.metrics);
    for (const key of [
      "recall",
      "precision",
      "mrr",
      "evidence_recall",
      "no_answer_accuracy",
    ])
      metric(metrics[key]);
    for (const key of [
      "sample_count",
      "positive_count",
      "evidence_labeled_count",
      "no_answer_count",
      "top_k",
    ])
      number(metrics[key]);
  } catch (reason: unknown) {
    error = reason;
  }
  return (
    <section className="surface evaluation-report">
      <h3>{typeof row.name === "string" ? row.name : "评测报告"}</h3>
      <small>
        候选 {typeof row.build_id === "string" ? row.build_id : "未提供"} ·{" "}
        {typeof row.created_at === "string"
          ? new Date(row.created_at).toLocaleString()
          : "时间未提供"}
      </small>
      {error !== null ? (
        <Failure error={error} />
      ) : (
        <>
          <p>
            共 {String(metrics.sample_count)} 题：正例{" "}
            {String(metrics.positive_count)} 题，条款标注{" "}
            {String(metrics.evidence_labeled_count)} 题，无答案{" "}
            {String(metrics.no_answer_count)} 题。实际 Top K：
            {String(metrics.top_k)}。
          </p>
          <dl className="evaluation-metrics">
            <Metric label="正例文档 Recall" value={metric(metrics.recall)} />
            <Metric
              label="正例文档 Precision"
              value={metric(metrics.precision)}
            />
            <Metric label="正例 MRR" value={metric(metrics.mrr)} />
            <Metric label="条款召回" value={metric(metrics.evidence_recall)} />
            <Metric
              label="无答案检查通过率"
              value={metric(metrics.no_answer_accuracy)}
            />
          </dl>
          <p className="help-copy">
            文档级指标按资料去重，条款按人工绑定的结果与内容块核对。无答案单列；这些指标不代表生成答案正确率。
          </p>
          <details
            open={expanded}
            onToggle={(event) => setExpanded(event.currentTarget.open)}
          >
            <summary>逐题结果与来源（{questions.length}）</summary>
            {expanded &&
              questions.map((question, index) => (
                <QuestionCard
                  key={question.sampleId}
                  api={api}
                  row={question}
                  index={index}
                  onOpen={onOpen}
                  onTrace={onTrace}
                />
              ))}
          </details>
        </>
      )}
      <details>
        <summary>原始评测记录（JSON）</summary>
        <pre>{JSON.stringify(row, null, 2)}</pre>
      </details>
    </section>
  );
}
