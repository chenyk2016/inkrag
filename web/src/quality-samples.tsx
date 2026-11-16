/** 质量金标绑定真实保存版本和元素，文档级与条款级指标明确区分。 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Checkbox, Input, Select } from "antd";
import { type DocumentDetail, type JsonObject } from "./api";
import { Failure, Pagination } from "./application";
import type { PlatformClient } from "./platform-api";
/** 用户选择实际来源与块引用；无答案样本单列，保存后历史标注不覆盖。 */
export function QualitySamples({
  api,
  canEdit,
}: {
  api: PlatformClient;
  canEdit: boolean;
}) {
  const [question, setQuestion] = useState(""),
    [noAnswer, setNoAnswer] = useState(false),
    [documentId, setDocumentId] = useState(""),
    [ref, setRef] = useState(""),
    [search, setSearch] = useState(""),
    [offset, setOffset] = useState(0),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState("");
  const docs = useQuery({
    queryKey: ["quality-docs", api.scopeId, search, offset],
    queryFn: ({ signal }) => api.documentPage(search, offset, signal),
  });
  const selected = useQuery<DocumentDetail>({
    queryKey: ["quality-source", documentId],
    queryFn: ({ signal }) => api.document(documentId, undefined, signal),
    enabled: Boolean(documentId) && !noAnswer,
  });
  const samples = useQuery({
    queryKey: ["quality-samples", api.scopeId],
    queryFn: () => api.qualitySamples(),
  });
  return (
    <section className="surface">
      <h3>维护质量样本</h3>
      <p>
        选择预期文档或精确内容块；“不应有结果”单独检验检索误召回，不等同回答质量。
      </p>
      {canEdit && (
        <form
          className="stack-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (busy || !question.trim() || (!noAnswer && !selected.data))
              return;
            setBusy(true);
            setError(null);
            const expected: JsonObject[] = [];
            if (!noAnswer && selected.data) {
              expected.push({
                document_id: documentId,
                result_id: selected.data.result_id,
                ...(ref ? { element_ref: ref } : {}),
              });
            }
            void api
              .saveQualitySample({
                query: question,
                expected_sources: expected,
                no_answer: noAnswer,
              })
              .then(() => {
                setQuestion("");
                setNotice("标注已保存，绑定当前不可变来源版本。");
                void samples.refetch();
              })
              .catch(setError)
              .finally(() => setBusy(false));
          }}
        >
          <label>
            问题
            <Input.TextArea
              required
              disabled={busy}
              maxLength={4000}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="用户真实会问的问题"
            />
          </label>
          <Checkbox
            className="checkbox-label"
            checked={noAnswer}
            disabled={busy}
            onChange={(event) => setNoAnswer(event.target.checked)}
          >
            不应有结果（无答案样本）
          </Checkbox>
          {!noAnswer && (
            <>
              <label>
                查找预期资料
                <Input
                  value={search}
                  disabled={busy}
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setOffset(0);
                  }}
                />
              </label>
              <label>
                预期资料
                <Select
                  aria-label="预期资料"
                  value={documentId}
                  disabled={busy}
                  loading={docs.isFetching}
                  onChange={(value) => {
                    setDocumentId(value);
                    setRef("");
                  }}
                  options={[
                    { value: "", label: "选择已解析资料" },
                    ...(docs.data?.items
                      .filter((row) => row.result_id)
                      .map((row) => ({
                        value: row.document_id,
                        label: row.name,
                      })) ?? []),
                  ]}
                />
              </label>
              {docs.data && docs.data.total > 100 && (
                <Pagination
                  page={docs.data}
                  offset={offset}
                  onOffset={setOffset}
                />
              )}
              <label>
                预期内容块
                <Select
                  aria-label="预期内容块"
                  value={ref}
                  disabled={busy || !selected.data}
                  loading={selected.isFetching}
                  onChange={setRef}
                  options={[
                    { value: "", label: "仅标注文档级相关" },
                    ...(selected.data?.result.order.map((item) => {
                      const text = selected.data.result.texts.find(
                        (text) => text.self_ref === item.self_ref,
                      );
                      return {
                        value: item.self_ref,
                        label: `${item.self_ref} · ${text?.text.slice(0, 70) ?? item.label}`,
                      };
                    }) ?? []),
                  ]}
                />
              </label>
              {selected.data && (
                <small>
                  绑定解析结果 {selected.data.result_id.slice(0, 12)} · v
                  {selected.data.result.revision}
                </small>
              )}
            </>
          )}
          {docs.isError && <Failure error={docs.error} />}
          {!noAnswer && selected.isError && <Failure error={selected.error} />}
          {error !== null && <Failure error={error} />}
          <Button
            type="primary"
            htmlType="submit"
            loading={busy}
            disabled={!question.trim() || (!noAnswer && !selected.data)}
          >
            保存质量标注
          </Button>
        </form>
      )}
      {notice && <p role="status">{notice}</p>}
      {samples.isError && <Failure error={samples.error} />}
      <details>
        <summary>已保存标注（{samples.data?.length ?? 0}）</summary>
        {samples.data?.map((sample) => (
          <article className="trial-hit" key={String(sample.sample_id)}>
            <strong>{String(sample.query)}</strong>
            <p>
              {sample.no_answer
                ? "无答案样本"
                : "预期来源 " + JSON.stringify(sample.expected_sources)}
            </p>
            <small>标注版本 {String(sample.annotation_revision)}</small>
          </article>
        ))}
      </details>
    </section>
  );
}
