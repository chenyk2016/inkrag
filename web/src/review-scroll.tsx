/** 所有审校滚动均有唯一、有界视口，定位只改变该视口的偏移。 */
import type { ReactNode, Ref } from "react";
import "./review.css";
import * as ScrollArea from "@radix-ui/react-scroll-area";

/** 使用成熟组件处理滚动条、触控和键盘，外层始终不参与内容滚动。 */
export function ReviewScroll({
  children,
  label,
  viewportRef,
  className = "",
}: {
  children: ReactNode;
  label: string;
  viewportRef?: Ref<HTMLDivElement>;
  className?: string;
}) {
  return (
    <ScrollArea.Root className={`review-scroll ${className}`} type="auto">
      <ScrollArea.Viewport
        ref={viewportRef}
        className="review-scroll-viewport"
        tabIndex={0}
        role="region"
        aria-label={label}
      >
        {children}
      </ScrollArea.Viewport>
      <ScrollArea.Scrollbar className="review-scrollbar" orientation="vertical">
        <ScrollArea.Thumb className="review-scroll-thumb" />
      </ScrollArea.Scrollbar>
      <ScrollArea.Scrollbar
        className="review-scrollbar"
        orientation="horizontal"
      >
        <ScrollArea.Thumb className="review-scroll-thumb" />
      </ScrollArea.Scrollbar>
      <ScrollArea.Corner className="review-scroll-corner" />
    </ScrollArea.Root>
  );
}

/** 禁用 scrollIntoView 的祖先传播：上下对照窗独立定位，页面及另一个窗不动。 */
export function revealInViewport(
  viewport: HTMLElement,
  target: HTMLElement,
): void {
  const viewportBounds = viewport.getBoundingClientRect();
  const targetBounds = target.getBoundingClientRect();
  viewport.scrollTo({
    top:
      viewport.scrollTop +
      targetBounds.top -
      viewportBounds.top -
      Math.max(
        12,
        (viewport.clientHeight -
          Math.min(target.clientHeight, viewport.clientHeight)) /
          2,
      ),
    behavior: "instant",
  });
}
