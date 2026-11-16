/** 资料检索开关只展示服务确认的状态，不以乐观切换冒充成功。 */
import { Switch, Tooltip } from "antd";

/** 开关控制整份资料的检索资格，原件和历史版本保持可查。 */
export function DocumentEnablement({
  name,
  enabled,
  loading,
  disabled,
  onChange,
}: {
  name: string;
  enabled: boolean;
  loading: boolean;
  disabled: boolean;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <Tooltip title="禁用后立即停止参与检索，保留原件和历史。重新启用后，已生效的内容恢复检索；未生效的内容仍需先完成生效。">
      <span className="document-enablement">
        <Switch
          size="small"
          aria-label={`检索启用：${name}`}
          checked={enabled}
          loading={loading}
          disabled={disabled}
          onChange={onChange}
        />
        <span>{enabled ? "启用" : "禁用"}</span>
      </span>
    </Tooltip>
  );
}
