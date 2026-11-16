/** 无解析结果时仍能查看持久任务与安全诊断，并在同一资料身份下修复原件。 */
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Tag } from "antd";
import type { DocumentSummary } from "./api";
import type { PlatformClient } from "./platform-api";
import { Failure, statusLabel } from "./application";
import { downloadBlob } from "./assets";
/** 确定性文件错误引导替换，暂时故障才开放原任务重试。 */
export function DocumentRecovery({
  document,
  api,
  canEdit,
  maxBytes,
  onTask,
  onResolved,
}: {
  document: DocumentSummary;
  api: PlatformClient;
  canEdit: boolean;
  maxBytes: number;
  onTask: () => void;
  onResolved: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const tasks = useQuery({
    queryKey: ["document-tasks", document.kb_id, document.document_id],
    queryFn: ({ signal }) =>
      api.taskPage(
        { kb_id: document.kb_id, document_id: document.document_id, limit: 30 },
        signal,
      ),
    refetchInterval: 5000,
  });
  const task = tasks.data?.items.find(
    (item) => item.job.document_id === document.document_id,
  )?.job;
  const reason = task?.error_diagnostic;
  const handled = useRef("");
  useEffect(() => {
    if (task?.status === "succeeded" && handled.current !== task.job_id) {
      handled.current = task.job_id;
      onResolved();
    }
  }, [task?.job_id, task?.status, onResolved]);
  return (
    <section className="recovery-pane">
      <h2>
        {task?.status === "failed"
          ? "解析未完成，可修复后继续"
          : "原件已保存，等待解析结果"}
      </h2>
      {task && (
        <Tag color={task.status === "failed" ? "red" : "blue"}>
          {statusLabel(task.status)} · {task.stage}
        </Tag>
      )}
      {reason && (
        <div role="alert">
          <p>
            {typeof reason.message === "string"
              ? reason.message
              : task?.error_code}
          </p>
          {typeof reason.location === "string" && <p>{reason.location}</p>}
          {typeof reason.suggestion === "string" && <p>{reason.suggestion}</p>}
        </div>
      )}
      <p>任务与原件已持久保存。替换原件后仍沿用此资料，旧版本保持可回查。</p>
      <Button
        type="default"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          void api
            .asset(document.source_asset_id)
            .then((blob) => downloadBlob(blob, document.name))
            .catch(setError)
            .finally(() => setBusy(false));
        }}
      >
        下载已保存原件
      </Button>
      {canEdit && (
        <div className="inline-actions">
          <Button disabled={busy} onClick={() => input.current?.click()}>
            替换原件并解析
          </Button>
          {task && reason?.retryable === true && (
            <Button
              type="default"
              disabled={busy}
              onClick={() => {
                setBusy(true);
                void api
                  .jobAction(task.job_id, "retry")
                  .then(onTask)
                  .catch(setError)
                  .finally(() => setBusy(false));
              }}
            >
              重试暂时故障
            </Button>
          )}
        </div>
      )}
      <input
        ref={input}
        className="sr-only"
        type="file"
        disabled={busy}
        aria-label="替换失败资料原件"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (!file) return;
          if (file.size > maxBytes) {
            setError(new Error("文件超过服务大小限制"));
            return;
          }
          setBusy(true);
          void api
            .replaceSource(document.document_id, document.revision, file)
            .then(onTask)
            .catch(setError)
            .finally(() => setBusy(false));
        }}
      />
      {tasks.isError && <Failure error={tasks.error} />}{" "}
      {error !== null && <Failure error={error} />}
    </section>
  );
}
