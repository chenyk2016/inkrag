/** PDF 只渲染真实原件的当前页，页号联动不冒充逐字坐标定位。 */
import { useEffect, useRef, useState } from "react";
import { Button, InputNumber } from "antd";
import { ChevronLeft, ChevronRight, Minus, Plus } from "lucide-react";
import {
  getDocument,
  GlobalWorkerOptions,
  type PDFDocumentProxy,
} from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { SourceLocation } from "./review-types";
import { ReviewScroll } from "./review-scroll";

GlobalWorkerOptions.workerSrc = workerUrl;

/** 切换原件时销毁工作线程；切换页或缩放时取消旧画布任务，避免晚响应污染。 */
export function PdfSource({
  blob,
  page,
  onSelect,
}: {
  blob: Blob;
  page?: number;
  onSelect?: (location: SourceLocation) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [current, setCurrent] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [error, setError] = useState("");
  const [rendering, setRendering] = useState(true);
  useEffect(() => {
    let cancelled = false;
    let dispose: (() => void) | undefined;
    setPdf(null);
    setError("");
    setRendering(true);
    void blob
      .arrayBuffer()
      .then((bytes) => {
        if (cancelled) return;
        const loading = getDocument({ data: new Uint8Array(bytes) });
        dispose = () => {
          void loading.destroy();
        };
        void loading.promise
          .then((document) => {
            if (!cancelled) {
              setPdf(document);
              setCurrent(1);
            }
          })
          .catch(() => {
            if (!cancelled) {
              setError("PDF 原件无法预览，请下载原件查看。");
              setRendering(false);
            }
          });
      })
      .catch(() => {
        if (!cancelled) {
          setError("PDF 原件读取失败，请重试。");
          setRendering(false);
        }
      });
    return () => {
      cancelled = true;
      dispose?.();
    };
  }, [blob]);
  useEffect(() => {
    if (pdf && page) setCurrent(Math.max(1, Math.min(pdf.numPages, page)));
  }, [pdf, page]);
  useEffect(() => {
    if (!pdf || !canvas.current) return;
    let cancelled = false;
    let cancelRender: (() => void) | undefined;
    const target = canvas.current;
    setRendering(true);
    setError("");
    void pdf
      .getPage(current)
      .then((pdfPage) => {
        if (cancelled) return;
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        const viewport = pdfPage.getViewport({ scale: zoom * 1.1 });
        // 限制画布像素面积，超大纸张在可见缩放下仍不会耗尽浏览器内存。
        const pixelScale = Math.min(
          ratio,
          Math.sqrt(16_000_000 / (viewport.width * viewport.height)),
        );
        target.width = Math.floor(viewport.width * pixelScale);
        target.height = Math.floor(viewport.height * pixelScale);
        target.style.width = `${viewport.width}px`;
        target.style.height = `${viewport.height}px`;
        const task = pdfPage.render({
          canvas: target,
          viewport,
          transform:
            pixelScale === 1 ? undefined : [pixelScale, 0, 0, pixelScale, 0, 0],
        });
        cancelRender = () => task.cancel();
        void task.promise
          .then(() => {
            if (!cancelled) setRendering(false);
          })
          .catch(() => {
            if (!cancelled) {
              setError("此页渲染失败，可切换页面重试或下载原件。");
              setRendering(false);
            }
          });
      })
      .catch(() => {
        if (!cancelled) {
          setError("此页读取失败。");
          setRendering(false);
        }
      });
    return () => {
      cancelled = true;
      cancelRender?.();
    };
  }, [pdf, current, zoom]);
  /** 用户翻页与外部定位分开处理，只有主动翻页才回传结果定位。 */
  function selectPage(value: number): void {
    if (!pdf) return;
    const next = Math.max(1, Math.min(pdf.numPages, value));
    setCurrent(next);
    onSelect?.({ page: next });
  }
  return (
    <>
      <div className="review-preview-controls">
        <Button
          size="small"
          type="default"
          aria-label="上一页原件"
          disabled={!pdf || current <= 1}
          onClick={() => selectPage(current - 1)}
        >
          <ChevronLeft size={14} />
        </Button>
        <label>
          第{" "}
          <InputNumber
            size="small"
            aria-label="原件页码"
            min={1}
            max={pdf?.numPages ?? 1}
            value={current}
            disabled={!pdf}
            onChange={(value) => {
              if (
                typeof value === "number" &&
                Number.isInteger(value) &&
                value > 0
              )
                selectPage(value);
            }}
          />{" "}
          / {pdf?.numPages ?? "…"} 页
        </label>
        <Button
          size="small"
          type="default"
          aria-label="下一页原件"
          disabled={!pdf || current >= pdf.numPages}
          onClick={() => selectPage(current + 1)}
        >
          <ChevronRight size={14} />
        </Button>
        <span className="review-control-spacer" />
        <Button
          size="small"
          type="text"
          aria-label="缩小原件"
          disabled={zoom <= 0.5}
          onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))}
        >
          <Minus size={14} />
        </Button>
        <span>{Math.round(zoom * 100)}%</span>
        <Button
          size="small"
          type="text"
          aria-label="放大原件"
          disabled={zoom >= 2}
          onClick={() => setZoom((value) => Math.min(2, value + 0.25))}
        >
          <Plus size={14} />
        </Button>
      </div>
      <div className="review-preview-note" aria-live="polite">
        真实 PDF 原件 · 第 {current} 页{rendering ? " · 正在渲染" : ""} ·
        仅按页关联，未提供精确区域
      </div>
      <ReviewScroll label="PDF 原件独立滚动区域">
        <div className="review-pdf-stage">
          {error && <p role="alert">{error}</p>}
          <canvas ref={canvas} aria-label={`PDF 原件第 ${current} 页`} />
        </div>
      </ReviewScroll>
      <div className="review-preview-note">
        <Button
          size="small"
          type="text"
          disabled={!pdf}
          onClick={() => onSelect?.({ page: current })}
        >
          定位到解析结果的第 {current} 页
        </Button>
      </div>
    </>
  );
}
