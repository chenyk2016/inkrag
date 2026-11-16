/** 用人工标注文档级相关性衡量真实发布版本，指标不等同问答正确率。 */
import { Button, Input, Select, Table } from "antd";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import type { Client, DocumentSummary, Evaluation, Sample } from "./api";
/** 样本由用户明确标注，评测只提交任务并轮询实际持久结果。 */
export function EvaluationPane({
  api,
  activeReleaseId,
  canEdit,
  onError,
}: {
  api: Client;
  activeReleaseId: string | null;
  canEdit: boolean;
  onError: (e: unknown) => void;
}) {
  const [documents, setDocuments] = useState<DocumentSummary[]>([]),
    [samples, setSamples] = useState<Sample[]>([]),
    [results, setResults] = useState<Evaluation[]>([]),
    [query, setQuery] = useState(""),
    [selected, setSelected] = useState<string[]>([]),
    [name, setName] = useState(""),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(""),
    [topK, setTopK] = useState(5),
    [mode, setMode] = useState<"hybrid" | "vector" | "keyword">("hybrid");
  const refresh = useCallback(async () => {
    const [d, s, r] = await Promise.all([
      api.documents(),
      api.samples(),
      api.evaluations(),
    ]);
    setDocuments(d);
    setSamples(s.filter((sample) => sample.document_level));
    setResults(r);
  }, [api]);
  useEffect(() => {
    let alive = true;
    const run = () => {
      void refresh().catch((e: unknown) => {
        if (alive) onError(e);
      });
    };
    run();
    const timer = window.setInterval(run, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [refresh, onError]);
  /** 只有服务确认保存标注后清空输入，失败时保留用户选择。 */
  async function save(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy || !query.trim() || !selected.length) return;
    setBusy(true);
    setNotice("");
    try {
      await api.createSample(query.trim(), selected);
      setQuery("");
      setSelected([]);
      await refresh();
      setNotice("标注样本已保存。");
    } catch (e: unknown) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }
  /** 固定当前发布版本与样本身份，实际检索由独立worker执行。 */
  async function evaluate(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy || !activeReleaseId || !samples.length || !name.trim()) return;
    setBusy(true);
    setNotice("");
    try {
      await api.evaluate(activeReleaseId, name.trim(), topK, mode);
      setNotice("评测任务已提交，完成后显示文档级真实指标。");
      setName("");
    } catch (e: unknown) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="settings-card">
      <h3>检索评测</h3>
      <p>
        在这里标注问题和应找到的资料，再按当前发布策略运行文档级评测。条款和无答案样本在上方运行候选评测。
      </p>
      {notice && <p role="status">{notice}</p>}
      {canEdit && (
        <>
          <form
            className="management-form"
            onSubmit={(event) => void save(event)}
          >
            <label htmlFor="sample-query">标注问题</label>
            <Input
              id="sample-query"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              maxLength={4000}
              required
              placeholder="例如：设备保修期限多久？"
            />
            <label htmlFor="expected-documents">相关资料（多选）</label>
            <Select
              id="expected-documents"
              aria-label="相关资料（多选）"
              mode="multiple"
              value={selected}
              showSearch
              optionFilterProp="label"
              disabled={busy}
              options={documents
                .filter((document) => document.result_id)
                .map((document) => ({
                  value: document.document_id,
                  label: document.name,
                }))}
              onChange={(values: string[]) => setSelected(values)}
            />
            <Button
              htmlType="submit"
              loading={busy}
              disabled={busy || !query.trim() || !selected.length}
            >
              保存标注
            </Button>
          </form>
          <small>
            当前目录前100份资料可选择，更多资料可通过同一HTTP接口标注。已保存{" "}
            {samples.length} 个文档级样本。
          </small>
          <form
            className="management-form"
            onSubmit={(event) => void evaluate(event)}
          >
            <Input
              aria-label="评测名称"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="评测名称"
              maxLength={128}
              required
            />
            <label>
              Top K
              <Select
                aria-label="评测结果数"
                value={topK}
                disabled={busy}
                onChange={(value: number) => setTopK(value)}
                options={[5, 10, 20].map((value) => ({
                  value,
                  label: String(value),
                }))}
              />
            </label>
            <Select
              aria-label="评测检索方式"
              value={mode}
              disabled={busy}
              options={[
                { value: "hybrid", label: "混合检索" },
                { value: "keyword", label: "BM25" },
                { value: "vector", label: "语义向量" },
              ]}
              onChange={(value: string) => {
                if (
                  value === "hybrid" ||
                  value === "keyword" ||
                  value === "vector"
                )
                  setMode(value);
              }}
            />
            <Button
              type="primary"
              htmlType="submit"
              loading={busy}
              disabled={
                busy || !activeReleaseId || !samples.length || !name.trim()
              }
            >
              提交评测
            </Button>
          </form>
          {!activeReleaseId && (
            <p className="muted">请先发布可用构建，再运行评测。</p>
          )}
        </>
      )}
      <div className="management-table">
        <Table<Evaluation>
          rowKey="evaluation_id"
          dataSource={results}
          size="small"
          pagination={false}
          scroll={{ x: 680 }}
          locale={{ emptyText: "尚无已完成评测。" }}
          columns={[
            {
              title: "评测 / 发布",
              key: "name",
              render: (_value: unknown, result: Evaluation) => (
                <div>
                  {result.name}
                  <small title={result.release_id}>
                    {result.release_id.slice(0, 12)} ·{" "}
                    {new Date(result.created_at).toLocaleString()}
                  </small>
                </div>
              ),
            },
            { title: "样本", dataIndex: "sample_count", key: "sample_count" },
            {
              title: "Recall",
              key: "recall",
              render: (_value: unknown, result: Evaluation) =>
                result.recall.toFixed(3),
            },
            {
              title: "Precision",
              key: "precision",
              render: (_value: unknown, result: Evaluation) =>
                result.precision.toFixed(3),
            },
            {
              title: "MRR",
              key: "mrr",
              render: (_value: unknown, result: Evaluation) =>
                result.mrr.toFixed(3),
            },
          ]}
        />
      </div>
    </section>
  );
}
