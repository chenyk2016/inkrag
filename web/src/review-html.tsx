/** HTML 快照只保留静态文档语义，网络资源、样式、脚本和表单均不可执行。 */
import DOMPurify from "dompurify";
import { useEffect, useMemo, useRef } from "react";
import { ReviewScroll, revealInViewport } from "./review-scroll";
import { markHtmlSources } from "./html-source-locators";
import type { SourceLocation } from "./review-types";

/** /text() 表示父标签的直接文本。同一位置包含多个引用时交给只读结构核对。 */
function uniqueHtmlLocators(locators: readonly string[]): Set<string> {
  const unique = new Set<string>();
  const ambiguous = new Set<string>();
  for (const locator of locators) {
    if (!locator.startsWith("html:/")) continue;
    const path = locator.replace(/\/text\(\)$/, "");
    if (unique.delete(path)) ambiguous.add(path);
    if (!ambiguous.has(path)) unique.add(path);
  }
  return unique;
}

/** DOMPurify 净化整个快照，仅保留本次预览生成且可核对的路径标记。 */
export function safeHtmlSnapshot(
  content: string,
  locators: readonly string[],
): { html: string; available: Set<string> } {
  const marker = `data-inkrag-${crypto.getRandomValues(new Uint32Array(4)).join("-")}`;
  const { marked, positions } = markHtmlSources(
    content,
    marker,
    uniqueHtmlLocators(locators),
  );
  const safe = DOMPurify.sanitize(marked, {
    WHOLE_DOCUMENT: true,
    RETURN_DOM: true,
    ALLOWED_TAGS: [
      "html",
      "head",
      "body",
      "title",
      "article",
      "section",
      "main",
      "header",
      "footer",
      "nav",
      "aside",
      "div",
      "span",
      "p",
      "h1",
      "h2",
      "h3",
      "h4",
      "h5",
      "h6",
      "ul",
      "ol",
      "li",
      "dl",
      "dt",
      "dd",
      "br",
      "hr",
      "strong",
      "b",
      "em",
      "i",
      "u",
      "s",
      "blockquote",
      "pre",
      "code",
      "table",
      "caption",
      "thead",
      "tbody",
      "tfoot",
      "tr",
      "th",
      "td",
      "figure",
      "figcaption",
      "a",
      "img",
      "sup",
      "sub",
    ],
    ALLOWED_ATTR: ["alt", "title", "colspan", "rowspan", "scope", marker],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    ADD_FORBID_CONTENTS: ["head", "noscript"],
  });
  const available = new Set<string>();
  const body = safe?.ownerDocument?.body;
  if (!body) return { html: "", available };

  // 浏览器可能复制格式化标签。只有唯一存活且标签名相符的标记才允许双向定位。
  const tags = new Map<string, Element>();
  const ambiguous = new Set<string>();
  body.querySelectorAll(`[${marker}]`).forEach((tag) => {
    const id = tag.getAttribute(marker) ?? "";
    tag.removeAttribute(marker);
    if (tags.has(id)) ambiguous.add(id);
    tags.set(id, tag);
  });
  for (const [id, tag] of tags) {
    const source = positions.get(id);
    if (source && source.name === tag.localName && !ambiguous.has(id)) {
      tag.setAttribute("data-source-locator", source.locator);
      available.add(source.locator);
    }
  }
  body.querySelectorAll("img").forEach((node) => {
    const placeholder = node.ownerDocument.createElement("span");
    const alt = node.getAttribute("alt");
    const locator = node.getAttribute("data-source-locator");
    placeholder.textContent = `[原文图片${alt ? `：${alt}` : ""}，见图片资源]`;
    if (locator) placeholder.setAttribute("data-source-locator", locator);
    node.replaceWith(placeholder);
  });
  return { html: body.innerHTML, available };
}

/** 点击真实段落回传其路径；外部定位仅在真实节点匹配时标记段落。 */
export function HtmlSource({
  content,
  location,
  origins,
  onSelect,
}: {
  content: string;
  location?: SourceLocation;
  origins: readonly { locator: string }[];
  onSelect?: (location: SourceLocation) => void;
}) {
  const { html, available } = useMemo(
    () =>
      safeHtmlSnapshot(
        content,
        origins.map((origin) => origin.locator),
      ),
    [content, origins],
  );
  const locator = location?.locator?.replace(/\/text\(\)$/, "");
  const missing = Boolean(
    location?.elementRef && (!locator || !available.has(locator)),
  );
  const viewport = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const container = viewport.current;
    if (!container) return;
    container
      .querySelectorAll(".is-focused")
      .forEach((node) => node.classList.remove("is-focused"));
    if (!locator) return;
    const target = Array.from(
      container.querySelectorAll<HTMLElement>("[data-source-locator]"),
    ).find((node) => node.dataset.sourceLocator === locator);
    if (target) {
      target.classList.add("is-focused");
      revealInViewport(container, target);
    }
  }, [location, locator, html]);
  return (
    <>
      <div className="review-preview-note">
        安全 HTML 快照 · 已移除脚本、样式及外链
        {available.size > 0 && " · 点击有来源标记的段落定位结果"}
        {missing && (
          <span role="status">
            {" · 此元素在当前 HTML 快照中无法定位，请切换「结构定位」核对。"}
          </span>
        )}
      </div>
      <ReviewScroll label="HTML 原文独立滚动区域" viewportRef={viewport}>
        <div
          className="review-html-snapshot"
          onClick={(event) => {
            if (!(event.target instanceof Element)) return;
            const target = event.target.closest<HTMLElement>(
              "[data-source-locator]",
            );
            if (target?.dataset.sourceLocator)
              onSelect?.({ locator: target.dataset.sourceLocator });
          }}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </ReviewScroll>
    </>
  );
}
