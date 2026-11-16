/** 受保护资源的浏览器视图：获取后使用临时 Blob URL，离开即释放。 */
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Button, Segmented } from "antd";
import { ReviewScroll } from "./review-scroll";
import { SourceStructure } from "./review-structure";
import type { ReviewResult, SourceLocation } from "./review-types";
const HtmlSource = lazy(() =>
  import("./review-html").then((module) => ({ default: module.HtmlSource })),
);
const PdfSource = lazy(() =>
  import("./review-pdf").then((module) => ({ default: module.PdfSource })),
);
import { Download, FileText, ImageOff, LoaderCircle } from "lucide-react";
import type { Asset, Client, ParsedResult } from "./api";

/** 生成下载动作并及时回收临时对象地址。 */
export function downloadBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 只从已知位图签名选择内联类型，拒绝 SVG 或 HTML 等主动内容。 */
async function bitmapType(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.slice(0, 16).arrayBuffer());
  if (
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47
  )
    return "image/png";
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff)
    return "image/jpeg";
  if (bytes[0] === 0x47 && bytes[1] === 0x49 && bytes[2] === 0x46)
    return "image/gif";
  if (bytes[0] === 0x42 && bytes[1] === 0x4d) return "image/bmp";
  if (
    (bytes[0] === 0x49 &&
      bytes[1] === 0x49 &&
      bytes[2] === 0x2a &&
      bytes[3] === 0) ||
    (bytes[0] === 0x4d &&
      bytes[1] === 0x4d &&
      bytes[2] === 0 &&
      bytes[3] === 0x2a)
  )
    return "image/tiff";
  if (
    String.fromCharCode(...bytes.slice(0, 4)) === "RIFF" &&
    String.fromCharCode(...bytes.slice(8, 12)) === "WEBP"
  )
    return "image/webp";
  throw new Error("此资源不是支持的安全位图");
}

/** 接近可见区域时才读取图片，避免大文档同时下载全部附件。 */
export function AssetImage({ asset, api }: { asset: Asset; api: Client }) {
  const anchor = useRef<HTMLElement>(null);
  const [visible, setVisible] = useState(false);
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const [extension, setExtension] = useState("png");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const element = anchor.current;
    if (!element) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "160px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    let temporary: string | null = null;
    setUrl(null);
    setError(false);
    api
      .asset(asset.asset_id, controller.signal)
      .then(async (blob) => {
        const mediaType = await bitmapType(blob);
        setExtension(
          mediaType === "image/jpeg" ? "jpg" : mediaType.split("/")[1],
        );
        if (controller.signal.aborted) return;
        temporary = URL.createObjectURL(new Blob([blob], { type: mediaType }));
        setUrl(temporary);
      })
      .catch((reason: unknown) => {
        if (
          !controller.signal.aborted &&
          !(reason instanceof DOMException && reason.name === "AbortError")
        )
          setError(true);
      });
    return () => {
      controller.abort();
      if (temporary) URL.revokeObjectURL(temporary);
    };
  }, [api, asset.asset_id, visible, retry]);
  return (
    <figure className="asset-card" ref={anchor}>
      <div className="asset-image">
        {error ? (
          <span className="muted">
            <ImageOff size={20} /> 图片读取失败
            <Button
              type="link"
              size="small"
              onClick={() => setRetry((value) => value + 1)}
            >
              重试读取
            </Button>
          </span>
        ) : url ? (
          <img
            src={url}
            alt={`文档图片${asset.page ? `，第 ${asset.page} 页` : ""}`}
            loading="lazy"
          />
        ) : (
          <LoaderCircle className="spin" size={20} aria-label="读取图片" />
        )}
      </div>
      <figcaption>
        <span>{asset.page ? `第 ${asset.page} 页` : "文档资源"}</span>
        <code>{asset.element_ref}</code>
        {url && (
          <a
            className="icon-button"
            href={url}
            download={`image-${asset.asset_id.slice(0, 12)}.${extension}`}
            aria-label="下载图片原件"
            title="下载图片原件"
          >
            <Download size={12} />
          </a>
        )}
      </figcaption>
    </figure>
  );
}

/** 原件与初次机器结构单独读取，修订结果绝不冒充原文。 */
export function OriginalPreview({
  result,
  api,
  onError,
  page,
  location,
  onSelect,
  loadSourcePreview,
}: {
  result: ParsedResult;
  page?: number;
  location?: SourceLocation;
  api: Client;
  onError: (error: unknown) => void;
  onSelect?: (location: SourceLocation) => void;
  loadSourcePreview?: (signal: AbortSignal) => Promise<ReviewResult>;
}) {
  const source = result.source;
  const nativeOnly = ![
    "pdf",
    "image",
    "text",
    "txt",
    "markdown",
    "html",
    "json",
    "xml",
  ].includes(source.format);
  const [mode, setMode] = useState(nativeOnly ? "structure" : "original");
  const [blob, setBlob] = useState<Blob | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [allowLarge, setAllowLarge] = useState(false);
  const [machine, setMachine] = useState<ReviewResult | null>(null);
  const [machineFailed, setMachineFailed] = useState(false);
  const previewLoader = useRef(loadSourcePreview);
  previewLoader.current = loadSourcePreview;
  const hasPreviewLoader = Boolean(loadSourcePreview);
  const large = source.size_bytes > 32 * 1024 * 1024;
  useEffect(() => {
    setMode(nativeOnly ? "structure" : "original");
    setAllowLarge(false);
  }, [source.asset_id, nativeOnly]);
  useEffect(() => {
    // 文字格式没有可验证的像素坐标，显式定位时转到原件的只读结构段落。
    if (
      location?.selection &&
      !["pdf", "html", "image"].includes(source.format)
    )
      setMode("structure");
  }, [location?.selection, source.format]);
  useEffect(() => {
    const controller = new AbortController();
    let temporary: string | null = null;
    setBlob(null);
    setImageUrl(null);
    setText(null);
    setFailed(false);
    if (!source.asset_id || nativeOnly || (large && !allowLarge))
      return () => controller.abort();
    void api
      .asset(source.asset_id, controller.signal)
      .then(async (value) => {
        if (controller.signal.aborted) return;
        if (source.format === "pdf") setBlob(value);
        else if (source.format === "image") {
          const mediaType = await bitmapType(value);
          if (controller.signal.aborted) return;
          temporary = URL.createObjectURL(
            new Blob([value], { type: mediaType }),
          );
          setImageUrl(temporary);
        } else {
          // 超长文本只展示受限预览，完整原件仍可下载；不把浏览器变成无界文本编辑器。
          const content = await value.slice(0, 1024 * 1024).text();
          if (!controller.signal.aborted) {
            let readable = content;
            if (source.format === "json" && value.size <= 1024 * 1024) {
              try {
                readable = JSON.stringify(JSON.parse(content), null, 2);
              } catch {
                /* 无效 JSON 保留原字节文字，校验由解析器报告。 */
              }
            }
            setText(readable);
          }
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => {
      controller.abort();
      if (temporary) URL.revokeObjectURL(temporary);
    };
  }, [
    api,
    source.asset_id,
    source.format,
    nativeOnly,
    large,
    allowLarge,
    retry,
  ]);
  useEffect(() => {
    const controller = new AbortController();
    setMachine(null);
    setMachineFailed(false);
    if (!previewLoader.current || mode !== "structure")
      return () => controller.abort();
    void previewLoader
      .current(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setMachine(value);
      })
      .catch(() => {
        if (!controller.signal.aborted) setMachineFailed(true);
      });
    return () => controller.abort();
  }, [hasPreviewLoader, source.asset_id, mode, retry]);
  /** 下载当前版本关联的不可变原件，失败保留审校上下文。 */
  async function downloadOriginal(): Promise<void> {
    if (!source.asset_id) return;
    try {
      downloadBlob(await api.asset(source.asset_id), source.name);
    } catch (error: unknown) {
      onError(error);
    }
  }
  return (
    <div className="review-original-view">
      <div className="review-preview-controls">
        <span className="review-original-label">
          <FileText size={14} /> 不可变原件
        </span>
        {!["pdf", "image"].includes(source.format) && !nativeOnly && (
          <Segmented
            size="small"
            value={mode}
            onChange={(value) => setMode(value)}
            aria-label="原件预览方式"
            options={[
              { value: "original", label: "原件内容" },
              { value: "structure", label: "结构定位" },
            ]}
          />
        )}
        <span className="review-control-spacer" />
        <Button
          size="small"
          type="default"
          disabled={!source.asset_id}
          onClick={() => void downloadOriginal()}
        >
          <Download size={13} />
          下载原件
        </Button>
      </div>
      {!source.asset_id ? (
        <div className="review-preview-empty">
          此版本未保存原件资产，无法核对原文。
        </div>
      ) : mode === "structure" ? (
        <>
          <div className="review-preview-note">
            {["doc", "docx", "ppt", "pptx", "word", "xls", "xlsx"].includes(
              source.format,
            )
              ? "Office 原生结构预览，非原应用版面；完整排版请下载原件。"
              : "原件首次机器解析的只读结构，保留原始内容与定位引用。"}
          </div>
          {!loadSourcePreview ? (
            <div className="review-preview-empty">
              此服务未提供原生结构预览，请下载原件对照。
            </div>
          ) : machineFailed ? (
            <div className="review-preview-empty">
              原件结构读取失败。
              <Button
                type="default"
                onClick={() => setRetry((value) => value + 1)}
              >
                重试
              </Button>
            </div>
          ) : machine ? (
            <SourceStructure
              result={machine}
              api={api}
              focusedElementRef={location?.elementRef}
              selection={location?.selection}
              onSelect={onSelect}
            />
          ) : (
            <div className="review-preview-empty">
              <LoaderCircle className="spin" />
              读取原件结构…
            </div>
          )}
        </>
      ) : large && !allowLarge ? (
        <div className="review-preview-empty">
          <p>原件超过 32 MB。按需加载可避免影响审校操作。</p>
          <Button onClick={() => setAllowLarge(true)}>加载原件预览</Button>
        </div>
      ) : failed ? (
        <div className="review-preview-empty">
          {source.format === "image"
            ? "原始图片无法预览，可能是资源读取失败或浏览器不支持该位图格式。请下载原件查看。"
            : "原件读取失败。"}
          <Button type="default" onClick={() => setRetry((value) => value + 1)}>
            重试
          </Button>
        </div>
      ) : imageUrl && source.format === "image" ? (
        <ReviewScroll label="原始图片独立滚动区域">
          <img
            className="review-original-image"
            src={imageUrl}
            alt={`原始图片：${source.name}`}
            onError={() => setFailed(true)}
          />
        </ReviewScroll>
      ) : blob && source.format === "pdf" ? (
        <Suspense
          fallback={
            <div className="review-preview-empty">加载 PDF 阅读器…</div>
          }
        >
          <PdfSource
            blob={blob}
            page={location?.page ?? page}
            onSelect={onSelect}
          />
        </Suspense>
      ) : text !== null ? (
        source.format === "html" ? (
          <>
            {source.size_bytes > 1024 * 1024 && (
              <p className="review-preview-note">
                HTML 仅预览前 1 MB，完整内容请下载原件。
              </p>
            )}
            <Suspense
              fallback={
                <div className="review-preview-empty">加载 HTML 快照…</div>
              }
            >
              <HtmlSource
                content={text}
                origins={result.origins}
                location={location}
                onSelect={onSelect}
              />
            </Suspense>
          </>
        ) : (
          <>
            <div className="review-preview-note">
              {source.format.toUpperCase()} 原件内容 · 保留文字格式
              {source.size_bytes > 1024 * 1024
                ? " · 仅预览前 1 MB，完整内容请下载"
                : ""}{" "}
              · 段落双向定位请选择「结构定位」
            </div>
            <ReviewScroll label="原文内容独立滚动区域">
              <pre className="review-original-text">{text}</pre>
            </ReviewScroll>
          </>
        )
      ) : (
        <div className="review-preview-empty">
          <LoaderCircle className="spin" />
          读取原件…
        </div>
      )}
    </div>
  );
}
