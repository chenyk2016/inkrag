/** 审校交互只传原生引用；资产、版本和权限仍由服务端决定。 */
import type { ParsedResult } from "./api";

export type StructureOperation =
  | { kind: "text"; element_ref: string; text: string }
  | {
      kind: "table_cell";
      element_ref: string;
      row: number;
      column: number;
      text: string;
    }
  | {
      kind: "picture_caption" | "picture_description";
      element_ref: string;
      text: string;
    }
  | { kind: "exclude"; element_ref: string; excluded: boolean }
  | { kind: "append_text"; text: string; label: "text" | "section_header" };

export interface SourceLocation {
  /** 仅用于重复点击同一位置时重新定位，不进入持久化协议。 */
  selection?: number;
  elementRef?: string;
  locator?: string;
  page?: number;
}
export interface ReviewOrderItem {
  self_ref: string;
  kind: "text" | "table" | "picture";
  label: string;
  page: number | null;
}
export interface ReviewPicture {
  self_ref: string;
  caption: string;
  page: number | null;
}
/** 结果结构统一由 HTTP 协议解码；审校不维护另一套数据模型。 */
export type ReviewResult = ParsedResult;

/** 新结果使用解析器给出的原生阅读顺序；旧结果保留内容并提示顺序未提供。 */
export function readingOrder(result: ReviewResult): ReviewOrderItem[] {
  if (result.order?.length) return result.order;
  return [
    ...result.texts.map(
      (item): ReviewOrderItem => ({
        self_ref: item.self_ref,
        kind: "text",
        label: item.label ?? "text",
        page: item.page,
      }),
    ),
    ...result.tables.map(
      (item): ReviewOrderItem => ({
        self_ref: item.self_ref,
        kind: "table",
        label: "table",
        page: item.page ?? null,
      }),
    ),
    ...(result.pictures ?? []).map(
      (item): ReviewOrderItem => ({
        self_ref: item.self_ref,
        kind: "picture",
        label: "picture",
        page: item.page,
      }),
    ),
  ];
}

/** 联动只定位解析器真实提供的页、段或路径，不从字符串猜造精确框。 */
export function sourceLocation(
  result: ReviewResult,
  item: ReviewOrderItem,
): SourceLocation {
  const origin = result.origins?.find(
    (value) => value.element_ref === item.self_ref,
  );
  return {
    elementRef: item.self_ref,
    locator: origin?.locator,
    page: item.page ?? undefined,
  };
}

/** 每个可编辑字段有稳定键，父层可合并多次修改而不重复提交。 */
export function operationKey(operation: StructureOperation): string {
  if (operation.kind === "append_text") return "append_text";
  return operation.kind === "table_cell"
    ? `${operation.kind}:${operation.element_ref}:${operation.row}:${operation.column}`
    : `${operation.kind}:${operation.element_ref}`;
}
