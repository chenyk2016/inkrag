/** 发布前审阅被冻结的真实范围，并以同次预览的线上指针做条件发布。 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { App, Button, Modal, Table, Tag } from "antd";
import {
  array,
  number,
  object,
  string,
  type Client,
  type JsonObject,
} from "./api";
import { Failure } from "./application";
interface Preview {
  publishable: boolean;
  stale_reasons: string[];
  expected_release_id: string | null;
  summary: JsonObject;
  service_config: JsonObject;
  strategy_changed: boolean;
  items: {
    document_id: string;
    name: string;
    result_id: string;
    revision: number;
    change: string;
  }[];
  total: number;
  offset: number;
  limit: number;
}
/** 外部预览严格解码，缺失清单时不能继续发布。 */
function preview(value: unknown): Preview {
  const r = object(value);
  return {
    publishable: r.publishable === true,
    stale_reasons: array(r.stale_reasons).map(string),
    expected_release_id:
      r.expected_release_id === null ? null : string(r.expected_release_id),
    summary: object(r.summary),
    service_config: object(r.service_config),
    strategy_changed: r.strategy_changed === true,
    items: array(r.items).map((value) => {
      const row = object(value);
      return {
        document_id: string(row.document_id),
        name: string(row.name),
        result_id: string(row.result_id),
        revision: number(row.revision),
        change: string(row.change),
      };
    }),
    total: number(r.total),
    offset: number(r.offset),
    limit: number(r.limit),
  };
}
/** 资料变化与服务策略分开展示；操作失败保留可重试的预览而不伪造发布状态。 */
export function PublicationDialog({
  api,
  buildId,
  disabled,
  onPublished,
  onDismiss,
  checks,
}: {
  api: Client;
  buildId: string;
  disabled: boolean;
  onPublished: () => Promise<void>;
  onDismiss?: () => void;
  checks?: string;
}) {
  const { message } = App.useApp();
  const [open, setOpen] = useState(Boolean(onDismiss)),
    [offset, setOffset] = useState(0),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  /** 默认更新的外层保留候选身份；专业入口使用内部弹窗状态。 */
  function close() {
    setOpen(false);
    onDismiss?.();
  }
  const data = useQuery({
    queryKey: ["publication-preview", buildId, offset],
    queryFn: async ({ signal }) =>
      preview(
        await (
          await api.request(
            `/builds/${buildId}/preview?limit=30&offset=${offset}`,
            { signal },
          )
        ).json(),
      ),
    enabled: open,
    staleTime: 0,
  });
  return (
    <>
      {!onDismiss && (
        <Button disabled={disabled} onClick={() => setOpen(true)}>
          审阅并发布
        </Button>
      )}
      <Modal
        centered
        destroyOnHidden
        open={open}
        title={onDismiss ? "确认这次更新" : "审阅上线范围"}
        width={760}
        className="publication-dialog"
        footer={null}
        mask={{ closable: false }}
        keyboard={false}
        closable={!busy}
        onCancel={() => {
          if (!busy) close();
        }}
      >
        {onDismiss ? (
          <p>确认后，本次变更会用于线上试问。</p>
        ) : (
          <p>
            候选 {buildId.slice(0, 12)}
            。确认后切换在线检索，未选用资料不会进入此清单。
          </p>
        )}
        {checks && <p role="status">{checks}</p>}
        {data.isPending ? (
          <p role="status">正在读取冻结清单…</p>
        ) : data.isError ? (
          <Failure error={data.error} retry={() => void data.refetch()} />
        ) : (
          <>
            {!data.data.publishable && (
              <Failure
                error={
                  new Error(
                    "候选不可发布，请重新构建或检查资料与授权变化。" +
                      data.data.stale_reasons.join("、"),
                  )
                }
              />
            )}
            <div className="publication-summary">
              {[
                ["added", "新增"],
                ["changed", "更新"],
                ["removed", "移除"],
                ["unchanged", "不变"],
              ].map(([key, label]) => (
                <Tag color={key === "removed" ? "red" : "blue"} key={key}>
                  {label} {String(data.data.summary[key] ?? "—")}
                </Tag>
              ))}
            </div>
            <p>
              当前线上：
              {data.data.expected_release_id?.slice(0, 12) ?? "尚未发布"} · 策略
              {data.data.strategy_changed ? "有变化" : "无变化"}
            </p>
            {(!onDismiss || data.data.strategy_changed) && (
              <details>
                <summary>本次冻结的服务策略</summary>
                <pre>{JSON.stringify(data.data.service_config, null, 2)}</pre>
              </details>
            )}
            <div
              className="publication-manifest"
              role="region"
              aria-label="上线资料清单"
              tabIndex={0}
            >
              <Table<Preview["items"][number]>
                size="small"
                rowKey="document_id"
                pagination={false}
                dataSource={data.data.items}
                columns={[
                  {
                    title: "变更",
                    dataIndex: "change",
                    render: (change: string) =>
                      ({
                        added: "新增",
                        changed: "更新",
                        removed: "移除",
                        unchanged: "不变",
                      })[change] ?? change,
                  },
                  {
                    title: "资料",
                    key: "document",
                    render: (_value: unknown, item) => (
                      <>
                        {item.name}
                        <small>{item.document_id.slice(0, 12)}</small>
                      </>
                    ),
                  },
                  ...(!onDismiss
                    ? [
                        {
                          title: "解析结果身份",
                          key: "result",
                          render: (
                            _value: unknown,
                            item: Preview["items"][number],
                          ) => (
                            <>
                              <small>资料快照 r{item.revision}</small>
                              <code>{item.result_id.slice(0, 16)}</code>
                            </>
                          ),
                        },
                      ]
                    : []),
                ]}
              />
            </div>
            <div className="pagination">
              <span>共 {data.data.total} 份</span>
              <Button
                disabled={!offset}
                onClick={() => setOffset(Math.max(0, offset - 30))}
              >
                上一页
              </Button>
              <Button
                disabled={offset + data.data.items.length >= data.data.total}
                onClick={() => setOffset(offset + 30)}
              >
                下一页
              </Button>
            </div>
            {error !== null && <Failure error={error} />}
            <div className="dialog-actions">
              <Button disabled={busy} onClick={close}>
                {onDismiss ? "稍后更新" : "返回验证"}
              </Button>
              <Button
                type="primary"
                loading={busy}
                disabled={
                  !data.data.publishable ||
                  data.isFetching ||
                  Boolean(
                    onDismiss &&
                      !data.data.strategy_changed &&
                      ["added", "changed", "removed"].every(
                        (key) => data.data.summary[key] === 0,
                      ),
                  )
                }
                onClick={() => {
                  setBusy(true);
                  setError(null);
                  void api
                    .publish(buildId, data.data.expected_release_id)
                    .then(async () => {
                      close();
                      try {
                        await onPublished();
                      } catch (reason: unknown) {
                        void message.error(
                          `服务已发布，页面刷新失败：${reason instanceof Error ? reason.message : "请刷新查看当前线上版本。"}`,
                        );
                      }
                    })
                    .catch(setError)
                    .finally(() => setBusy(false));
                }}
              >
                {onDismiss ? "确认生效" : "确认切换在线服务"}
              </Button>
            </div>
          </>
        )}
      </Modal>
    </>
  );
}
