/** 导入向导保留批次、属性和暂存文件，只有最终确认才创建正式资料。 */
import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Select, Steps } from "antd";
import { ArrowLeft, CloudUpload, Trash2 } from "lucide-react";
import "./upload-flow.css";
import {
  UploadParameters,
  supportsLayout,
  type FileParsing,
} from "./upload-parameters";
import type {
  Client,
  Job,
  JsonObject,
  MetadataField,
  ParseEngine,
  ProcessingOptions,
  ProcessingCapabilities,
} from "./api";
import {
  engineSupports,
  engineSupportsOcr,
  fileParseRules,
  isImageFile,
  uploadAccept,
} from "./parser-engines";

type Values = Record<string, string>;
type Preview = Awaited<ReturnType<Client["importPreview"]>> & { id: string };

/** 业务字段按已有字段类型提交，单份值覆盖批次值。 */
function metadataFor(fields: MetadataField[], values: Values): JsonObject {
  const result: JsonObject = {};
  for (const field of fields) {
    const value = values[field.name];
    if (value === undefined || !value.trim()) {
      if (field.required)
        throw new Error(`请填写必填业务字段「${field.label}」。`);
      continue;
    }
    if (field.type === "number") {
      const numeric = Number(value);
      if (!Number.isFinite(numeric))
        throw new Error(`${field.label}需要有限数字`);
      result[field.name] = numeric;
    } else {
      result[field.name] = field.type === "boolean" ? value === "true" : value;
    }
  }
  return result;
}

/** 复用原有字段语义，区分继承批次值和明确清空选填值。 */
function AttributeField({
  field,
  value,
  change,
  disabled,
}: {
  field: MetadataField;
  value: string;
  change: (value: string) => void;
  disabled: boolean;
}) {
  const id = `import-attribute-${field.name}`;
  return (
    <div className="upload-field">
      <label htmlFor={id}>
        {field.label}
        {field.required ? " *" : ""}
      </label>
      {field.type === "enum" || field.type === "boolean" ? (
        <Select
          id={id}
          disabled={disabled}
          value={value || undefined}
          placeholder="请选择"
          allowClear
          options={
            field.type === "boolean"
              ? [
                  { value: "true", label: "是" },
                  { value: "false", label: "否" },
                ]
              : field.options.map((option) => ({
                  value: option,
                  label: option,
                }))
          }
          onChange={(next: string | undefined) => change(next ?? "")}
        />
      ) : (
        <Input
          id={id}
          disabled={disabled}
          type={
            field.type === "date"
              ? "date"
              : field.type === "number"
                ? "number"
                : "text"
          }
          step="any"
          maxLength={4000}
          value={value}
          onChange={(event) => change(event.target.value)}
        />
      )}
    </div>
  );
}

/** 默认对独立图片整页识字，PDF仍优先利用原有文字层。 */
function defaultParsing(file: File, engines: ParseEngine[]): FileParsing {
  const engine = engines.find((tool) => engineSupports(tool, file));
  return {
    engine: engine?.id ?? "",
    ocr_enabled: engine?.ocr_default ?? false,
    ocr_mode: isImageFile(file) ? "full_page" : "auto",
    table_structure: true,
    table_mode: "accurate",
  };
}

/** 同批设置共享规则，可逐份覆盖属性并检查冻结预览。 */
export function UploadDialog({
  files: initialFiles,
  fields,
  engines,
  processingCapabilities,
  api,
  maxBytes,
  beforeConfirm,
  onCancel,
  onConfirm,
}: {
  files: File[];
  fields: MetadataField[];
  engines: ParseEngine[];
  processingCapabilities: ProcessingCapabilities | null;
  api: Client;
  maxBytes: number;
  beforeConfirm: () => Promise<boolean>;
  onCancel: () => void;
  onConfirm: (jobs: Job[]) => void;
}) {
  const [files, setFiles] = useState(initialFiles);
  const input = useRef<HTMLInputElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const [parsing, setParsing] = useState<FileParsing[]>(() =>
    initialFiles.map((file) => defaultParsing(file, engines)),
  );
  const [processing, setProcessing] = useState<ProcessingOptions | null>(
    processingCapabilities?.defaults ?? null,
  );
  const [custom, setCustom] = useState(false);
  /** 解析参数按单文件冻结，批次变更只覆盖当前适用文件。 */
  function changeParsing(indices: number[], patch: Partial<FileParsing>) {
    setParsing((prior) =>
      prior.map((item, index) =>
        indices.includes(index) ? { ...item, ...patch } : item,
      ),
    );
  }
  /** 与预览及确认汇总使用同一份逐文件工具选择。 */
  function engineFor(index: number) {
    return engines.find((item) => item.id === parsing[index]?.engine);
  }
  const [step, setStep] = useState(0);
  useEffect(() => {
    body.current?.scrollTo({ top: 0 });
  }, [step]);
  const [values, setValues] = useState<Values>({});
  const [overrides, setOverrides] = useState<Record<number, Values>>({});
  const [editing, setEditing] = useState(-1);
  const [active, setActive] = useState(0);
  const [excludeHeaders, setExcludeHeaders] = useState(false);
  const [excludeImages, setExcludeImages] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [ocrRetry, setOcrRetry] = useState<number | null>(null);
  const previewWait = useRef<AbortController | null>(null);
  const [confirmPending, setConfirmPending] = useState(false);
  const [previews, setPreviews] = useState<Preview[]>([]);
  const staged = useRef<string[]>([]);
  const queued = useRef<{ key: string; operation: string; id?: string }[]>([]);
  const mounted = useRef(false);
  const locked = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      previewWait.current?.abort();
    };
  }, []);

  /** 串行暂存限制内存和并发，失败后只补传未完成的文件。 */
  async function uploadFiles() {
    if (
      files.length > 20 ||
      files.some((file) => file.size === 0 || file.size > maxBytes)
    ) {
      throw new Error("每批最多 20 份资料，文件须非空且符合单文件上传上限。");
    }
    for (const [index, file] of files.entries()) {
      if (staged.current[index]) continue;
      setBusy(`正在上传 ${index + 1}/${files.length}`);
      staged.current[index] = await api.stageImport(file);
      if (!mounted.current) return;
    }
    setStep(1);
  }

  /** 固定每份属性，错误精确指出需修改的资料。 */
  function attributes() {
    return files.map((file, index) => {
      try {
        return metadataFor(fields, { ...values, ...overrides[index] });
      } catch (cause) {
        throw new Error(
          `${file.name}：${cause instanceof Error ? cause.message : "业务字段无效"}`,
        );
      }
    });
  }

  /** 提交后台预览，轮询真实状态。同规则重试时继续等待已有任务。 */
  async function previewFiles(signal: AbortSignal, fileOptions: FileParsing[]) {
    attributes();
    setPreviews([]);
    setActive(0);
    // 先校验全批，避免后面的文件无可用工具时前面的预览已经启动。
    if (
      processingCapabilities?.available &&
      processing &&
      (processing.chunk_overlap >= processing.chunk_size ||
        processing.chunk_size < 32 ||
        processing.chunk_size > (processingCapabilities.max_tokens ?? 0))
    )
      throw new Error(
        "请检查分块参数：块长度不能超过模型上限，重叠必须小于块长度。",
      );
    const allRules = files.map((file, index) => ({
      ...fileParseRules(
        file,
        engineFor(index),
        fileOptions[index].ocr_enabled,
        {
          exclude_headers: excludeHeaders,
          exclude_images: excludeImages,
        },
      ),
      ...(supportsLayout(engineFor(index), file)
        ? {
            ...(fileOptions[index].ocr_enabled
              ? { ocr_mode: fileOptions[index].ocr_mode }
              : {}),
            table_structure: fileOptions[index].table_structure,
            ...(fileOptions[index].table_structure
              ? { table_mode: fileOptions[index].table_mode }
              : {}),
          }
        : {}),
      ...(processingCapabilities?.available && processing
        ? { processing }
        : {}),
    }));
    for (const [index, stage] of staged.current.entries()) {
      signal.throwIfAborted();
      setBusy(`正在提交预览 ${index + 1}/${files.length}`);
      const rules = allRules[index];
      const key = JSON.stringify(rules);
      if (queued.current[index]?.key !== key) {
        queued.current[index] = { key, operation: crypto.randomUUID() };
      }
      const entry = queued.current[index];
      entry.id ??= await api.previewImport(
        stage,
        rules,
        entry.operation,
        signal,
      );
      if (!mounted.current) return;
    }
    const pending = new Set(files.map((_, index) => index));
    const results: Preview[] = [];
    const deadline = Date.now() + 10 * 60 * 1000;
    while (pending.size && mounted.current) {
      signal.throwIfAborted();
      setBusy(
        `后台正在解析，已完成 ${files.length - pending.size}/${files.length} 份`,
      );
      for (const index of pending) {
        const { id } = queued.current[index];
        if (!id) throw new Error("预览任务尚未接受，请重试。");
        const result = await api.importPreview(id, signal);
        if (["failed", "cancelled"].includes(result.status)) {
          delete queued.current[index];
          const options = fileOptions[index];
          const canRetryOcr =
            result.errorCode === "empty_document" &&
            supportsLayout(engineFor(index), files[index]) &&
            (!options.ocr_enabled || options.ocr_mode !== "full_page");
          setOcrRetry(canRetryOcr ? index : null);
          setError(
            `${files[index].name}：${result.error || "预览失败"}。${canRetryOcr ? "可改用整页 OCR 重试。" : (result.suggestion ?? "")}`,
          );
          return;
        }
        if (result.status === "succeeded") {
          results[index] = { ...result, id };
          pending.delete(index);
        }
      }
      if (!pending.size || !mounted.current) break;
      if (Date.now() >= deadline)
        throw new Error("后台仍在处理，请再次查看预览进度。");
      await new Promise<void>((resolve) => setTimeout(resolve, 1500));
    }
    if (mounted.current) {
      setPreviews(results);
      setStep(2);
    }
  }

  /** 确认引用当前预览。结果不明时可用原批次重试。 */
  async function confirmFiles() {
    setConfirmPending(true);
    if (!(await beforeConfirm())) {
      setConfirmPending(false);
      return;
    }
    const metadata = attributes();
    const jobs = await api.confirmImport(
      previews.map((item, index) => ({
        preview_id: item.id,
        metadata: metadata[index],
      })),
    );
    onConfirm(jobs);
  }

  /** 串行推进当前步骤，保留可恢复的输入。 */
  async function advance(retryIndex?: number) {
    if (locked.current) return;
    locked.current = true;
    setError("");
    setOcrRetry(null);
    setBusy("正在处理");
    try {
      if (step === 0) await uploadFiles();
      else if (step === 1) {
        const controller = new AbortController();
        previewWait.current = controller;
        const options = parsing.map(
          (item, index): FileParsing =>
            retryIndex === index
              ? { ...item, ocr_enabled: true, ocr_mode: "full_page" }
              : item,
        );
        if (retryIndex !== undefined) setParsing(options);
        await previewFiles(controller.signal, options);
      } else if (step === 2) setStep(3);
      else await confirmFiles();
    } catch (cause) {
      if (mounted.current)
        setError(
          previewWait.current?.signal.aborted
            ? "已停止等待。再点下一步可继续查看已提交的预览。"
            : cause instanceof Error
              ? cause.message
              : "操作未完成",
        );
    } finally {
      previewWait.current = null;
      locked.current = false;
      if (mounted.current) {
        setBusy("");
        setConfirmPending(false);
      }
    }
  }

  const options = files.map((file, index) => ({
    value: index,
    label: file.name,
  }));
  const preview = previews[active];
  /** 修改文件集合时清除旧暂存和预览身份，避免将旧文件结果误用于新批次。 */
  function chooseFiles(next: File[]): void {
    if (
      next.length > 20 ||
      next.some((file) => !file.size || file.size > maxBytes)
    ) {
      setError("每批最多 20 份资料，文件须非空且符合单文件上传上限。");
      return;
    }
    if (
      next.some((file) => !engines.some((item) => engineSupports(item, file)))
    ) {
      setError("所选文件包含当前服务不支持的格式，请按页面列出的格式选择。");
      return;
    }
    setFiles(next);
    staged.current = [];
    queued.current = [];
    setPreviews([]);
    setActive(0);
    setOverrides({});
    setEditing(-1);
    setParsing(next.map((file) => defaultParsing(file, engines)));
    setOcrRetry(null);
    setError("");
  }
  return (
    <section className="upload-flow" aria-label="新增资料四步流程">
      <Button
        className="upload-flow-exit"
        type="text"
        icon={<ArrowLeft size={18} />}
        disabled={confirmPending}
        onClick={onCancel}
      >
        退出
      </Button>
      <div className="upload-flow-steps">
        <Steps
          current={step}
          items={[
            { title: "选择文件" },
            { title: "参数设置" },
            { title: "数据预览" },
            { title: "确认上传" },
          ]}
        />
      </div>
      <div
        className="upload-flow-body"
        ref={body}
        tabIndex={0}
        role="region"
        aria-label="当前上传步骤"
      >
        {step === 0 && (
          <>
            <input
              ref={input}
              type="file"
              className="sr-only"
              multiple
              accept={uploadAccept(engines)}
              aria-label="选择要上传的文件"
              onChange={(event) => {
                chooseFiles(Array.from(event.target.files ?? []));
                event.target.value = "";
              }}
            />
            <button
              type="button"
              className="upload-flow-dropzone"
              disabled={Boolean(busy)}
              onClick={() => input.current?.click()}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                if (!busy) chooseFiles(Array.from(event.dataTransfer.files));
              }}
            >
              <CloudUpload size={36} aria-hidden="true" />
              <strong>点击或拖动文件到此处上传</strong>
              <span>
                支持{" "}
                {uploadAccept(engines).split(",").join("、") || "暂无可用格式"}
              </span>
              <span>
                每批最多 20 个文件，单个文件不超过{" "}
                {Math.floor(maxBytes / 1024 / 1024)} MB
              </span>
            </button>
            {files.length > 0 && (
              <ul className="upload-flow-files">
                {files.map((file, index) => (
                  <li key={index}>
                    <span>{file.name}</span>
                    <small>
                      {Math.max(1, Math.round(file.size / 1024))} KB
                    </small>
                    <Button
                      type="text"
                      disabled={Boolean(busy)}
                      aria-label={`移除 ${file.name}`}
                      icon={<Trash2 size={16} />}
                      onClick={() =>
                        chooseFiles(
                          files.filter((_, position) => position !== index),
                        )
                      }
                    />
                  </li>
                ))}
              </ul>
            )}
            <p className="upload-flow-help">
              选择文件后点击下一步。原件会暂存以生成预览，最后确认前不会加入资料列表。
            </p>
          </>
        )}
        {step === 1 && (
          <>
            <UploadParameters
              files={files}
              engines={engines}
              parsing={parsing}
              onParsing={changeParsing}
              processing={processing}
              onProcessing={setProcessing}
              capabilities={processingCapabilities}
              custom={custom}
              onCustom={(value) => {
                setCustom(value);
                if (!value && processing && processingCapabilities?.defaults)
                  setProcessing({
                    ...processing,
                    chunk_size: processingCapabilities.defaults.chunk_size,
                    chunk_overlap:
                      processingCapabilities.defaults.chunk_overlap,
                  });
              }}
              excludeHeaders={excludeHeaders}
              excludeImages={excludeImages}
              onCleaning={(headers, images) => {
                setExcludeHeaders(headers);
                setExcludeImages(images);
              }}
              disabled={Boolean(busy)}
            />
            {fields.length > 0 && (
              <>
                <h3>业务字段</h3>
                <p className="upload-flow-help">
                  业务字段用于资料分类和检索筛选。默认不填写，标 *
                  的字段必须填写；单份修改覆盖对应批量字段，其余继续继承批量值。字段名称、类型和可选值由本库「业务字段」定义。
                </p>
                <Select
                  aria-label="业务字段应用范围"
                  disabled={Boolean(busy)}
                  value={editing}
                  options={[{ value: -1, label: "整批资料" }, ...options]}
                  onChange={(value: number) => setEditing(value)}
                />
                {editing >= 0 && (
                  <p>只修改这一份的业务字段，其余字段继续使用批量值。</p>
                )}
                {fields.map((field) => (
                  <AttributeField
                    key={field.name}
                    field={field}
                    disabled={Boolean(busy)}
                    value={
                      (editing >= 0
                        ? overrides[editing]?.[field.name]
                        : undefined) ??
                      values[field.name] ??
                      ""
                    }
                    change={(value) => {
                      if (editing < 0)
                        setValues((prior) => ({
                          ...prior,
                          [field.name]: value,
                        }));
                      else
                        setOverrides((prior) => ({
                          ...prior,
                          [editing]: { ...prior[editing], [field.name]: value },
                        }));
                    }}
                  />
                ))}
                {editing >= 0 && overrides[editing] && (
                  <Button
                    disabled={Boolean(busy)}
                    onClick={() =>
                      setOverrides((prior) => {
                        const next = { ...prior };
                        delete next[editing];
                        return next;
                      })
                    }
                  >
                    恢复这份资料的批量业务字段
                  </Button>
                )}
              </>
            )}
          </>
        )}
        {step === 2 && preview && (
          <>
            <p>
              本次预览：{engineFor(active)?.name ?? "未知工具"} ·{" "}
              {engineSupportsOcr(engineFor(active), files[active])
                ? `OCR ${parsing[active].ocr_enabled ? "开启" : "关闭"}`
                : "无需 OCR"}
              。确认导入沿用这次预览，不会重新改变识别配置。
            </p>
            <Select
              aria-label="预览文件"
              options={options}
              value={active}
              onChange={(value: number) => setActive(value)}
            />
            {preview.warnings.map((warning, index) => (
              <Alert
                key={index}
                type="warning"
                showIcon
                title={
                  typeof warning.message === "string"
                    ? warning.message
                    : "解析提示"
                }
              />
            ))}
            {preview.effective_processing && (
              <>
                <div className="chunk-preview-stats">
                  <div>
                    <strong>{preview.chunk_count}</strong>
                    <span>个实际分块</span>
                  </div>
                  <div>
                    <strong>{preview.effective_processing.chunk_size}</strong>
                    <span>块长度上限 · token</span>
                  </div>
                  <div>
                    <strong>
                      {preview.effective_processing.chunk_overlap}
                    </strong>
                    <span>重叠 · token</span>
                  </div>
                  <div>
                    <strong>{preview.max_tokens}</strong>
                    <span>模型上限 · token</span>
                  </div>
                </div>
                <p className="parameter-note">
                  策略：
                  {
                    {
                      auto: "自动",
                      sentence: "按句",
                      markdown: "Markdown 层级",
                    }[preview.effective_processing.chunking]
                  }{" "}
                  · 标题入索引：
                  {preview.effective_processing.include_title ? "开启" : "关闭"}
                  。以下来自本次真实分块，确认上传将冻结这组设置。
                </p>
                <div className="chunk-preview-list" aria-label="实际分块预览">
                  {preview.chunks.map((chunk, index) => (
                    <article key={index}>
                      <header>
                        <strong>分块 {index + 1}</strong>
                        <span>{chunk.token_count} token</span>
                      </header>
                      <small>
                        来源：{files[active].name}
                        {chunk.page ? ` · 第 ${chunk.page} 页` : ""}
                        {chunk.element_ref ? ` · ${chunk.element_ref}` : ""}
                      </small>
                      <pre>{chunk.text}</pre>
                    </article>
                  ))}
                </div>
                {preview.chunks_truncated && (
                  <p>
                    共 {preview.chunk_count} 个分块，页面展示前{" "}
                    {preview.chunks.length} 个；确认使用全部分块。
                  </p>
                )}
              </>
            )}
            <details open={!preview.effective_processing}>
              <summary>查看清洗正文</summary>
              <pre aria-label="清洗正文预览" className="upload-source-text">
                {preview.text || "本规则下没有可展示的正文。"}
              </pre>
            </details>
            <details>
              <summary>查看清洗前的解析正文</summary>
              <pre className="upload-source-text">{preview.before}</pre>
            </details>
            {preview.truncated && (
              <p>仅展示前 16,000 字符。确认后使用完整清洗结果。</p>
            )}
          </>
        )}
        {step === 3 && (
          <div className="upload-flow-confirm">
            <h2>确认上传 {files.length} 份资料</h2>
            <p>
              将沿用刚才检查的完整预览结果和参数，不会重新切换解析工具。确认后按本库自动生效设置处理，可在资料列表查看状态。
            </p>
            <ul className="upload-flow-summary">
              {files.map((file, index) => (
                <li key={index}>
                  <strong>{file.name}</strong>
                  <span>
                    {engineFor(index)?.name} ·{" "}
                    {engineSupportsOcr(engineFor(index), file)
                      ? `OCR ${parsing[index].ocr_enabled ? "开启" : "关闭"}`
                      : "无需 OCR"}
                  </span>
                  {supportsLayout(engineFor(index), file) && (
                    <span>
                      OCR 范围：
                      {parsing[index].ocr_enabled
                        ? parsing[index].ocr_mode === "full_page"
                          ? "整页"
                          : "自动"
                        : "不启用"}{" "}
                      · 表格结构：
                      {parsing[index].table_structure
                        ? parsing[index].table_mode === "accurate"
                          ? "精确"
                          : "快速"
                        : "关闭"}
                    </span>
                  )}
                  <span>
                    页眉页脚：{excludeHeaders ? "排除" : "保留"} · 图片：
                    {excludeImages ? "排除" : "保留"}
                  </span>
                  {previews[index]?.effective_processing && (
                    <span>
                      分块：{previews[index].chunk_count} 个 ·{" "}
                      {
                        {
                          auto: "自动",
                          sentence: "按句",
                          markdown: "Markdown 层级",
                        }[
                          previews[index].effective_processing?.chunking ??
                            "auto"
                        ]
                      }{" "}
                      · 长度 {previews[index].effective_processing?.chunk_size}{" "}
                      / 重叠{" "}
                      {previews[index].effective_processing?.chunk_overlap}{" "}
                      token · 标题入索引{" "}
                      {previews[index].effective_processing?.include_title
                        ? "开启"
                        : "关闭"}
                    </span>
                  )}
                  <span>
                    预览已生成
                    {previews[index]?.truncated
                      ? "（页面预览截断，导入使用完整结果）"
                      : ""}
                  </span>
                  {fields.length > 0 && (
                    <dl>
                      {fields.map((field) => (
                        <div key={field.name}>
                          <dt>{field.label}</dt>
                          <dd>
                            {{ ...values, ...overrides[index] }[field.name] ||
                              "未填写"}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
      {(busy || error) && (
        <div className="upload-flow-feedback">
          {busy && <p role="status">{busy}</p>}
          {error && <Alert type="error" title={error} showIcon role="alert" />}
        </div>
      )}
      <div className="upload-flow-actions">
        {step === 1 && busy && (
          <Button onClick={() => previewWait.current?.abort()}>停止等待</Button>
        )}
        {step === 1 && ocrRetry !== null && !busy && (
          <Button onClick={() => void advance(ocrRetry)}>整页 OCR 重试</Button>
        )}
        {step > 0 && (
          <Button
            disabled={Boolean(busy)}
            onClick={() => {
              setStep(step - 1);
              setError("");
              setOcrRetry(null);
            }}
          >
            上一步
          </Button>
        )}
        <Button
          type="primary"
          size="large"
          loading={Boolean(busy)}
          disabled={step === 0 && !files.length}
          onClick={() => void advance()}
        >
          {step === 3 ? "确认上传" : "下一步"}
        </Button>
      </div>
    </section>
  );
}
