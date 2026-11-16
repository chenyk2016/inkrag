import { Alert, Button, Input, Select, Checkbox } from "antd";
/** 管理员声明受控业务字段，资料编辑和索引筛选共享类型定义。 */
import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";
import {
  ApiError,
  object,
  type Client,
  type KnowledgeBase,
  type MetadataField,
} from "./api";
/** 归一化表单并定位不符合接口约束的具体行。 */
function validateFields(fields: MetadataField[]): MetadataField[] {
  const names = new Set<string>();
  return fields.map((field, index) => {
    const prefix = `第 ${index + 1} 个业务字段`;
    const label = field.label.trim();
    const name = field.name.trim();
    const options =
      field.type === "enum"
        ? field.options.map((value) => value.trim()).filter(Boolean)
        : [];
    if (!label) throw new Error(`${prefix}缺少名称。`);
    if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(name))
      throw new Error(
        `${prefix}的标识须以英文字母开头，仅使用字母、数字或下划线，最多 64 个字符。`,
      );
    if (names.has(name)) throw new Error(`${prefix}的标识与其他字段重复。`);
    names.add(name);
    if (
      field.type === "enum" &&
      (!options.length ||
        options.length > 100 ||
        options.length !== new Set(options).size ||
        options.some((value) => value.length > 128))
    )
      throw new Error(
        `${prefix}需要 1 到 100 个不重复的选项，每项最多 128 个字符。`,
      );
    return { ...field, name, label, options };
  });
}

/** 保存以知识库revision保护；不静默丢弃不符合新定义的现有业务字段值。 */
export function FieldSchema({
  api,
  parseOnly,
  onError,
  onDirtyChange,
}: {
  api: Client;
  parseOnly: boolean;
  onError: (e: unknown) => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const cache = useQueryClient();
  const [kb, setKb] = useState<KnowledgeBase | null>(null),
    [fields, setFields] = useState<MetadataField[]>([]),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [blockedFields, setBlockedFields] = useState<string[]>([]);
  const [showIdentifiers, setShowIdentifiers] = useState(false);
  useEffect(() => {
    onDirtyChange?.(
      kb !== null && JSON.stringify(fields) !== JSON.stringify(kb.fields),
    );
    return () => onDirtyChange?.(false);
  }, [fields, kb, onDirtyChange]);
  useEffect(() => {
    let active = true;
    void api
      .schema()
      .then((value) => {
        if (active) {
          setKb(value);
          setFields(value.fields);
        }
      })
      .catch((e: unknown) => {
        if (active) onError(e);
      });
    return () => {
      active = false;
    };
  }, [api, onError]);
  /** 字段的类型和值约束由HTTP和PG同时重新校验。 */
  function edit(index: number, change: Partial<MetadataField>): void {
    setNotice("");
    setError("");
    setBlockedFields([]);
    setFields((before) =>
      before.map((field, i) => (i === index ? { ...field, ...change } : field)),
    );
  }
  /** 失败保留表单；冲突后明确刷新基线以保留人工可见的取舍。 */
  async function save(draft = fields): Promise<void> {
    if (!kb) return;
    setBusy(true);
    setNotice("");
    setError("");
    setBlockedFields([]);
    try {
      const current = await api.setSchema(kb, validateFields(draft));
      setKb(current);
      setFields(current.fields);
      await cache.invalidateQueries({
        queryKey: ["knowledge-base", current.kb_id],
      });
      setNotice(
        parseOnly
          ? "业务字段已保存。可在资料列表填写。"
          : "字段定义已保存，重新构建并发布后在线筛选才会改变。",
      );
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "业务字段保存失败。");
      if (
        e instanceof ApiError &&
        e.code === "required_fields_need_backfill" &&
        e.details?.missing_required_fields
      ) {
        setBlockedFields(
          Object.keys(object(e.details.missing_required_fields)),
        );
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="settings-card">
      <h3>业务字段定义</h3>
      <p>已有资料时，新字段可先存为选填。在资料列表补齐后，再设为必填。</p>
      {fields.map((field, index) => (
        <div className="field-schema-row" key={index}>
          <Input
            aria-label="业务字段名称"
            placeholder="名称，例如部门"
            value={field.label}
            maxLength={128}
            disabled={busy}
            onChange={(e) => edit(index, { label: e.target.value })}
          />
          {showIdentifiers && (
            <Input
              aria-label="字段标识"
              placeholder="字段标识"
              value={field.name}
              maxLength={64}
              disabled={busy}
              onChange={(e) => edit(index, { name: e.target.value })}
            />
          )}
          <Select
            aria-label="字段数据类型"
            value={field.type}
            disabled={busy}
            onChange={(value: string) =>
              edit(index, { type: value, options: [] })
            }
            options={[
              { value: "text", label: "文字" },
              { value: "number", label: "数字" },
              { value: "date", label: "日期" },
              { value: "boolean", label: "布尔" },
              { value: "enum", label: "枚举" },
            ]}
          />
          {field.type === "enum" && (
            <Input
              aria-label="枚举选项"
              placeholder="财务,研发,运营"
              value={field.options.join(",")}
              disabled={busy}
              onChange={(e) =>
                edit(index, {
                  options: e.target.value.split(/[,，]/),
                })
              }
            />
          )}
          <Checkbox
            checked={field.required}
            disabled={busy}
            onChange={(event) =>
              edit(index, { required: event.target.checked })
            }
          >
            必填
          </Checkbox>
          <Button
            type="text"
            aria-label="移除字段定义"
            disabled={busy}
            onClick={() =>
              setFields((before) => before.filter((_, i) => i !== index))
            }
          >
            <X size={15} />
          </Button>
        </div>
      ))}
      <div className="management-form">
        <Button
          disabled={!kb || busy || fields.length >= 64}
          onClick={() =>
            setFields((before) => [
              ...before,
              {
                name: `field_${crypto.randomUUID().replaceAll("-", "")}`,
                label: "",
                type: "text",
                required: false,
                options: [],
              },
            ])
          }
        >
          <Plus size={14} />
          添加业务字段
        </Button>
        <Button
          type="primary"
          loading={busy}
          disabled={!kb || busy}
          onClick={() => void save()}
        >
          保存业务字段
        </Button>
        {!!fields.length && (
          <Button
            type="link"
            onClick={() => setShowIdentifiers(!showIdentifiers)}
          >
            {showIdentifiers ? "隐藏字段标识" : "显示字段标识"}
          </Button>
        )}
        <Button
          type="link"
          disabled={busy}
          onClick={() => {
            void api
              .schema()
              .then((value) => {
                setKb(value);
                setNotice("已刷新并保留尚未保存的字段表单，请再次检查再保存。");
              })
              .catch(onError);
          }}
        >
          刷新字段版本
        </Button>
      </div>
      {error && (
        <Alert
          type="error"
          showIcon
          title={error}
          action={
            blockedFields.length > 0 && (
              <Button
                size="small"
                disabled={busy}
                onClick={() =>
                  void save(
                    fields.map((field) =>
                      blockedFields.includes(field.name)
                        ? { ...field, required: false }
                        : field,
                    ),
                  )
                }
              >
                先保存为选填
              </Button>
            )
          }
        />
      )}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
