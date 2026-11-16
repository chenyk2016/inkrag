/** 原生结构预览保留文本、表格和图片的阅读顺序，不声称恢复 Office 版面。 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Input } from "antd";
import type { Client, NativeTable } from "./api";
import { AssetImage } from "./assets";
import {
  readingOrder,
  sourceLocation,
  type ReviewResult,
  type SourceLocation,
  type StructureOperation,
} from "./review-types";
import { revealInViewport, ReviewScroll } from "./review-scroll";

/** 分组预览表格，保持合并单元格定义；每个坐标修改回传原生行列。 */
export function ReviewTable({
  table,
  editable = false,
  operations = [],
  onChange,
}: {
  table: NativeTable;
  editable?: boolean;
  operations?: readonly StructureOperation[];
  onChange?: (operation: StructureOperation) => void;
}) {
  const [rowGroup, setRowGroup] = useState(0);
  const [columnGroup, setColumnGroup] = useState(0);
  const firstRow = rowGroup * 50;
  const firstColumn = columnGroup * 12;
  const lastRow = Math.min(firstRow + 50, table.rows);
  const lastColumn = Math.min(firstColumn + 12, table.cols);
  const visibleCells = table.cells.filter(
    (cell) =>
      cell.row < lastRow &&
      cell.row + cell.rowSpan > firstRow &&
      cell.col < lastColumn &&
      cell.col + cell.colSpan > firstColumn,
  );
  return (
    <div className="review-table-wrap">
      <div
        className="review-table-scroll"
        tabIndex={0}
        role="region"
        aria-label={`表格，${table.rows} 行 ${table.cols} 列`}
      >
        <table className="review-native-table">
          <tbody>
            {Array.from(
              { length: lastRow - firstRow },
              (_, index) => firstRow + index,
            ).map((row) => (
              <tr key={row}>
                {visibleCells
                  .filter((cell) => Math.max(firstRow, cell.row) === row)
                  .sort((a, b) => a.col - b.col)
                  .map((cell) => {
                    const pending = operations.find(
                      (operation) =>
                        operation.kind === "table_cell" &&
                        operation.element_ref === table.self_ref &&
                        operation.row === cell.row &&
                        operation.column === cell.col,
                    );
                    const value =
                      pending && "text" in pending ? pending.text : cell.text;
                    const content = editable ? (
                      <Input.TextArea
                        aria-label={`第 ${cell.row + 1} 行，第 ${cell.col + 1} 列`}
                        rows={2}
                        value={value}
                        onChange={(event) =>
                          onChange?.({
                            kind: "table_cell",
                            element_ref: table.self_ref,
                            row: cell.row,
                            column: cell.col,
                            text: event.target.value,
                          })
                        }
                      />
                    ) : (
                      value
                    );
                    const spans = {
                      rowSpan:
                        Math.min(cell.row + cell.rowSpan, lastRow) -
                        Math.max(cell.row, firstRow),
                      colSpan:
                        Math.min(cell.col + cell.colSpan, lastColumn) -
                        Math.max(cell.col, firstColumn),
                    };
                    return cell.header ? (
                      <th key={`${cell.row}:${cell.col}`} {...spans}>
                        {content}
                      </th>
                    ) : (
                      <td key={`${cell.row}:${cell.col}`} {...spans}>
                        {content}
                      </td>
                    );
                  })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(table.rows > 50 || table.cols > 12) && (
        <div className="review-table-pagination">
          <Button
            size="small"
            type="default"
            disabled={rowGroup === 0}
            onClick={() => setRowGroup((value) => value - 1)}
          >
            前 50 行
          </Button>
          <span>
            行 {firstRow + 1}–{lastRow} / {table.rows}
          </span>
          <Button
            size="small"
            type="default"
            disabled={lastRow >= table.rows}
            onClick={() => setRowGroup((value) => value + 1)}
          >
            后 50 行
          </Button>
          <Button
            size="small"
            type="default"
            disabled={columnGroup === 0}
            onClick={() => setColumnGroup((value) => value - 1)}
          >
            前 12 列
          </Button>
          <span>
            列 {firstColumn + 1}–{lastColumn} / {table.cols}
          </span>
          <Button
            size="small"
            type="default"
            disabled={lastColumn >= table.cols}
            onClick={() => setColumnGroup((value) => value + 1)}
          >
            后 12 列
          </Button>
          <small>跨组的合并单元格保留原始坐标，修改后同步所有展示位置。</small>
        </div>
      )}
    </div>
  );
}

/** 原生只读来源按引用定位；分组避免超长文档一次挂载全部内容。 */
export function SourceStructure({
  result,
  api,
  focusedElementRef,
  selection,
  onSelect,
}: {
  result: ReviewResult;
  api: Client;
  focusedElementRef?: string;
  selection?: number;
  onSelect?: (location: SourceLocation) => void;
}) {
  const order = useMemo(() => readingOrder(result), [result]);
  const viewport = useRef<HTMLDivElement>(null);
  const [group, setGroup] = useState(0);
  useEffect(() => {
    const index = order.findIndex(
      (item) => item.self_ref === focusedElementRef,
    );
    if (index >= 0) setGroup(Math.floor(index / 40));
  }, [focusedElementRef, order]);
  useEffect(() => {
    if (!focusedElementRef || !viewport.current) return;
    const target = Array.from(
      viewport.current.querySelectorAll<HTMLElement>("[data-source-ref]"),
    ).find((node) => node.dataset.sourceRef === focusedElementRef);
    if (target) revealInViewport(viewport.current, target);
  }, [focusedElementRef, group, result.result_id, selection]);
  return (
    <>
      <ReviewScroll label="原文结构独立滚动区域" viewportRef={viewport}>
        <div className="review-source-structure">
          {order.slice(group * 40, (group + 1) * 40).map((item) => {
            const text = result.texts.find(
              (value) => value.self_ref === item.self_ref,
            );
            const table = result.tables.find(
              (value) => value.self_ref === item.self_ref,
            );
            const picture = result.pictures?.find(
              (value) => value.self_ref === item.self_ref,
            );
            const asset = result.assets.find(
              (value) => value.element_ref === item.self_ref,
            );
            return (
              <div
                key={item.self_ref}
                data-source-ref={item.self_ref}
                className={`review-source-block ${focusedElementRef === item.self_ref ? "is-focused" : ""}`}
              >
                <Button
                  size="small"
                  type="text"
                  onClick={() => onSelect?.(sourceLocation(result, item))}
                >
                  定位到解析结果
                  {item.page ? ` · 第 ${item.page} 页` : " · 原文段落"}
                </Button>
                {text && (
                  <p
                    className={
                      item.label === "section_header" || item.label === "title"
                        ? "review-source-heading"
                        : ""
                    }
                  >
                    {text.text}
                  </p>
                )}
                {table && <ReviewTable table={table} />}
                {picture && (
                  <figure>
                    {asset && <AssetImage asset={asset} api={api} />}
                    <figcaption>
                      {picture.caption || "原文图片（没有文字说明）"}
                    </figcaption>
                  </figure>
                )}
              </div>
            );
          })}
        </div>
      </ReviewScroll>
      {order.length > 40 && (
        <div className="review-pagination">
          <Button
            size="small"
            type="default"
            disabled={group === 0}
            onClick={() => setGroup((value) => value - 1)}
          >
            上一组
          </Button>
          <span>
            {group + 1} / {Math.ceil(order.length / 40)}
          </span>
          <Button
            size="small"
            type="default"
            disabled={(group + 1) * 40 >= order.length}
            onClick={() => setGroup((value) => value + 1)}
          >
            下一组
          </Button>
        </div>
      )}
    </>
  );
}
