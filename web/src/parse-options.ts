/** 解析工具选项只来自服务能力；OCR作为所选工具的参数单独展示。 */
import type { ParseEngine } from "./api";

export interface ParseOption {
  /** 稳定键；解析工具对应后端 ParserEngine，扩展能力用独立键。 */
  key: string;
  /** 展示名称。 */
  label: string;
  /** 一句话说明用途，供选择项解释。 */
  description: string;
  /** 该方式对应的后端解析引擎；纯前端占位能力为 null。 */
  engine: string | null;
  /** 是否已经开放可选用；false 表示占位，界面置灰并说明原因。 */
  released: boolean;
  /** 未开放时的说明。 */
  reason: string;
  /** 适用格式名（语义格式名，与 capabilities.parse_engines 一致）；空表示随引擎决定。 */
  formats: string[];
}

/** 把一个解析工具映射成可选择项；不可用时说明原因而非静默隐藏。 */
export function engineOption(
  engine: ParseEngine,
  supportsFile: boolean,
): ParseOption & { applicable: boolean } {
  const reason = !engine.available
    ? (engine.reason ?? "该解析工具当前不可用。")
    : supportsFile
      ? ""
      : "该工具不支持此文件类型。";
  return {
    key: engine.id,
    label: engine.name,
    description: engine.formats.length
      ? `支持：${engine.formats.join("、")}`
      : "",
    engine: engine.id,
    released: engine.available && supportsFile,
    reason,
    formats: engine.formats,
    applicable: supportsFile,
  };
}

/** 对单个文件汇总可选解析方式：不支持项保留原因并置灰。 */
export function parseOptionsFor(
  file: File,
  engines: ParseEngine[],
  supports: (engine: ParseEngine, file: File) => boolean,
): Array<ParseOption & { applicable: boolean }> {
  const tools = engines.map((engine) =>
    engineOption(engine, supports(engine, file)),
  );
  return tools;
}
