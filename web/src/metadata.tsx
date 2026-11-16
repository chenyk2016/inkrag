/** 按知识库定义编辑资料的业务字段，列表和详情共用同一份表单。 */
import { Alert, Button, Input, Select, Spin } from "antd";
import { useEffect, useRef, useState } from "react";
import type { Client, DocumentSummary, JsonObject, MetadataField } from "./api";

type Values = Map<string, string>;

/** 保留现有标量的表单值，未填写与布尔否值分别处理。 */
function fieldValues(metadata: JsonObject): Values {
  return new Map(
    Object.entries(metadata).map(([name, value]) => [name, String(value)]),
  );
}

/** 按定义生成严格的请求值，留空的可选字段从资料中清除。 */
function metadataFor(fields: MetadataField[], values: Values): JsonObject {
  const metadata: JsonObject = {};
  for (const field of fields) {
    const value = values.get(field.name);
    if (value === undefined || (field.required && !value.trim())) {
      if (field.required) throw new Error(`请填写「${field.label}」。`);
      continue;
    }
    if (field.type === "number") {
      const number = Number(value);
      if (!value.trim() || !Number.isFinite(number) || Math.abs(number) > 1e100)
        throw new Error(`「${field.label}」需要有效数字。`);
      metadata[field.name] = number;
    } else {
      metadata[field.name] =
        field.type === "boolean" ? value === "true" : value;
    }
  }
  return metadata;
}

/** 以当前资料版本保存业务字段，失败时保留输入供用户更正。 */
export function MetadataEditor({
  document,
  api,
  parseOnly,
  canEdit,
  onDirtyChange,
  onSaved,
  onSavingChange,
  showHeading = false,
}: {
  document: DocumentSummary;
  api: Client;
  parseOnly: boolean;
  canEdit: boolean;
  onDirtyChange: (dirty: boolean) => void;
  onSaved: (document: DocumentSummary) => void;
  onSavingChange?: (saving: boolean) => void;
  showHeading?: boolean;
}) {
  const [current, setCurrent] = useState(document);
  const [values, setValues] = useState(() => fieldValues(document.metadata));
  const [definitions, setDefinitions] = useState<MetadataField[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const edited = useRef(false);

  useEffect(() => {
    if (edited.current && current.document_id === document.document_id) return;
    edited.current = false;
    onDirtyChange(false);
    setCurrent(document);
    setValues(fieldValues(document.metadata));
  }, [document, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);
  useEffect(() => {
    let alive = true;
    void api
      .schema()
      .then((kb) => {
        if (alive) setDefinitions(kb.fields);
      })
      .catch((reason: unknown) => {
        if (alive)
          setError(
            reason instanceof Error ? reason.message : "无法读取业务字段。",
          );
      });
    return () => {
      alive = false;
    };
  }, [api]);

  /** 表单草稿只在服务确认保存后回写目录或详情。 */
  function edit(name: string, value?: string): void {
    edited.current = true;
    onDirtyChange(true);
    setError("");
    setNotice("");
    setValues((before) => {
      const next = new Map(before);
      if (value === undefined || value === "") next.delete(name);
      else next.set(name, value);
      return next;
    });
  }

  /** 保存当前草稿并回传服务确认的资料状态。 */
  async function save(): Promise<void> {
    if (!definitions || !edited.current || busy) return;
    setBusy(true);
    onSavingChange?.(true);
    setError("");
    try {
      const saved = await api.metadata(
        current,
        metadataFor(definitions, values),
      );
      setCurrent(saved);
      setValues(fieldValues(saved.metadata));
      edited.current = false;
      onDirtyChange(false);
      setNotice(
        parseOnly
          ? "业务字段已保存。"
          : "业务字段已保存。生效后可用于在线筛选。",
      );
      onSaved(saved);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "业务字段保存失败。");
    } finally {
      setBusy(false);
      onSavingChange?.(false);
    }
  }

  return (
    <section
      className="inspector-section metadata-editor"
      aria-label="业务字段"
    >
      {showHeading && <h3>业务字段</h3>}
      {!definitions && !error && (
        <Spin size="small" aria-label="正在读取业务字段" />
      )}
      {definitions?.length === 0 && (
        <p>本库还没有业务字段。可在「业务字段」页添加后再回来填写。</p>
      )}
      {definitions?.map((field) => {
        const inputId = `metadata-${current.document_id}-${field.name}`;
        const options =
          field.type === "boolean"
            ? [
                { value: "true", label: "是" },
                { value: "false", label: "否" },
              ]
            : field.options.map((value) => ({ value, label: value }));
        return (
          <div className="business-field-value" key={field.name}>
            <label htmlFor={inputId}>
              {field.label}
              {field.required ? " *" : ""}
            </label>
            {field.type === "boolean" || field.type === "enum" ? (
              <Select
                id={inputId}
                aria-label={field.label}
                placeholder="未填写"
                disabled={!canEdit || busy}
                allowClear={!field.required}
                value={values.get(field.name) || undefined}
                options={options}
                onChange={(value?: string) => edit(field.name, value)}
              />
            ) : (
              <Input
                id={inputId}
                aria-label={field.label}
                type={field.type === "date" ? "date" : "text"}
                inputMode={field.type === "number" ? "decimal" : undefined}
                value={values.get(field.name) ?? ""}
                maxLength={512}
                disabled={!canEdit || busy}
                onChange={(event) => edit(field.name, event.target.value)}
              />
            )}
          </div>
        );
      })}
      {error && <Alert type="error" showIcon title={error} />}
      {notice && <p role="status">{notice}</p>}
      {canEdit && !!definitions?.length && (
        <Button
          type="primary"
          loading={busy}
          disabled={!edited.current}
          onClick={() => void save()}
        >
          保存业务字段
        </Button>
      )}
    </section>
  );
}
