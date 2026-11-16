/** 知识库目录承载多维核对：属性、状态、解析方式与版本，次要操作收进「…」。 */
import { Button, Dropdown, Input, Tag } from "antd";
import { ChevronRight, Ellipsis, Search } from "lucide-react";
import type { DocumentListItem, DocumentListStatus } from "./platform-api";
import type { MetadataField, ParseEngine } from "./api";
import { selectionMetadataCurrent } from "./document-selection";
import { ReviewScroll } from "./review-scroll";
import "./document-catalog.css";
import { DocumentEnablement } from "./document-enablement";

const labels: Record<DocumentListStatus, string> = {
  review: "未生效",
  ready: "未生效",
  activating: "正在生效",
  failed: "需要处理",
  processing: "正在处理",
  live: "使用中",
  saved: "已保存",
};
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

/** 已上线的解析正文可以单独更新业务字段，未核对的新正文仍走资料核对。 */
export function canActivateBusinessFields(document: DocumentListItem): boolean {
  return Boolean(
    document.result_id &&
      document.indexed_result_id === document.result_id &&
      document.listStatus !== "processing" &&
      document.listStatus !== "activating" &&
      (!selectionMetadataCurrent(document) ||
        (document.listStatus === "failed" &&
          document.selected_result_id === document.result_id &&
          document.indexed_revision !== document.selected_revision)),
  );
}

/** 当前资料的处理状态与线上内容的实际指针分别展示。 */
function online(document: DocumentListItem): string {
  if (!document.enabled) return "已禁用，不参与检索";
  if (!document.indexed_result_id) return "尚未生效";
  if (canActivateBusinessFields(document)) return "业务字段待生效";
  if (
    document.indexed_result_id !== document.result_id ||
    (document.listStatus === "ready" &&
      document.indexed_revision !== document.selected_revision)
  )
    return "仍在用旧内容";
  return "已供业务使用";
}

/** 把后端属性值转成可读文本，布尔用是/否，缺失不显示。 */
function attributeText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

/** 按字段声明取前几条属性，未知字段也保留原名，方便核对。 */
function attributes(
  document: DocumentListItem,
  fields: MetadataField[],
): Array<{ label: string; value: string }> {
  const labelByKey = new Map(fields.map((field) => [field.name, field.label]));
  return Object.entries(document.metadata)
    .map(([key, value]) => ({
      label: labelByKey.get(key) ?? key,
      value: attributeText(value),
    }))
    .filter((item) => item.value !== "");
}

export function DocumentCatalog({
  documents,
  counts,
  state,
  onState,
  search,
  onSearch,
  onOpen,
  parseOnly,
  fields,
  engines,
  loading,
  loadingMore,
  onMore,
  hasMore,
  canEdit,
  onAdd,
  onReparse,
  onDelete,
  onEditMetadata,
  onActivateBusinessFields,
  onEnabledChange,
  changingEnabledId,
}: {
  documents: DocumentListItem[];
  counts: Partial<Record<DocumentListStatus, number>>;
  state: DocumentListStatus | undefined;
  onState: (value: DocumentListStatus | undefined) => void;
  search: string;
  onSearch: (value: string) => void;
  onOpen: (id: string) => void;
  parseOnly: boolean;
  fields: MetadataField[];
  engines: ParseEngine[];
  loading: boolean;
  loadingMore: boolean;
  onMore: () => void;
  hasMore: boolean;
  canEdit: boolean;
  onAdd: () => void;
  onReparse: (document: DocumentListItem) => void;
  onDelete: (document: DocumentListItem) => void;
  onEditMetadata: (document: DocumentListItem) => void;
  onActivateBusinessFields: (document: DocumentListItem) => void;
  onEnabledChange: (document: DocumentListItem, enabled: boolean) => void;
  changingEnabledId: string | null;
}) {
  const total = Object.values(counts).reduce((sum, count) => sum + count, 0);
  const filters = [
    { key: undefined, label: "全部资料", count: total },
    { key: "failed" as const, label: "需处理", count: counts.failed ?? 0 },
  ];
  const engineName = (id: string): string =>
    engines.find((engine) => engine.id === id)?.name ?? id;
  return (
    <ReviewScroll label="资料列表" className="catalog-scroll">
      <div className="catalog">
        <div className="catalog-toolbar">
          <div className="catalog-filters" aria-label="资料状态筛选">
            {filters.map(({ key, label, count }) => (
              <Button
                key={key ?? "all"}
                type={state === key ? "default" : "text"}
                aria-pressed={state === key}
                onClick={() => onState(key)}
              >
                {label} <span>{count}</span>
              </Button>
            ))}
          </div>
          <Input
            allowClear
            aria-label="搜索资料名称"
            placeholder="搜索资料名称"
            prefix={<Search size={16} aria-hidden />}
            value={search}
            maxLength={255}
            onChange={(event) => onSearch(event.target.value)}
          />
        </div>
        {loading ? (
          <p role="status">正在读取资料…</p>
        ) : documents.length > 0 ? (
          <div className="catalog-table-wrap">
            <table className="catalog-table" aria-label="资料">
              <thead>
                <tr>
                  <th scope="col">资料</th>
                  <th scope="col">业务字段</th>
                  <th scope="col">处理状态</th>
                  {!parseOnly && <th scope="col">检索启用</th>}
                  <th scope="col">解析方式</th>
                  <th scope="col">版本</th>
                  <th scope="col" className="catalog-time">
                    最近活动
                  </th>
                  <th scope="col">
                    <span className="sr-only">操作</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {documents.map((document) => {
                  const extension =
                    /\.([a-z\d]{1,8})$/i
                      .exec(document.name)?.[1]
                      ?.toUpperCase() ?? "文件";
                  const action =
                    document.listStatus === "review"
                      ? "核对"
                      : document.listStatus === "failed"
                        ? "处理"
                        : "查看";
                  const attrs = attributes(document, fields);
                  return (
                    <tr key={document.document_id}>
                      <th scope="row">
                        <div className="catalog-file">
                          <span aria-hidden className="catalog-filetype">
                            {extension}
                          </span>
                          <div>
                            <strong>{document.name}</strong>
                            {!parseOnly && <small>{online(document)}</small>}
                          </div>
                        </div>
                      </th>
                      <td className="catalog-attributes">
                        {attrs.length ? (
                          <div className="catalog-attr-list">
                            {attrs.slice(0, 3).map((attr) => (
                              <span key={attr.label} className="catalog-attr">
                                <em>{attr.label}</em>
                                {attr.value}
                              </span>
                            ))}
                            {attrs.length > 3 && <Tag>+{attrs.length - 3}</Tag>}
                          </div>
                        ) : (
                          <span className="catalog-muted">—</span>
                        )}
                      </td>
                      <td>
                        <span
                          className={`catalog-status catalog-${document.listStatus}`}
                        >
                          {!parseOnly &&
                          !document.enabled &&
                          document.listStatus === "live"
                            ? "已生效"
                            : labels[document.listStatus]}
                        </span>
                      </td>
                      {!parseOnly && (
                        <td>
                          <DocumentEnablement
                            name={document.name}
                            enabled={document.enabled}
                            loading={changingEnabledId === document.document_id}
                            disabled={!canEdit || changingEnabledId !== null}
                            onChange={(enabled) =>
                              onEnabledChange(document, enabled)
                            }
                          />
                        </td>
                      )}
                      <td>
                        <span className="catalog-engine">
                          {engineName(document.parser_engine)}
                        </span>
                      </td>
                      <td>
                        <span className="catalog-version">
                          V{document.revision}
                        </span>
                        {document.indexed_revision != null &&
                          document.indexed_revision !== document.revision && (
                            <small className="catalog-online-note">
                              线上 V{document.indexed_revision}
                            </small>
                          )}
                      </td>
                      <td className="catalog-time">
                        <time dateTime={document.activityAt}>
                          {dateFormat.format(new Date(document.activityAt))}
                        </time>
                      </td>
                      <td className="catalog-actions">
                        <Button
                          type="text"
                          aria-label={`${action} ${document.name}`}
                          onClick={() => onOpen(document.document_id)}
                        >
                          {action}
                          <ChevronRight size={14} aria-hidden />
                        </Button>
                        <Dropdown
                          trigger={["click"]}
                          menu={{
                            items: [
                              {
                                key: "metadata",
                                label: "编辑业务字段",
                                disabled:
                                  !canEdit ||
                                  ["processing", "activating"].includes(
                                    document.listStatus,
                                  ),
                              },
                              ...(!parseOnly &&
                              canActivateBusinessFields(document)
                                ? [
                                    {
                                      key: "activateFields",
                                      label: "生效业务字段",
                                      disabled: !canEdit,
                                    },
                                  ]
                                : []),
                              {
                                key: "reparse",
                                label: "重新清洗",
                                disabled: !canEdit,
                              },
                              {
                                key: "delete",
                                label: "删除",
                                danger: true,
                                disabled: !canEdit,
                              },
                            ],
                            onClick: ({ key }) => {
                              if (key === "metadata") onEditMetadata(document);
                              else if (key === "activateFields")
                                onActivateBusinessFields(document);
                              else if (key === "reparse") onReparse(document);
                              else if (key === "delete") onDelete(document);
                            },
                          }}
                        >
                          <Button
                            type="text"
                            shape="circle"
                            aria-label={`更多操作：${document.name}`}
                            icon={<Ellipsis size={16} />}
                          />
                        </Dropdown>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {hasMore && (
              <div className="catalog-more">
                <Button loading={loadingMore} onClick={onMore}>
                  加载更多资料
                </Button>
              </div>
            )}
          </div>
        ) : (
          <div className="catalog-empty">
            <h2>{search || state ? "没有匹配的资料" : "添加第一份资料"}</h2>
            {search || state ? (
              <Button
                onClick={() => {
                  onState(undefined);
                  onSearch("");
                }}
              >
                清除筛选
              </Button>
            ) : (
              canEdit && (
                <Button type="primary" onClick={onAdd}>
                  添加资料
                </Button>
              )
            )}
          </div>
        )}
      </div>
    </ReviewScroll>
  );
}
