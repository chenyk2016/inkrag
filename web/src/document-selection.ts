/** 选用冻结解析版本和业务属性，聚合修订号本身不表示属性是否过期。 */
import type { DocumentSummary } from "./api";

/** 按字段和值比较标量属性，避免字段顺序及审核修订造成误报。 */
export function selectionMetadataCurrent(document: DocumentSummary): boolean {
  const frozen = document.selected_metadata;
  if (frozen === null) return false;
  const keys = Object.keys(document.metadata);
  return (
    keys.length === Object.keys(frozen).length &&
    keys.every(
      (key) =>
        Object.hasOwn(frozen, key) &&
        Object.is(frozen[key], document.metadata[key]),
    )
  );
}

/** 下一次构建是否已明确选用了当前解析和当前属性。 */
export function selectionCurrent(document: DocumentSummary): boolean {
  return (
    Boolean(document.result_id) &&
    document.selected_result_id === document.result_id &&
    selectionMetadataCurrent(document)
  );
}
