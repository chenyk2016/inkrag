/** 工作台负责页面状态和用户操作，业务规则由 HTTP 核心完成。 */
import {
  useBlocker,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { App, Button, Modal, Switch, Tag } from "antd";
import {
  platformClient,
  type DocumentListItem,
  type DocumentListStatus,
} from "./platform-api";
import { DocumentCatalog, canActivateBusinessFields } from "./document-catalog";
import { operationKey, type StructureOperation } from "./review-types";
import { ServiceStudio } from "./service-studio";
import { FieldSchema } from "./field-schema";
import { MetadataEditor } from "./metadata";
import { ReviewScroll } from "./review-scroll";
import { DocumentRecovery } from "./document-recovery";
import {
  Activity,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AlertCircle,
  Check,
  ChevronRight,
  LoaderCircle,
  RefreshCw,
  Upload,
  X,
} from "lucide-react";
import {
  ApiError,
  type Capabilities,
  type DocumentDetail,
  type DocumentSummary,
  type Version,
  type Job,
  type KnowledgeBase,
} from "./api";
import { downloadBlob } from "./assets";
import { Review } from "./review";
import { SearchView } from "./search";
import { Settings } from "./settings";
import { UploadWizard } from "./upload-wizard";
import { Management } from "./management";
import {
  KnowledgeNav,
  knowledgePageTitle,
  type KnowledgePage,
} from "./knowledge-nav";

type Page = KnowledgePage | "upload";

/** 错误展示不序列化未知对象或请求配置。 */
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作未完成，请重试。";
}

/** 已连接工作台维护纯界面状态；所有业务结果来自服务。 */
export function Workbench({
  token,
  capabilities,
  knowledgeBase,
  onChooseKnowledgeBase,
}: {
  token: () => string;
  capabilities: Capabilities;
  knowledgeBase: KnowledgeBase;
  onChooseKnowledgeBase: () => void;
}) {
  const { modal, message } = App.useApp();
  const parseOnly =
    capabilities.mode === "parse" || knowledgeBase.kind === "parse";
  const routeNavigate = useNavigate(),
    { view } = useParams(),
    [routeParams] = useSearchParams();
  const collectionLabel = parseOnly ? "资料集合" : "知识库";
  const tokenReference = useRef(token);
  tokenReference.current = token;
  const api = useMemo(
    // 每次请求读取最新内存令牌，续期不依赖父页面重新渲染。
    () => platformClient(() => tokenReference.current(), knowledgeBase.kb_id),
    [knowledgeBase.kb_id],
  );
  const canEdit = capabilities.roles.some((role) =>
    ["admin", "editor"].includes(role),
  );
  const knownViews = new Set<Page>([
    "documents",
    "search",
    "fields",
    "operations",
    "settings",
    "upload",
    "configuration",
    "evaluation",
    "diagnostics",
    "integrations",
  ]);
  const selectedPage: Page = knownViews.has(view as Page)
    ? (view as Page)
    : "documents";
  /** 库内导航也经过数据路由，未保存内容由统一离开保护处理。 */
  function setPage(next: Page): void {
    routeNavigate(`/knowledge/${knowledgeBase.kb_id}/${next}`);
  }
  const page =
    parseOnly &&
    [
      "search",
      "configuration",
      "evaluation",
      "diagnostics",
      "integrations",
    ].includes(selectedPage)
      ? "documents"
      : selectedPage;
  const [searchTarget, setSearchTarget] = useState<"published" | "candidate">(
    "published",
  );
  const studioPage = [
    "configuration",
    "evaluation",
    "diagnostics",
    "integrations",
    "operations",
  ].includes(page);
  const studioView =
    page === "configuration"
      ? "draft"
      : page === "evaluation"
        ? "evaluation"
        : page === "diagnostics"
          ? "trace"
          : page === "integrations"
            ? "integrations"
            : page === "operations"
              ? "operations"
              : "trial";
  const pageTitle =
    page === "upload"
      ? "上传资料"
      : parseOnly && page === "operations"
        ? "解析任务"
        : knowledgePageTitle(page);
  const [changingEnabledId, setChangingEnabledId] = useState<string | null>(
    null,
  );
  const enablementLocked = useRef(false);
  const [documents, setDocuments] = useState<DocumentListItem[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [nextOffset, setNextOffset] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [selectedCurrent, setSelectedCurrent] =
    useState<DocumentSummary | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [requestedVersion, setRequestedVersion] = useState<
    string | undefined
  >();
  const [focusedElementRef, setFocusedElementRef] = useState<
    string | undefined
  >();
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [metadataDirty, setMetadataDirty] = useState(false),
    [reviewEpoch, setReviewEpoch] = useState(0);
  const [focusReview, setFocusReview] = useState(false);
  const [editingDocument, setEditingDocument] =
    useState<DocumentListItem | null>(null);
  const [savingBusinessFields, setSavingBusinessFields] = useState(false);
  const [autoActivate, setAutoActivate] = useState(knowledgeBase.auto_activate);
  const [settingActivation, setSettingActivation] = useState(false);
  const [serviceDirty, setServiceDirty] = useState(false);
  const [serviceEpoch, setServiceEpoch] = useState(0);
  const [structuralChanges, setStructuralChanges] = useState<
    StructureOperation[]
  >([]);
  const [changes, setChanges] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [filter, setFilter] = useState("");
  const [catalogState, setCatalogState] = useState<DocumentListStatus>();
  const [counts, setCounts] = useState<
    Partial<Record<DocumentListStatus, number>>
  >({});
  const selected =
    documents.find((document) => document.document_id === selectedId) ??
    selectedCurrent;
  const dirty =
    serviceDirty ||
    Object.keys(changes).length > 0 ||
    metadataDirty ||
    structuralChanges.length > 0;
  const blocker = useBlocker(dirty);
  /** 路由获准切换后才清理旧草稿，取消离开保留全部输入。 */
  useEffect(() => {
    const nextDocument = routeParams.get("doc");
    const nextVersion = routeParams.get("result") ?? undefined;
    // 重选当前资料或仅变更定位时不清空结果；相同读取依赖不会再次发起请求。
    if (nextDocument !== selectedId || nextVersion !== requestedVersion) {
      setDetail(null);
    }
    if (nextDocument !== selectedId) setSelectedCurrent(null);
    if (metadataDirty) setReviewEpoch((epoch) => epoch + 1);
    setEditingDocument(null);
    setSelectedId(nextDocument);
    if (!nextDocument) setFocusReview(false);
    setRequestedVersion(nextVersion);
    setFocusedElementRef(routeParams.get("element") ?? undefined);
    setChanges({});
    setStructuralChanges([]);
    setMetadataDirty(false);
    setError(null);
  }, [routeParams]);
  const documentOpen = page === "documents" && routeParams.has("doc");

  /** 刷新目录，同时保留用户正在查看的资料。 */
  const listRequest = useRef(0);
  const queryKey = JSON.stringify([filter, catalogState]);
  const activeFilter = useRef(queryKey);
  activeFilter.current = queryKey;
  const refresh = useCallback(async (): Promise<DocumentSummary[]> => {
    const requestNumber = ++listRequest.current;
    const page = await api.documentPage(filter, 0, undefined, catalogState);
    if (
      requestNumber !== listRequest.current ||
      activeFilter.current !== queryKey
    )
      return page.items;
    setDocuments(page.items);
    setCounts(page.counts);
    setHasMore(page.items.length < page.total);
    setNextOffset(page.items.length);
    return page.items;
  }, [api, filter, catalogState, queryKey]);
  useEffect(() => {
    let active = true;
    setLoading(true);
    refresh()
      .catch((reason: unknown) => {
        if (active) setError(reason);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [refresh]);
  useEffect(() => {
    if (!selectedId || selected?.result_id === "") {
      setDetail(null);
      setVersions([]);
      return;
    }
    if (dirty && detail?.document_id === selectedId) return;
    // 撤回最后一处输入或保存业务字段时保留当前视口；只有真正切版本才重新挂载。
    if (
      detail?.document_id === selectedId &&
      detail.result_id === (requestedVersion ?? selected?.result_id) &&
      detail.revision === selected?.revision
    )
      return;
    const controller = new AbortController();
    setReading(true);
    setDetail(null);
    setVersions([]);
    setChanges({});
    const currentRequest = api.document(
      selectedId,
      undefined,
      controller.signal,
    );
    Promise.all([
      requestedVersion
        ? api.document(selectedId, requestedVersion, controller.signal)
        : currentRequest,
      currentRequest,
      api.versions(selectedId, controller.signal),
    ])
      .then(([next, current, history]) => {
        if (!controller.signal.aborted) {
          setDetail(next);
          setSelectedCurrent(current);
          setVersions(history);
        }
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        if (reason instanceof ApiError && reason.code === "not_ready") {
          void api
            .documentInfo(selectedId, controller.signal)
            .then((info) => {
              if (!controller.signal.aborted) setSelectedCurrent(info);
            })
            .catch((error: unknown) => {
              if (!controller.signal.aborted) setError(error);
            });
        } else setError(reason);
      })
      .finally(() => {
        if (!controller.signal.aborted) setReading(false);
      });
    return () => controller.abort();
  }, [
    api,
    selectedId,
    requestedVersion,
    selected?.result_id,
    selected?.revision,
    dirty,
  ]);
  useEffect(() => {
    let alive = true;
    const timer = window.setInterval(() => {
      if (!dirty && !busy && alive && nextOffset <= 100)
        void refresh().catch((reason: unknown) => {
          if (alive) setError(reason);
        });
    }, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [refresh, dirty, busy, nextOffset]);
  useEffect(() => {
    /** 离开页面时提示未保存的修改，防止意外丢失审校内容。 */
    function beforeUnload(event: BeforeUnloadEvent): void {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  /** 有界追加目录页，明确区分已加载数量和全库总量。 */
  async function loadMore(): Promise<void> {
    if (loadingMore || !hasMore || busy) return;
    setLoadingMore(true);
    try {
      const requestNumber = listRequest.current;
      const response = await api.documentPage(
        filter,
        nextOffset,
        undefined,
        catalogState,
      );
      if (
        requestNumber !== listRequest.current ||
        activeFilter.current !== queryKey
      )
        return;
      const next = response.items;
      setCounts(response.counts);
      setDocuments((previous) => {
        const ids = new Set(previous.map((document) => document.document_id));
        return [
          ...previous,
          ...next.filter((document) => !ids.has(document.document_id)),
        ];
      });
      setNextOffset(nextOffset + next.length);
      setHasMore(nextOffset + next.length < response.total);
    } catch (reason: unknown) {
      setError(reason);
    } finally {
      setLoadingMore(false);
    }
  }

  /** 导航前确认是否放弃尚未保存的修订，已保存历史不受影响。 */
  async function canLeave(): Promise<boolean> {
    if (!dirty) return true;
    return await modal.confirm({
      title: "放弃尚未保存的修改？",
      content: "当前审校或业务字段尚未保存。继续当前操作将丢弃这些输入。",
      okText: "放弃并继续",
      cancelText: "继续编辑",
      okButtonProps: { danger: true },
      focusable: { autoFocusButton: "cancel" },
    });
  }
  /** 选择资料或来源版本；快速切换时旧请求不会覆盖新选择。 */
  function openDocument(
    id: string,
    resultId?: string,
    elementRef?: string,
  ): void {
    if (busy) return;
    const params = new URLSearchParams({ doc: id });
    if (resultId) params.set("result", resultId);
    if (elementRef) params.set("element", elementRef);
    routeNavigate(`/knowledge/${knowledgeBase.kb_id}/documents?${params}`);
  }
  /** 切换主视图，保留已经保存的资料选择。 */
  function navigate(next: Page): void {
    if ((parseOnly && next === "search") || busy) return;
    setPage(next);
  }
  /** 只保留与已保存版本不同的文字元素，撤回修改会清除脏状态。 */
  function changeText(ref: string, value: string): void {
    const original = detail?.result.texts.find(
      (element) => element.self_ref === ref,
    )?.text;
    setChanges((previous) => {
      const next = { ...previous };
      if (value === original) delete next[ref];
      else next[ref] = value;
      return next;
    });
    setNotice("");
  }
  /** 确认成功后刷新资料，避免重复提交已经接受的批次。 */
  async function finishUpload(jobs: Job[]): Promise<void> {
    setChanges({});
    setStructuralChanges([]);
    setMetadataDirty(false);
    setReviewEpoch((epoch) => epoch + 1);
    if (serviceDirty) {
      setServiceDirty(false);
      setServiceEpoch((epoch) => epoch + 1);
    }
    try {
      await refresh();
    } catch (reason: unknown) {
      setError(reason);
    }
    setNotice(
      `${jobs.length} 份资料已提交，处理状态见资料列表。${parseOnly ? "" : autoActivate ? "处理完成后会自动生效。" : "自动生效已关闭，可打开资料手动生效。"}`,
    );
  }
  /** 新版本成功后才清除草稿；冲突或失败保留用户修改。 */
  async function save(confirmContent = false): Promise<void> {
    if (!detail || (!dirty && !confirmContent) || busy || metadataDirty) return;
    if (Object.values(changes).some((text) => !text.trim())) {
      setError(
        new ApiError(
          "empty_text",
          "文本元素不能为空；请保留有意义的文字后再保存。",
        ),
      );
      return;
    }
    setBusy("正在保存新版本…");
    setError(null);
    setNotice("");
    try {
      const operations: StructureOperation[] = [
        ...Object.entries(changes).map(
          ([element_ref, text]): StructureOperation => ({
            kind: "text",
            element_ref,
            text,
          }),
        ),
        ...structuralChanges,
      ];
      if (operations.length) {
        await api.structure(
          detail.document_id,
          detail.result_id,
          operations,
          confirmContent ? detail.revision : undefined,
        );
      } else if (confirmContent) {
        await api.selectResult(detail, detail.result_id, false, true);
      }
      const next = await api.document(detail.document_id);
      setStructuralChanges([]);
      setDetail(next);
      setChanges({});
      setRequestedVersion(undefined);
      const [, history] = await Promise.all([
        refresh(),
        api.versions(next.document_id),
      ]);
      setVersions(history);
      if (routeParams.has("result") || routeParams.has("element"))
        routeNavigate(
          `/knowledge/${knowledgeBase.kb_id}/documents?doc=${next.document_id}`,
          { replace: true },
        );
      setNotice(
        confirmContent
          ? "版本已保存，已提交生效。"
          : parseOnly
            ? `解析版本 v${next.result.revision} 已保存，可查看历史或导出。`
            : `解析版本 v${next.result.revision} 已保存。知识库尚未更新，请按需入库。`,
      );
    } catch (reason: unknown) {
      setError(reason);
    } finally {
      setBusy(null);
    }
  }
  /** 导出服务生成的真实文件，历史版本通过结果摘要显式选择。 */
  async function exportDocument(
    format: "json" | "markdown" | "bundle",
  ): Promise<void> {
    if (!detail || busy) return;
    setBusy("正在导出…");
    setError(null);
    try {
      downloadBlob(
        format === "bundle"
          ? await api.bundle(detail.document_id, detail.result_id)
          : await api.export(detail.document_id, format, detail.result_id),
        `${detail.name.replace(/\.[^.]+$/, "")}.v${detail.result.revision}.${format === "bundle" ? "zip" : format === "json" ? "json" : "md"}`,
      );
    } catch (reason: unknown) {
      setError(reason);
    } finally {
      setBusy(null);
    }
  }
  /** 显式重新读取服务当前状态，提示丢弃冲突的未保存草稿。 */
  async function reloadCurrent(): Promise<void> {
    if (busy || !(await canLeave())) return;
    setError(null);
    setChanges({});
    setStructuralChanges([]);
    setMetadataDirty(false);
    setReviewEpoch((epoch) => epoch + 1);
    if (serviceDirty) {
      setServiceDirty(false);
      setServiceEpoch((epoch) => epoch + 1);
    }
    setBusy("正在刷新…");
    try {
      await refresh();
      if (selectedId) {
        setDetail(await api.document(selectedId));
        setVersions(await api.versions(selectedId));
        setRequestedVersion(undefined);
        // 当前版本和地址同步，避免再次选择同一历史版本时路由没有变化。
        if (routeParams.has("result") || routeParams.has("element"))
          routeNavigate(
            `/knowledge/${knowledgeBase.kb_id}/documents?doc=${selectedId}`,
            { replace: true },
          );
      }
    } catch (reason: unknown) {
      setError(reason);
    } finally {
      setBusy(null);
    }
  }
  /** 关闭列表编辑弹窗时，保护未保存的业务字段。 */
  function closeBusinessFields(): void {
    if (savingBusinessFields) return;
    if (!metadataDirty) {
      setEditingDocument(null);
      return;
    }
    modal.confirm({
      title: "放弃未保存的业务字段？",
      okText: "放弃修改",
      cancelText: "继续编辑",
      onOk: () => setEditingDocument(null),
    });
  }

  /** 保存后只回写当前行的业务字段和版本，保留列表筛选与分页。 */
  function saveCatalogBusinessFields(saved: DocumentSummary): void {
    setDocuments((rows) =>
      rows.map((row) =>
        row.document_id === saved.document_id
          ? { ...row, metadata: saved.metadata, revision: saved.revision }
          : row,
      ),
    );
    setEditingDocument(null);
    setNotice(
      parseOnly
        ? "业务字段已保存。"
        : "业务字段已保存。已上线资料可从该行菜单生效业务字段。",
    );
  }

  /** 复用版本条件选用接口，只对已发布的同一份解析正文提交字段更新。 */
  async function activateBusinessFields(
    document: DocumentListItem,
  ): Promise<void> {
    if (busy || parseOnly || !canEdit || !canActivateBusinessFields(document))
      return;
    setBusy("正在提交业务字段生效…");
    setError(null);
    try {
      await api.selectResult(document, document.result_id, false, true);
      await refresh();
      setNotice(`「${document.name}」已提交业务字段生效，完成后可按新值筛选。`);
    } catch (reason: unknown) {
      setError(reason);
    } finally {
      setBusy(null);
    }
  }

  /** 从列表直接重新清洗，无需先打开详情。 */
  function reparseDocument(document: DocumentListItem): void {
    if (busy || !canEdit) return;
    void api
      .reparse(document.document_id, document.revision)
      .then(() => {
        void refresh().catch(setError);
        setNotice(`「${document.name}」的重新清洗任务已提交。`);
      })
      .catch(setError);
  }
  /** 只合并启用事实，切换时保留尚未保存的审校正文和当前历史版本。 */
  async function setDocumentEnabled(
    document: DocumentSummary,
    enabled: boolean,
  ): Promise<void> {
    if (!canEdit || enablementLocked.current) return;
    enablementLocked.current = true;
    setChangingEnabledId(document.document_id);
    setError(null);
    try {
      const saved = await api.setDocumentEnabled(document.document_id, enabled);
      setDocuments((rows) =>
        rows.map((row) =>
          row.document_id === saved.document_id
            ? { ...row, enabled: saved.enabled }
            : row,
        ),
      );
      setSelectedCurrent((row) =>
        row?.document_id === saved.document_id
          ? { ...row, enabled: saved.enabled }
          : row,
      );
      setDetail((row) =>
        row?.document_id === saved.document_id
          ? { ...row, enabled: saved.enabled }
          : row,
      );
      setNotice(
        saved.enabled
          ? `「${document.name}」已启用。已生效内容恢复参与检索。`
          : `「${document.name}」已禁用，不再参与检索。原件和历史保留。`,
      );
    } catch (reason: unknown) {
      setError(reason);
    } finally {
      enablementLocked.current = false;
      setChangingEnabledId(null);
    }
  }
  /** 从列表直接删除资料，需二次确认并携带版本条件。 */
  function deleteDocument(document: DocumentListItem): void {
    if (busy || !canEdit) return;
    modal.confirm({
      title: "删除这份资料？",
      content: parseOnly
        ? "删除后将无法查看原件、解析历史和资源。"
        : "在线检索将立即排除这份资料，其余资料保持可用。",
      okText: "删除资料",
      cancelText: "保留资料",
      okButtonProps: { danger: true },
      focusable: { autoFocusButton: "cancel" },
      onOk: async () => {
        setBusy("正在删除资料…");
        try {
          await api.remove(document);
          await refresh();
        } catch (reason: unknown) {
          setError(reason);
          throw reason;
        } finally {
          setBusy(null);
        }
      },
    });
  }
  return (
    <div className="app-shell embedded-workbench">
      <Modal
        title="编辑业务字段"
        open={editingDocument !== null}
        width={520}
        footer={null}
        destroyOnHidden
        mask={{ closable: false }}
        closable={!savingBusinessFields}
        keyboard={!savingBusinessFields}
        onCancel={closeBusinessFields}
      >
        {editingDocument && (
          <>
            <p className="muted">{editingDocument.name}</p>
            <MetadataEditor
              key={editingDocument.document_id}
              document={editingDocument}
              api={api}
              parseOnly={parseOnly}
              canEdit={canEdit}
              onDirtyChange={setMetadataDirty}
              onSaved={saveCatalogBusinessFields}
              onSavingChange={setSavingBusinessFields}
            />
          </>
        )}
      </Modal>
      <Modal
        title="保留当前修改？"
        open={blocker.state === "blocked"}
        width={450}
        mask={{ closable: false }}
        onCancel={() => blocker.state === "blocked" && blocker.reset()}
        footer={[
          <Button
            key="keep"
            onClick={() => blocker.state === "blocked" && blocker.reset()}
          >
            继续编辑
          </Button>,
          <Button
            key="leave"
            danger
            type="primary"
            onClick={() => {
              if (blocker.state !== "blocked") return;
              if (serviceDirty) {
                setServiceDirty(false);
                setServiceEpoch((epoch) => epoch + 1);
              }
              blocker.proceed();
            }}
          >
            放弃并离开
          </Button>,
        ]}
      >
        <p>
          当前审校、业务字段或服务草稿尚未保存。继续编辑可保留输入，放弃后才会离开。
        </p>
      </Modal>
      <div className="knowledge-layout">
        <KnowledgeNav
          name={knowledgeBase.name}
          kind={knowledgeBase.kind}
          parseOnly={parseOnly}
          view={page === "upload" ? "documents" : page}
          canEdit={canEdit}
          published={Boolean(knowledgeBase.active_release_id)}
          onChoose={navigate}
          onUpload={() => setPage("upload")}
          onBack={onChooseKnowledgeBase}
        />
        <main
          className={`workspace ${documentOpen ? "document-is-open" : ""} ${focusReview ? "focus-review" : ""}`}
        >
          {page === "upload" ? (
            <UploadWizard
              api={api}
              capabilities={capabilities}
              fields={knowledgeBase.fields}
              onDone={(jobs) => void finishUpload(jobs)}
              onBack={() =>
                routeNavigate(`/knowledge/${knowledgeBase.kb_id}/documents`)
              }
            />
          ) : (
            <>
              <header className="app-header">
                <div className="breadcrumb">
                  <Button
                    type="link"
                    size="small"
                    onClick={onChooseKnowledgeBase}
                  >
                    全部{collectionLabel}
                  </Button>
                  <ChevronRight size={14} />
                  <strong>{knowledgeBase.name}</strong>
                  <Tag>仅本{parseOnly ? "集合" : "库"}</Tag>
                  <ChevronRight size={14} />
                  <span>{pageTitle}</span>
                </div>
                <span className="server-badge">
                  <span className="small-dot green" />
                  {capabilities.storage === "external"
                    ? "外部注入存储"
                    : capabilities.storage}
                </span>
              </header>
              <div className="page-heading">
                <div>
                  <h1>
                    {page === "documents"
                      ? documentOpen
                        ? "资料核对"
                        : knowledgeBase.name
                      : pageTitle}
                  </h1>
                </div>
                {page === "documents" && (
                  <div className="heading-actions">
                    {documentOpen && (
                      <Button
                        type="link"
                        className="catalog-back"
                        onClick={() => navigate("documents")}
                      >
                        ← 返回资料列表
                      </Button>
                    )}
                    <Button
                      type="default"
                      onClick={() => void reloadCurrent()}
                      disabled={Boolean(busy) || loading}
                    >
                      <RefreshCw size={15} />
                      刷新
                    </Button>
                    <Button
                      type="primary"
                      onClick={() => setPage("upload")}
                      disabled={Boolean(busy) || !canEdit}
                    >
                      <Upload size={16} />
                      添加资料
                    </Button>
                  </div>
                )}
              </div>
              {error !== null && (
                <div className="message-banner error-banner" role="alert">
                  <AlertCircle size={18} />
                  <div>
                    <strong>操作未完成</strong>
                    <p>{errorMessage(error)}</p>
                    {error instanceof ApiError && (
                      <small>
                        错误码：{error.code}
                        {error.recordId ? ` · 记录：${error.recordId}` : ""}
                      </small>
                    )}
                    {error instanceof ApiError &&
                      ["version_conflict", "stale_result", "conflict"].includes(
                        error.code,
                      ) && (
                        <Button
                          type="link"
                          size="small"
                          onClick={() => void reloadCurrent()}
                        >
                          刷新到最新版本
                        </Button>
                      )}
                  </div>
                  <Button
                    type="text"
                    size="small"
                    shape="circle"
                    aria-label="关闭错误提示"
                    onClick={() => setError(null)}
                  >
                    <X size={16} />
                  </Button>
                </div>
              )}
              {notice && (
                <div className="message-banner success-banner" role="status">
                  <Check size={17} />
                  <span>{notice}</span>
                  <Button
                    type="text"
                    size="small"
                    shape="circle"
                    aria-label="关闭提示"
                    onClick={() => setNotice("")}
                  >
                    <X size={16} />
                  </Button>
                </div>
              )}
              {page === "search" && !parseOnly && (
                <div
                  className="retrieval-target"
                  role="group"
                  aria-label="检索目标"
                >
                  <Button
                    type={searchTarget === "published" ? "primary" : "default"}
                    onClick={() => setSearchTarget("published")}
                  >
                    线上知识
                  </Button>
                  <Button
                    type={searchTarget === "candidate" ? "primary" : "default"}
                    onClick={() => setSearchTarget("candidate")}
                  >
                    候选验证与对比
                  </Button>
                  <span>线上结果使用当前发布；候选验证不改变线上知识。</span>
                </div>
              )}
              {!parseOnly && (
                /* 同库核对原文后保留问题、结果、质量标签和展开状态；隐藏时暂停副作用。
             切换知识库或明确放弃草稿才销毁上下文，不持久保存查询或正文。 */
                <Activity
                  key={`service:${knowledgeBase.kb_id}:${serviceEpoch}`}
                  mode={
                    studioPage ||
                    (page === "search" && searchTarget === "candidate")
                      ? "visible"
                      : "hidden"
                  }
                >
                  <ServiceStudio
                    view={studioView}
                    onDirtyChange={setServiceDirty}
                    api={api}
                    kbId={knowledgeBase.kb_id}
                    canEdit={canEdit}
                    canAdmin={capabilities.roles.includes("admin")}
                    onOpen={openDocument}
                    onError={setError}
                  />
                </Activity>
              )}
              {!parseOnly && (
                <Activity
                  key={`search:${knowledgeBase.kb_id}`}
                  mode={
                    page === "search" && searchTarget === "published"
                      ? "visible"
                      : "hidden"
                  }
                >
                  <SearchView
                    canAnswer={capabilities.answer.available}
                    onTrace={(trace) =>
                      routeNavigate(
                        `/knowledge/${knowledgeBase.kb_id}/diagnostics?trace=${encodeURIComponent(trace)}`,
                      )
                    }
                    api={api}
                    onOpen={openDocument}
                    onError={setError}
                  />
                </Activity>
              )}
              {(studioPage || page === "search") && !parseOnly ? null : page ===
                "operations" ? (
                <Management
                  api={api}
                  kbId={knowledgeBase.kb_id}
                  canEdit={canEdit}
                  canAdmin={capabilities.roles.includes("admin")}
                  parseOnly={parseOnly}
                  onOpen={openDocument}
                  onError={setError}
                />
              ) : page === "fields" ? (
                <ReviewScroll label="业务字段">
                  {capabilities.roles.includes("admin") ? (
                    <FieldSchema
                      api={api}
                      parseOnly={parseOnly}
                      onError={setError}
                      onDirtyChange={setServiceDirty}
                    />
                  ) : (
                    <section className="surface">
                      <h2>业务字段</h2>
                      <p>由知识库管理员维护字段定义。</p>
                      {knowledgeBase.fields.map((field) => (
                        <p key={field.name}>
                          {field.label} · {field.name} · {field.type}
                        </p>
                      ))}
                    </section>
                  )}
                </ReviewScroll>
              ) : page === "settings" ? (
                <ReviewScroll label="本库设置">
                  <div className="library-settings">
                    {!parseOnly && (
                      <section className="surface">
                        <h2>上传后自动生效</h2>
                        <p>
                          开启后，新上传资料清洗完成即可用于试问。关闭后，可打开资料手动生效。
                        </p>
                        <Switch
                          aria-label="上传后自动生效"
                          checked={autoActivate}
                          loading={settingActivation}
                          disabled={!canEdit}
                          onChange={(enabled) => {
                            setSettingActivation(true);
                            void api
                              .setAutoActivate(enabled)
                              .then((kb) => setAutoActivate(kb.auto_activate))
                              .catch(setError)
                              .finally(() => setSettingActivation(false));
                          }}
                        />
                      </section>
                    )}
                    <Settings capabilities={capabilities} api={api} />
                  </div>
                </ReviewScroll>
              ) : !documentOpen ? (
                <DocumentCatalog
                  documents={documents}
                  counts={counts}
                  state={catalogState}
                  onState={setCatalogState}
                  search={filter}
                  onSearch={setFilter}
                  onOpen={openDocument}
                  parseOnly={parseOnly}
                  fields={knowledgeBase.fields}
                  engines={capabilities.parse_engines}
                  loading={loading}
                  loadingMore={loadingMore}
                  onMore={() => void loadMore()}
                  hasMore={hasMore}
                  canEdit={canEdit}
                  onAdd={() => setPage("upload")}
                  onReparse={reparseDocument}
                  onDelete={deleteDocument}
                  changingEnabledId={changingEnabledId}
                  onEnabledChange={(document, enabled) =>
                    void setDocumentEnabled(document, enabled)
                  }
                  onEditMetadata={setEditingDocument}
                  onActivateBusinessFields={(document) =>
                    void activateBusinessFields(document)
                  }
                />
              ) : (
                <div className="documents-layout catalog-review-layout">
                  <section className="review-panel" aria-label="资料审校">
                    {reading ? (
                      <div className="empty">
                        <LoaderCircle className="spin" size={28} />
                        <h3>正在读取解析结果</h3>
                      </div>
                    ) : selected && !selected.result_id ? (
                      <DocumentRecovery
                        document={selected}
                        api={api}
                        canEdit={canEdit}
                        maxBytes={capabilities.max_upload_bytes}
                        onResolved={() => {
                          void api
                            .documentInfo(selected.document_id)
                            .then(setSelectedCurrent)
                            .catch(setError);
                        }}
                        onTask={() => {
                          void refresh().catch(setError);
                          setNotice("修复任务已提交，可继续等待后台结果。");
                        }}
                      />
                    ) : detail && selected ? (
                      <Review
                        key={`${detail.document_id}:${reviewEpoch}`}
                        visionAvailable={capabilities.vision.available}
                        visionProvider={capabilities.vision.provider}
                        onReparse={() => {
                          void api
                            .reparse(detail.document_id, detail.revision)
                            .then(() => setNotice("原件重解析任务已提交。"))
                            .catch(setError);
                        }}
                        onReplaceSource={(file) => {
                          if (file.size > capabilities.max_upload_bytes) {
                            setError(new Error("文件超过服务上传大小限制"));
                            return;
                          }
                          void api
                            .replaceSource(
                              detail.document_id,
                              detail.revision,
                              file,
                            )
                            .then(() =>
                              setNotice(
                                "原件替换解析任务已提交，成功前保留当前结果。",
                              ),
                            )
                            .catch(setError);
                        }}
                        structuralChanges={structuralChanges}
                        onStructureChange={(operation) =>
                          setStructuralChanges((previous) => [
                            ...previous.filter(
                              (item) =>
                                operationKey(item) !== operationKey(operation),
                            ),
                            operation,
                          ])
                        }
                        loadSourcePreview={(signal) =>
                          api.sourcePreview(
                            detail.document_id,
                            detail.result_id,
                            signal,
                          )
                        }
                        onBundle={() => void exportDocument("bundle")}
                        onMetadataDirtyChange={setMetadataDirty}
                        onMetadataSaved={(saved) => {
                          setDetail((previous) =>
                            previous?.document_id === saved.document_id
                              ? { ...previous, ...saved }
                              : previous,
                          );
                          void refresh().catch(setError);
                          setNotice(
                            parseOnly
                              ? "业务字段已保存。"
                              : "业务字段已保存，可以手动生效。",
                          );
                        }}
                        document={{
                          ...detail,
                          indexed_result_id: selected.indexed_result_id,
                          indexed_revision: selected.indexed_revision,
                        }}
                        enabledChanging={changingEnabledId !== null}
                        onEnabledChange={(enabled) =>
                          void setDocumentEnabled(detail, enabled)
                        }
                        focusedElementRef={focusedElementRef}
                        versions={versions}
                        api={api}
                        changes={changes}
                        latestResultId={selected.result_id}
                        busy={busy}
                        canEdit={canEdit}
                        parseOnly={parseOnly}
                        canIngest={
                          canEdit &&
                          capabilities.mode !== "parse" &&
                          !metadataDirty
                        }
                        activating={
                          "listStatus" in selected &&
                          selected.listStatus === "activating"
                        }
                        onDelete={() => {
                          if (busy || !canEdit) return;
                          modal.confirm({
                            title: "撤销这份资料？",
                            content: parseOnly
                              ? "撤销后将无法通过这份资料查看原文、解析历史和资源。"
                              : "在线检索将立即排除这份资料，其余资料保持可用。",
                            okText: "撤销资料",
                            cancelText: "保留资料",
                            okButtonProps: { danger: true },
                            focusable: { autoFocusButton: "cancel" },
                            onOk: async () => {
                              setBusy("正在撤销资料…");
                              let removed = false;
                              try {
                                await api.remove(detail);
                                removed = true;
                                setSelectedId(null);
                                setDetail(null);
                                await refresh();
                              } catch (reason: unknown) {
                                setError(reason);
                                void message.error(
                                  removed
                                    ? `资料已撤销，列表刷新失败：${errorMessage(reason)}`
                                    : errorMessage(reason),
                                );
                                if (!removed) throw reason;
                              } finally {
                                setBusy(null);
                              }
                            },
                          });
                        }}
                        onChange={changeText}
                        onVersion={(id) => openDocument(detail.document_id, id)}
                        onSave={() => void save(!parseOnly)}
                        focusedReview={focusReview}
                        onToggleFocus={() => setFocusReview((value) => !value)}
                        onExport={(format) => void exportDocument(format)}
                        onError={setError}
                      />
                    ) : (
                      <div className="empty">
                        <h3>{error ? "无法打开这份资料" : "正在读取资料…"}</h3>
                      </div>
                    )}
                  </section>
                </div>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  );
}
