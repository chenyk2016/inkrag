/** 知识库导航按资料、检索质量和发布运行分组，名称与内容路由共用。 */
import { Button, Tag } from "antd";
import {
  ArrowLeft,
  FileStack,
  GitBranch,
  MessageSquareText,
  Settings2,
  Tags,
  Upload,
  SlidersHorizontal,
  ClipboardCheck,
  Activity,
  Plug,
} from "lucide-react";

export type KnowledgePage =
  | "documents"
  | "search"
  | "fields"
  | "operations"
  | "settings"
  | "configuration"
  | "evaluation"
  | "diagnostics"
  | "integrations";

const ITEMS: Array<{
  key: KnowledgePage;
  label: string;
  icon: typeof FileStack;
  group: string;
}> = [
  { key: "documents", label: "资料", icon: FileStack, group: "资料管理" },
  { key: "fields", label: "业务字段", icon: Tags, group: "资料管理" },
  {
    key: "search",
    label: "检索测试",
    icon: MessageSquareText,
    group: "检索与质量",
  },
  {
    key: "configuration",
    label: "检索配置",
    icon: SlidersHorizontal,
    group: "检索与质量",
  },
  {
    key: "evaluation",
    label: "质量评测",
    icon: ClipboardCheck,
    group: "检索与质量",
  },
  {
    key: "operations",
    label: "版本与发布",
    icon: GitBranch,
    group: "发布与运行",
  },
  {
    key: "diagnostics",
    label: "调用与诊断",
    icon: Activity,
    group: "发布与运行",
  },
  {
    key: "integrations",
    label: "接口与调用方",
    icon: Plug,
    group: "发布与运行",
  },
  { key: "settings", label: "本库设置", icon: Settings2, group: "设置" },
];

/** 只读展示当前库身份与发布状态，导航由父级决定，不在这里触碰业务。 */
export function KnowledgeNav({
  name,
  kind,
  parseOnly,
  view,
  canEdit,
  published,
  onChoose,
  onUpload,
  onBack,
}: {
  name: string;
  kind: "knowledge" | "parse";
  parseOnly: boolean;
  view: KnowledgePage;
  canEdit: boolean;
  published: boolean;
  onChoose: (page: KnowledgePage) => void;
  onUpload: () => void;
  onBack: () => void;
}) {
  const items = parseOnly
    ? ITEMS.filter((item) =>
        ["documents", "fields", "operations", "settings"].includes(item.key),
      )
    : ITEMS;
  return (
    <aside className="knowledge-sidebar">
      <div className="knowledge-sidebar-head">
        <Button
          type="text"
          size="small"
          className="knowledge-back"
          icon={<ArrowLeft size={15} />}
          onClick={onBack}
        >
          全部{kind === "parse" ? "集合" : "知识库"}
        </Button>
        <h2>{name}</h2>
        <Tag color={published ? "green" : "default"}>
          {kind === "parse" ? "独立解析" : published ? "已发布" : "尚未发布"}
        </Tag>
      </div>
      <nav aria-label="知识库导航">
        {[...new Set(items.map((item) => item.group))].map((group) => (
          <div className="knowledge-nav-group" key={group}>
            <span className="knowledge-nav-label">{group}</span>
            {items
              .filter((item) => item.group === group)
              .map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  type="button"
                  className={view === key ? "active" : ""}
                  aria-current={view === key ? "page" : undefined}
                  onClick={() => onChoose(key)}
                >
                  <Icon size={17} />
                  {parseOnly && key === "operations" ? "解析任务" : label}
                </button>
              ))}
          </div>
        ))}
      </nav>
      {canEdit && (
        <Button
          type="primary"
          className="knowledge-upload"
          icon={<Upload size={16} />}
          onClick={onUpload}
        >
          上传资料
        </Button>
      )}
    </aside>
  );
}

/** 同一导航定义同时供页标题使用，避免入口与页面名称漂移。 */
export function knowledgePageTitle(page: KnowledgePage): string {
  return ITEMS.find((item) => item.key === page)?.label ?? "知识库";
}
