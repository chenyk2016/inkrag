/** 原文、结构编辑与版本发布分开，审校始终在有界的双视口中进行。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DocumentEnablement } from "./document-enablement";
import { Button, Dropdown, Input, Modal, Select, Tag } from "antd";
import { Group, Panel, Separator, useGroupRef } from "react-resizable-panels";
import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
  Download,
  FileText,
  History,
  List,
  LocateFixed,
  PanelRight,
  Plus,
  Save,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react";
import type {
  Client,
  DocumentDetail,
  DocumentSummary,
  PictureSuggestion,
  Version,
} from "./api";
import { selectionCurrent } from "./document-selection";
import { MetadataEditor } from "./metadata";
import { AssetImage, OriginalPreview } from "./assets";
import { ReviewScroll, revealInViewport } from "./review-scroll";
import { ReviewTable } from "./review-structure";
import {
  readingOrder,
  sourceLocation,
  type ReviewOrderItem,
  type ReviewResult,
  type SourceLocation,
  type StructureOperation,
} from "./review-types";
import "./review.css";

/** 解析工具在详情里用友好名展示，未知 id 原样回显避免静默隐藏。 */
const ENGINE_LABELS: Record<string, string> = {
  builtin: "内置解析",
  docling: "Docling 解析",
};

interface ReviewProps {
  document: DocumentDetail;
  onEnabledChange?: (enabled: boolean) => void;
  enabledChanging?: boolean;
  focusedElementRef?: string;
  versions: Version[];
  api: Client;
  changes: Record<string, string>;
  structuralChanges?: readonly StructureOperation[];
  latestResultId: string;
  busy: string | null;
  canEdit: boolean;
  visionAvailable: boolean;
  visionProvider: string | null;
  canIngest: boolean;
  activating: boolean;
  parseOnly: boolean;
  onDelete: () => void;
  onReparse?: () => void;
  onReplaceSource?: (file: File) => void;
  onMetadataDirtyChange: (dirty: boolean) => void;
  onMetadataSaved: (document: DocumentSummary) => void;
  onChange: (ref: string, value: string) => void;
  onStructureChange?: (operation: StructureOperation) => void;
  onVersion: (id: string) => void;
  onSave: () => void;
  focusedReview: boolean;
  onToggleFocus: () => void;
  onExport: (format: "json" | "markdown") => void;
  onBundle?: () => void;
  loadSourcePreview?: (signal: AbortSignal) => Promise<ReviewResult>;
  onError: (error: unknown) => void;
}

/** 仅响应媒体查询改变分栏方向，不销毁两个面板或其中的草稿。 */
function useNarrowReview(): boolean {
  const [narrow, setNarrow] = useState(
    () => window.matchMedia("(max-width: 1000px)").matches,
  );
  useEffect(() => {
    const query = window.matchMedia("(max-width: 1000px)");
    const update = () => setNarrow(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return narrow;
}

/** 把技术标签转成用户可理解的元素类型，同时保留原生引用。 */
function elementLabel(item: ReviewOrderItem): string {
  if (item.kind === "table") return "表格";
  if (item.kind === "picture") return "图片";
  return item.label === "section_header" || item.label === "title"
    ? "标题"
    : item.label === "list_item"
      ? "列表项"
      : "文本";
}

/** 图意草稿和当前保存版本绑定，关闭、换图或换版本时撤销本地等待。 */
function PictureUnderstanding({
  api,
  documentId,
  resultId,
  elementRef,
  provider,
  onAccept,
}: {
  api: Client;
  documentId: string;
  resultId: string;
  elementRef: string;
  provider: string | null;
  onAccept: (text: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<PictureSuggestion>();
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  function generate() {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setDraft(undefined);
    setError("");
    setOpen(true);
    void api
      .inspectPicture(documentId, resultId, elementRef, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setDraft(result);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(
            reason instanceof Error ? reason.message : "图意生成失败，请重试。",
          );
      });
  }

  function close() {
    request.current?.abort();
    setOpen(false);
  }

  return (
    <>
      <div>
        <Button icon={<Sparkles size={16} />} onClick={generate}>
          {provider === "siliconflow" ? "用硅基流动理解图片" : "理解图片"}
        </Button>
        {provider === "siliconflow" && (
          <p className="review-preview-note">
            点击后会将当前图片发送给硅基流动分析。
          </p>
        )}
      </div>
      <Modal
        title="审校图片说明"
        open={open}
        onCancel={close}
        footer={[
          <Button key="close" onClick={close}>
            关闭
          </Button>,
          error ? (
            <Button key="retry" type="primary" onClick={generate}>
              重试
            </Button>
          ) : (
            <Button
              key="accept"
              type="primary"
              disabled={!draft?.suggested_text}
              onClick={() => {
                if (draft?.suggested_text) onAccept(draft.suggested_text);
                close();
              }}
            >
              采用为图片说明
            </Button>
          ),
        ]}
      >
        {error ? (
          <p role="alert">{error}</p>
        ) : draft ? (
          <>
            <p>{draft.description}</p>
            {!!draft.relationships.length && (
              <>
                <h4>图中关系</h4>
                <ul>
                  {draft.relationships.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </>
            )}
            {!!draft.uncertainties.length && (
              <>
                <h4>待核对</h4>
                <ul>
                  {draft.uncertainties.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </>
            )}
            <p className="review-preview-note">
              {draft.suggested_text
                ? "采用后可继续修改。保存并入库后，图片说明才会参与检索。"
                : "图片细节无法可靠辨认，你可以手动填写图片说明。"}
            </p>
          </>
        ) : (
          <p role="status">正在分析图片…</p>
        )}
      </Modal>
    </>
  );
}

/** 双窗只在用户显式定位时联动，日常阅读滚动互不干扰。 */
export function Review({
  document,
  onEnabledChange,
  enabledChanging = false,
  focusedElementRef,
  versions,
  api,
  changes,
  structuralChanges = [],
  latestResultId,
  busy,
  canEdit,
  visionAvailable,
  visionProvider,
  canIngest,
  activating,
  parseOnly,
  onDelete,
  onReparse,
  onReplaceSource,
  onMetadataDirtyChange,
  onMetadataSaved,
  onChange,
  onStructureChange,
  onVersion,
  onSave,
  focusedReview,
  onToggleFocus,
  onExport,
  onBundle,
  loadSourcePreview,
  onError,
}: ReviewProps) {
  const result: ReviewResult = document.result;
  const order = useMemo(() => readingOrder(result), [result]);
  const [focused, setFocused] = useState(focusedElementRef ?? "");
  const [location, setLocation] = useState<SourceLocation>();
  const [group, setGroup] = useState(0);
  const splitRef = useGroupRef();
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [metadataDirty, setMetadataDirty] = useState(false);
  const [inspectorNotice, setInspectorNotice] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [addition, setAddition] = useState("");
  const [additionLabel, setAdditionLabel] = useState<"text" | "section_header">(
    "text",
  );
  const viewport = useRef<HTMLDivElement>(null);
  const replaceInput = useRef<HTMLInputElement>(null);
  const explicitFocus = useRef(false);
  const selectionSequence = useRef(0);
  const directorySelection = useRef(false);
  const inspectorSelection = useRef(false);
  const narrow = useNarrowReview();
  const history = document.result_id !== latestResultId;
  const indexed =
    document.indexed_result_id === document.result_id &&
    document.indexed_revision === document.revision;
  const dirty = Object.keys(changes).length + structuralChanges.length;
  const editable = canEdit && !history && !busy;
  const pendingAddition = structuralChanges.find(
    (operation) => operation.kind === "append_text",
  );
  const excludedRefs = new Set(result.editor?.excluded_refs ?? []);
  for (const operation of structuralChanges)
    if (operation.kind === "exclude") {
      if (operation.excluded) excludedRefs.add(operation.element_ref);
      else excludedRefs.delete(operation.element_ref);
    }
  const textByRef = useMemo(
    () => new Map(result.texts.map((text) => [text.self_ref, text])),
    [result.texts],
  );
  const tableByRef = useMemo(
    () => new Map(result.tables.map((table) => [table.self_ref, table])),
    [result.tables],
  );
  const pictureByRef = useMemo(
    () =>
      new Map(result.pictures.map((picture) => [picture.self_ref, picture])),
    [result.pictures],
  );
  const assetByRef = useMemo(
    () => new Map(result.assets.map((asset) => [asset.element_ref, asset])),
    [result.assets],
  );
  const filteredOrder = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    return !term
      ? order
      : order.filter((item) =>
          `${item.self_ref} ${elementLabel(item)} ${textByRef.get(item.self_ref)?.text ?? ""}`
            .toLocaleLowerCase()
            .includes(term),
        );
  }, [query, order, textByRef]);
  useEffect(() => {
    explicitFocus.current = Boolean(focusedElementRef);
    setFocused(focusedElementRef ?? "");
    setGroup(0);
    setLocation(undefined);
    setQuery("");
    const item = order.find((value) => value.self_ref === focusedElementRef);
    if (item) {
      setGroup(Math.floor(order.indexOf(item) / 40));
      setLocation({
        ...sourceLocation(result, item),
        selection: ++selectionSequence.current,
      });
    }
  }, [document.result_id, focusedElementRef, order, result]);
  useEffect(() => {
    if (
      !focused ||
      !viewport.current ||
      !explicitFocus.current ||
      directoryOpen ||
      inspectorOpen
    )
      return;
    const target = Array.from(
      viewport.current.querySelectorAll<HTMLElement>("[data-result-ref]"),
    ).find((node) => node.dataset.resultRef === focused);
    if (target) {
      revealInViewport(viewport.current, target);
      target.focus({ preventScroll: true });
      explicitFocus.current = false;
    }
  }, [
    focused,
    group,
    document.result_id,
    directoryOpen,
    inspectorOpen,
    location,
  ]);
  /** 引用、路径、页号按实际粒度匹配；找不到时保留当前内容并提示。 */
  function selectSource(source: SourceLocation): boolean {
    const origin = result.origins?.find(
      (value) =>
        value.locator === source.locator ||
        (source.locator && value.locator === `${source.locator}/text()`),
    );
    const item =
      order.find(
        (value) =>
          value.self_ref === (source.elementRef ?? origin?.element_ref),
      ) ??
      (source.page
        ? order.find((value) => value.page === source.page)
        : undefined);
    if (!item) {
      setInspectorNotice(
        "此原文位置没有对应的解析元素，可在目录查找相关内容。",
      );
      setInspectorOpen(true);
      return false;
    }
    selectItem(item);
    return true;
  }
  /** 定位只设置本双窗状态，不访问全局页面滚动或合成区域框。 */
  function selectItem(item: ReviewOrderItem): void {
    explicitFocus.current = true;
    setFocused(item.self_ref);
    setGroup(Math.floor(order.indexOf(item) / 40));
    setLocation({
      ...sourceLocation(result, item),
      selection: ++selectionSequence.current,
    });
  }
  /** 元数据编辑器关闭前必须保存，避免折叠面板悄悄丢弃未保存字段。 */
  const metadataChanged = useCallback(
    (value: boolean): void => {
      setMetadataDirty(value);
      if (!value) setInspectorNotice("");
      onMetadataDirtyChange(value);
    },
    [onMetadataDirtyChange],
  );
  /** 展示待保存字段值，原件内容仍由独立来源快照提供。 */
  function pictureValue(
    ref: string,
    kind: "picture_caption" | "picture_description",
    original: string,
  ): string {
    const operation = structuralChanges.find(
      (value) => value.kind === kind && value.element_ref === ref,
    );
    return operation && "text" in operation ? operation.text : original;
  }
  /** 弹窗退场后再恢复定位焦点，不让对话框的焦点还原覆盖结果定位。 */
  function focusResultAfterClose(): void {
    if (!viewport.current) return;
    const target = Array.from(
      viewport.current.querySelectorAll<HTMLElement>("[data-result-ref]"),
    ).find((node) => node.dataset.resultRef === focused);
    if (target) {
      revealInViewport(viewport.current, target);
      target.focus({ preventScroll: true });
      explicitFocus.current = false;
    }
  }
  const inspectCount = result.warnings.length;
  const resultEngine = result.options.engine ?? document.parser_engine;
  // 展示当前保存版本的实际选项，历史缺失不能由库名或当前默认设置推断。
  const ocrLabel =
    result.options.ocr_enabled === null
      ? "OCR 配置未记录"
      : result.options.ocr_enabled
        ? "OCR 已开启"
        : "OCR 已关闭";
  return (
    <div className="review-studio" data-review-document={document.document_id}>
      <header className="review-studio-toolbar">
        <div className="review-studio-title">
          <FileText size={23} />
          <div>
            <h2 title={document.name}>{document.name}</h2>
            <p>
              {ENGINE_LABELS[resultEngine] ?? resultEngine} ·{" "}
              {result.source.format.toUpperCase()} ·{" "}
              {formatSize(result.source.size_bytes)} · {order.length} 个元素
              {" · "}
              {ocrLabel}
            </p>
          </div>
        </div>
        <div className="review-studio-actions">
          {!parseOnly && onEnabledChange && (
            <DocumentEnablement
              name={document.name}
              enabled={document.enabled}
              loading={enabledChanging}
              disabled={!canEdit || Boolean(busy) || enabledChanging}
              onChange={onEnabledChange}
            />
          )}
          <Select
            aria-label="解析版本"
            value={document.result_id}
            onChange={onVersion}
            disabled={Boolean(busy)}
            options={versions.map((version) => ({
              value: version.result_id,
              label: `版本 ${version.revision}${version.result_id === latestResultId ? " · 当前" : " · 历史只读"}`,
            }))}
          />
          <Dropdown
            disabled={Boolean(busy)}
            trigger={["click"]}
            menu={{
              items: [
                {
                  key: "saved",
                  label: `导出当前已保存的版本 ${result.revision}`,
                  disabled: true,
                },
                {
                  key: "json",
                  label: "结构 JSON",
                  onClick: () => onExport("json"),
                },
                {
                  key: "markdown",
                  label: "Markdown",
                  onClick: () => onExport("markdown"),
                },
                ...(onBundle
                  ? [
                      {
                        key: "bundle",
                        label: "图文资源包 ZIP（含原件）",
                        onClick: onBundle,
                      },
                    ]
                  : []),
              ],
            }}
          >
            <Button disabled={Boolean(busy)} icon={<Download size={15} />}>
              导出
            </Button>
          </Dropdown>
          {(onReparse || onReplaceSource) && (
            <Dropdown
              disabled={Boolean(busy)}
              trigger={["click"]}
              menu={{
                items: [
                  ...(onReparse
                    ? [
                        {
                          key: "reparse",
                          label: "使用原件重新解析",
                          disabled:
                            !canEdit ||
                            Boolean(busy) ||
                            Boolean(dirty) ||
                            metadataDirty,
                          onClick: onReparse,
                        },
                      ]
                    : []),
                  ...(onReplaceSource
                    ? [
                        {
                          key: "replace",
                          label: "替换原件并重新解析",
                          disabled:
                            !canEdit ||
                            Boolean(busy) ||
                            Boolean(dirty) ||
                            metadataDirty,
                          onClick: () => replaceInput.current?.click(),
                        },
                      ]
                    : []),
                ],
              }}
            >
              <Button type="text" disabled={Boolean(busy)}>
                重新处理
              </Button>
            </Dropdown>
          )}
          {onReplaceSource && (
            <input
              ref={replaceInput}
              className="sr-only"
              type="file"
              aria-label="替换原件文件"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) onReplaceSource(file);
                event.target.value = "";
              }}
            />
          )}
          {canEdit && (
            <Button
              type="text"
              danger
              aria-label="撤销此资料"
              title="撤销资料"
              disabled={Boolean(busy)}
              onClick={onDelete}
            >
              <Trash2 size={15} />
            </Button>
          )}
        </div>
      </header>
      {!parseOnly && !document.enabled && (
        <p className="review-preview-note" role="status">
          此资料已禁用，不参与检索。原件、解析结果和历史版本仍可查看和编辑。
        </p>
      )}
      <div className="review-studio-status">
        <div className="review-studio-actions">
          <Button
            size="small"
            icon={<List size={14} />}
            onClick={() => setDirectoryOpen(true)}
          >
            目录与定位
          </Button>
          <Modal
            centered
            destroyOnHidden
            open={directoryOpen}
            title="目录与定位"
            width={560}
            className="review-directory-dialog"
            footer={null}
            onCancel={() => setDirectoryOpen(false)}
            focusable={{ focusTriggerAfterClose: !directorySelection.current }}
            afterClose={() => {
              // 目录定位后将焦点交还结果元素，避免关闭动画把焦点拉回触发按钮。
              if (!directorySelection.current) return;
              directorySelection.current = false;
              focusResultAfterClose();
            }}
          >
            <p>按原生阅读顺序浏览，或查找文本、标题及元素引用。</p>
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索内容或元素引用"
              aria-label="查找解析元素"
              prefix={<Search size={15} />}
            />
            <div className="review-directory-scroll">
              <ReviewScroll label="文档目录">
                <div className="review-directory-list">
                  {filteredOrder.slice(0, 300).map((item) => (
                    <Button
                      type="text"
                      key={item.self_ref}
                      className={focused === item.self_ref ? "is-focused" : ""}
                      onClick={() => {
                        directorySelection.current = true;
                        selectItem(item);
                        setDirectoryOpen(false);
                      }}
                    >
                      <div className="review-directory-item">
                        <span>
                          {elementLabel(item)}
                          {item.page ? ` · 第 ${item.page} 页` : ""}
                        </span>
                        <p>
                          {textByRef.get(item.self_ref)?.text.slice(0, 110) ??
                            item.self_ref}
                        </p>
                        <code>{item.self_ref}</code>
                      </div>
                    </Button>
                  ))}
                  {!filteredOrder.length && <p>未找到匹配元素。</p>}
                  {filteredOrder.length > 300 && (
                    <p>显示前 300 个匹配项，请输入关键词缩小范围。</p>
                  )}
                </div>
              </ReviewScroll>
            </div>
            <div className="review-dialog-footer">
              <span>{filteredOrder.length} 个匹配元素</span>
              <Button onClick={() => setDirectoryOpen(false)}>关闭</Button>
            </div>
          </Modal>
          <Tag
            color={
              history ? "default" : indexed || parseOnly ? "success" : "warning"
            }
          >
            {history
              ? "历史版本 · 只读"
              : parseOnly
                ? "独立解析"
                : activating
                  ? "正在生效"
                  : indexed
                    ? "已生效"
                    : document.indexed_result_id
                      ? "线上仍用旧版"
                      : "未生效"}
          </Tag>
          {dirty > 0 && <Tag color="blue">{dirty} 处未保存</Tag>}
        </div>
        <Button
          size="small"
          icon={<PanelRight size={14} />}
          onClick={() => setInspectorOpen(true)}
        >
          检查与资源
          {inspectCount > 0 && <Tag color="warning">{inspectCount}</Tag>}
        </Button>
        <Modal
          centered
          destroyOnHidden
          open={inspectorOpen}
          title="检查、资源与业务字段"
          width={620}
          className="review-inspector-dialog"
          footer={null}
          mask={{ closable: !metadataDirty }}
          keyboard={!metadataDirty}
          focusable={{ focusTriggerAfterClose: !inspectorSelection.current }}
          afterClose={() => {
            if (!inspectorSelection.current) return;
            inspectorSelection.current = false;
            focusResultAfterClose();
          }}
          onCancel={() => {
            if (metadataDirty) {
              setInspectorNotice("业务字段尚未保存，请在下方保存后关闭面板。");
              return;
            }
            setInspectorOpen(false);
          }}
        >
          <p>核对解析风险、查看关联图片，并填写业务字段。</p>
          {inspectorNotice && (
            <p className="review-inspector-notice" role="status">
              {inspectorNotice}
            </p>
          )}
          <div className="review-inspector-scroll">
            <ReviewScroll label="检查与资源独立滚动区域">
              <div className="review-inspector-content">
                <section>
                  <h3>解析校验 · {inspectCount} 项提示</h3>
                  {inspectCount ? (
                    result.warnings.map((warning, index) => (
                      <div
                        className="review-check-warning"
                        key={`${warning.code}-${index}`}
                      >
                        <AlertTriangle size={15} />
                        <div>
                          <p>{warning.message}</p>
                          <code>{warning.code}</code>
                          {warning.page && (
                            <Button
                              type="text"
                              size="small"
                              onClick={() => {
                                if (metadataDirty) {
                                  setInspectorNotice(
                                    "业务字段尚未保存，请先保存后定位原文。",
                                  );
                                  return;
                                }
                                if (
                                  selectSource({
                                    page: warning.page ?? undefined,
                                  })
                                ) {
                                  inspectorSelection.current = true;
                                  setInspectorOpen(false);
                                }
                              }}
                            >
                              定位第 {warning.page} 页
                            </Button>
                          )}
                        </div>
                      </div>
                    ))
                  ) : (
                    <p className="review-check-ok">
                      <Check size={15} />
                      解析器未报告风险，请结合原文人工核对。
                    </p>
                  )}
                  <p className="review-preview-note">
                    校验提示不等同于准确率评测；图片说明也不代表 OCR 已完成。
                  </p>
                  {!result.order?.length && (
                    <p className="review-inspector-notice">
                      此旧结果没有原生阅读顺序，当前分类型展示。重新解析可获得顺序信息。
                    </p>
                  )}
                </section>
                <section>
                  <h3>关联图片 · {result.assets.length} 个资源</h3>
                  {result.assets.length ? (
                    result.assets.map((asset, index) => (
                      <AssetImage
                        key={`${asset.asset_id}-${index}`}
                        asset={asset}
                        api={api}
                      />
                    ))
                  ) : (
                    <p>此版本未提取到图片资产。</p>
                  )}
                </section>
                <MetadataEditor
                  document={document}
                  api={api}
                  parseOnly={parseOnly}
                  canEdit={editable && !dirty}
                  showHeading
                  onDirtyChange={metadataChanged}
                  onSaved={onMetadataSaved}
                />
                <section>
                  <h3>版本身份</h3>
                  <dl className="review-version-info">
                    <dt>版本</dt>
                    <dd>
                      v{result.revision}
                      {history ? "（历史只读）" : "（当前）"}
                    </dd>
                    <dt>解析方式</dt>
                    <dd>{ENGINE_LABELS[resultEngine] ?? resultEngine}</dd>
                    <dt>本次 OCR</dt>
                    <dd>{ocrLabel}（保存版本的配置）</dd>
                    <dt>结果 ID</dt>
                    <dd>
                      <code>{document.result_id}</code>
                    </dd>
                    <dt>资料 ID</dt>
                    <dd>
                      <code>{document.document_id}</code>
                    </dd>
                    <dt>原件资产</dt>
                    <dd>
                      <code>{result.source.asset_id ?? "未保存"}</code>
                    </dd>
                  </dl>
                </section>
              </div>
            </ReviewScroll>
          </div>
        </Modal>
      </div>
      {history && (
        <div className="review-history-notice">
          <History size={14} />
          正在查看 v{result.revision} 的不可变历史。选择当前版本后可继续修订。
        </div>
      )}
      <Group
        groupRef={splitRef}
        id="document-review-panes"
        orientation={narrow ? "vertical" : "horizontal"}
        className="review-split-group"
        resizeTargetMinimumSize={{ coarse: 28, fine: 12 }}
      >
        <Panel
          id="source"
          defaultSize="48%"
          minSize="25%"
          className="review-split-panel"
        >
          <section className="review-pane" aria-label="原文对照">
            <div className="review-pane-heading">
              <h3>原文</h3>
              <span>只读 · 原始资产</span>
            </div>
            <OriginalPreview
              result={result}
              api={api}
              onError={onError}
              location={location}
              onSelect={selectSource}
              loadSourcePreview={loadSourcePreview}
            />
          </section>
        </Panel>
        <Separator
          onDoubleClick={() =>
            splitRef.current?.setLayout({ source: 48, result: 52 })
          }
          className="review-resize-separator"
          aria-label={
            narrow
              ? "调整原文与结果的高度，方向键调整，双击还原"
              : "调整原文与结果的宽度，方向键调整，双击还原"
          }
        >
          <span />
        </Separator>
        <Panel
          id="result"
          defaultSize="52%"
          minSize="30%"
          className="review-split-panel"
        >
          <section className="review-pane" aria-label="解析结果">
            <div className="review-pane-heading">
              <h3>
                解析结果 <Tag color="default">v{result.revision}</Tag>
              </h3>
              <span>{history ? "历史只读" : "修订后保存为新版本"}</span>
            </div>
            <ReviewScroll label="解析结果独立滚动区域" viewportRef={viewport}>
              <div className="review-result-content">
                {order
                  .slice(group * 40, (group + 1) * 40)
                  .map((item, localIndex) => {
                    const text = textByRef.get(item.self_ref);
                    const table = tableByRef.get(item.self_ref);
                    const picture = pictureByRef.get(item.self_ref);
                    const asset = assetByRef.get(item.self_ref);
                    const excluded = excludedRefs.has(item.self_ref);
                    return (
                      <article
                        key={item.self_ref}
                        tabIndex={-1}
                        data-result-ref={item.self_ref}
                        className={`review-element ${focused === item.self_ref ? "is-focused" : ""} ${excluded ? "is-excluded" : ""} ${item.self_ref in changes ? "is-edited" : ""}`}
                      >
                        <div className="review-element-heading">
                          <span>
                            {group * 40 + localIndex + 1}. {elementLabel(item)}
                            {item.page ? ` · 第 ${item.page} 页` : ""}
                          </span>
                          <div>
                            <Button
                              size="small"
                              type="text"
                              title="定位原文的页或段落"
                              aria-label={`定位元素 ${group * 40 + localIndex + 1} 的原文`}
                              onClick={() => selectItem(item)}
                            >
                              <LocateFixed size={14} />
                              原文
                            </Button>
                            {onStructureChange && (
                              <Button
                                size="small"
                                type="text"
                                disabled={!editable}
                                onClick={() =>
                                  onStructureChange({
                                    kind: "exclude",
                                    element_ref: item.self_ref,
                                    excluded: !excluded,
                                  })
                                }
                              >
                                {excluded ? "恢复" : "排除"}
                              </Button>
                            )}
                          </div>
                        </div>
                        <code className="review-element-ref">
                          {item.self_ref}
                        </code>
                        {excluded && (
                          <p className="review-excluded-note">
                            此元素已标记排除，将保留在解析结果中，供追溯及恢复。
                          </p>
                        )}
                        {text && (
                          <Input.TextArea
                            className={`review-text-editor ${item.label === "section_header" || item.label === "title" ? "is-heading" : ""}`}
                            aria-label={`${elementLabel(item)} ${group * 40 + localIndex + 1}`}
                            rows={Math.max(
                              2,
                              Math.min(
                                16,
                                text.text.split("\n").length +
                                  Math.ceil(text.text.length / 70),
                              ),
                            )}
                            value={changes[item.self_ref] ?? text.text}
                            readOnly={!editable || excluded}
                            spellCheck={false}
                            onFocus={() => setFocused(item.self_ref)}
                            onChange={(event) =>
                              onChange(item.self_ref, event.target.value)
                            }
                          />
                        )}
                        {table && (
                          <>
                            <p className="review-preview-note">
                              {table.rows} 行 × {table.cols} 列 ·{" "}
                              {editable && onStructureChange && !excluded
                                ? "可修改单元格文字，保留合并结构"
                                : "只读"}
                            </p>
                            <ReviewTable
                              table={table}
                              editable={
                                editable &&
                                Boolean(onStructureChange) &&
                                !excluded
                              }
                              operations={structuralChanges}
                              onChange={onStructureChange}
                            />
                          </>
                        )}
                        {picture && (
                          <div className="review-picture-editor">
                            {asset ? (
                              <AssetImage asset={asset} api={api} />
                            ) : (
                              <p className="review-preview-note">
                                原文图片没有可内嵌的资产，请下载原件核对。
                              </p>
                            )}
                            <label>
                              图片标题
                              <Input.TextArea
                                aria-label={`图片 ${group * 40 + localIndex + 1} 的标题`}
                                rows={2}
                                value={pictureValue(
                                  item.self_ref,
                                  "picture_caption",
                                  picture.caption,
                                )}
                                readOnly={
                                  !editable || !onStructureChange || excluded
                                }
                                onChange={(event) =>
                                  onStructureChange?.({
                                    kind: "picture_caption",
                                    element_ref: item.self_ref,
                                    text: event.target.value,
                                  })
                                }
                              />
                            </label>
                            <label>
                              图片内容说明
                              <Input.TextArea
                                aria-label={`图片 ${group * 40 + localIndex + 1} 的内容说明`}
                                rows={3}
                                value={pictureValue(
                                  item.self_ref,
                                  "picture_description",
                                  result.editor?.picture_descriptions[
                                    item.self_ref
                                  ] ?? "",
                                )}
                                placeholder="补充图中含义、流程及关键文字；不会覆盖原图"
                                readOnly={
                                  !editable || !onStructureChange || excluded
                                }
                                onChange={(event) =>
                                  onStructureChange?.({
                                    kind: "picture_description",
                                    element_ref: item.self_ref,
                                    text: event.target.value,
                                  })
                                }
                              />
                            </label>
                            {visionAvailable &&
                              asset &&
                              editable &&
                              !excluded &&
                              onStructureChange && (
                                <PictureUnderstanding
                                  key={document.result_id}
                                  api={api}
                                  documentId={document.document_id}
                                  resultId={document.result_id}
                                  elementRef={item.self_ref}
                                  provider={visionProvider}
                                  onAccept={(text) =>
                                    onStructureChange({
                                      kind: "picture_description",
                                      element_ref: item.self_ref,
                                      text,
                                    })
                                  }
                                />
                              )}
                          </div>
                        )}
                      </article>
                    );
                  })}
                {pendingAddition?.kind === "append_text" && (
                  <article className="review-element is-edited">
                    <div className="review-element-heading">
                      新增
                      {pendingAddition.label === "section_header"
                        ? "标题"
                        : "文本"}{" "}
                      · 未保存
                    </div>
                    <Input.TextArea
                      className="review-text-editor"
                      aria-label="新增待保存文本"
                      value={pendingAddition.text}
                      readOnly={!editable}
                      rows={4}
                      onChange={(event) =>
                        onStructureChange?.({
                          ...pendingAddition,
                          text: event.target.value,
                        })
                      }
                    />
                    <p className="review-preview-note">
                      人工补充内容，没有对应原文段落。
                    </p>
                  </article>
                )}
                {!order.length && (
                  <div className="review-preview-empty">
                    未解析到可审校元素。请检查解析提示和原件。
                  </div>
                )}
              </div>
            </ReviewScroll>
            <div className="review-pagination">
              <Button
                size="small"
                type="default"
                aria-label="上一组结果"
                disabled={group === 0}
                onClick={() => setGroup((value) => value - 1)}
              >
                <ChevronLeft size={14} />
              </Button>
              <span>
                {order.length
                  ? `${group * 40 + 1}–${Math.min((group + 1) * 40, order.length)} / ${order.length}`
                  : "0 个元素"}
              </span>
              <Button
                size="small"
                type="default"
                aria-label="下一组结果"
                disabled={(group + 1) * 40 >= order.length}
                onClick={() => setGroup((value) => value + 1)}
              >
                <ChevronRight size={14} />
              </Button>
              <span className="review-control-spacer" />
              {onStructureChange && (
                <>
                  <Button
                    size="small"
                    type="text"
                    disabled={!editable || Boolean(pendingAddition)}
                    icon={<Plus size={14} />}
                    onClick={() => setAddOpen(true)}
                  >
                    补充文本
                  </Button>
                  <Modal
                    centered
                    destroyOnHidden
                    open={addOpen}
                    title="补充解析遗漏的内容"
                    width={500}
                    footer={null}
                    mask={{ closable: false }}
                    keyboard={false}
                    onCancel={() => setAddOpen(false)}
                  >
                    <p>追加人工文本，原件不受影响。保存版本后可继续追加。</p>
                    <Select
                      aria-label="补充内容类型"
                      value={additionLabel}
                      onChange={(value: "text" | "section_header") =>
                        setAdditionLabel(value)
                      }
                      options={[
                        { value: "text", label: "正文文本" },
                        { value: "section_header", label: "标题" },
                      ]}
                    />
                    <Input.TextArea
                      className="review-addition-text"
                      aria-label="补充内容"
                      rows={6}
                      value={addition}
                      onChange={(event) => setAddition(event.target.value)}
                    />
                    <div className="review-dialog-footer">
                      <Button onClick={() => setAddOpen(false)}>返回</Button>
                      <Button
                        type="primary"
                        disabled={!addition.trim() || !editable}
                        onClick={() => {
                          onStructureChange({
                            kind: "append_text",
                            label: additionLabel,
                            text: addition,
                          });
                          setAddition("");
                          setAddOpen(false);
                        }}
                      >
                        加入草稿
                      </Button>
                    </div>
                  </Modal>
                </>
              )}
            </div>
          </section>
        </Panel>
      </Group>
      <footer className="review-studio-footer">
        <Button type="text" onClick={onToggleFocus}>
          {focusedReview ? "退出专注审校" : "专注审校"}
        </Button>
        <div className="review-save-status" role="status">
          {busy ??
            (dirty
              ? `${dirty} 处修改尚未保存，切换前请先处理草稿`
              : history
                ? `历史版本 v${result.revision} · 只读`
                : "解析修改已保存")}
        </div>
        <div className="review-studio-actions">
          <Button
            type="primary"
            disabled={
              !editable ||
              metadataDirty ||
              (parseOnly
                ? !dirty
                : !canIngest ||
                  (!dirty &&
                    (activating || (indexed && selectionCurrent(document)))))
            }
            onClick={onSave}
          >
            <Save size={15} />
            {parseOnly
              ? "保存新版本"
              : dirty
                ? "保存成新版本生效"
                : "生效此版本"}
          </Button>
        </div>
      </footer>
    </div>
  );
}

/** 以二进制单位显示真实文件大小。 */
export function formatSize(bytes: number): string {
  return bytes < 1024
    ? `${bytes} B`
    : bytes < 1024 * 1024
      ? `${(bytes / 1024).toFixed(1)} KB`
      : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
