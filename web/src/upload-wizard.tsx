/** 新增资料统一使用全页四步流程，保留原件暂存、真实预览和确认语义。 */
import type { Capabilities, Job, MetadataField } from "./api";
import type { PlatformClient } from "./platform-api";
import { UploadDialog } from "./upload-dialog";

/** 每次显式下一步只推进一个阶段，最终确认才创建资料。 */
export function UploadWizard({
  api,
  capabilities,
  fields,
  onDone,
  onBack,
}: {
  api: PlatformClient;
  capabilities: Capabilities;
  fields: MetadataField[];
  onDone: (jobs: Job[]) => void;
  onBack: () => void;
}) {
  return (
    <UploadDialog
      files={[]}
      fields={fields}
      engines={capabilities.parse_engines}
      processingCapabilities={capabilities.processing}
      api={api}
      maxBytes={capabilities.max_upload_bytes}
      beforeConfirm={async () => true}
      onCancel={onBack}
      onConfirm={(jobs) => {
        onDone(jobs);
        onBack();
      }}
    />
  );
}
