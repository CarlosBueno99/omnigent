import { Button } from "@/components/ui/button";
import { getEmbedRoot } from "@/lib/host";
import type { ComponentProps, ReactNode, RefObject } from "react";
import {
  cloneElement,
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

// Streamdown portals fullscreen to document.body, outside the embed's scoped CSS.
export const MERMAID_NO_FULLSCREEN = { fullscreen: false } as const;

// Reproduced from Streamdown 2.5's own "View fullscreen" / "Exit fullscreen"
// glyphs so our button matches the download/copy icons in the same mermaid bar.
export const FullscreenIcon = (props: ComponentProps<"svg">) => (
  <svg fill="none" height={16} viewBox="0 0 16 16" width={16} {...props}>
    <path
      clipRule="evenodd"
      d="M1 5.25V6H2.5V5.25V2.5H5.25H6V1H5.25H2C1.44772 1 1 1.44772 1 2V5.25ZM5.25 14.9994H6V13.4994H5.25H2.5V10.7494V9.99939H1V10.7494V13.9994C1 14.5517 1.44772 14.9994 2 14.9994H5.25ZM15 10V10.75V14C15 14.5523 14.5523 15 14 15H10.75H10V13.5H10.75H13.5V10.75V10H15ZM10.75 1H10V2.5H10.75H13.5V5.25V6H15V5.25V2C15 1.44772 14.5523 1 14 1H10.75Z"
      fill="currentColor"
      fillRule="evenodd"
    />
  </svg>
);

export function isMermaidPre(children: ReactNode): boolean {
  return (
    isValidElement<{ className?: string }>(children) &&
    (children.props.className?.split(/\s+/).includes("language-mermaid") ?? false)
  );
}

// A click that lands while Streamdown is still drawing the diagram opens it once drawn.
const DIAGRAM_RENDER_WAIT_MS = 5_000;

// Opens the block's diagram, or waits for it to draw; returns a cancel for that wait.
const MermaidFullscreenContext = createContext<((block: HTMLElement) => () => void) | null>(null);

function MermaidFullscreenOverlay({
  onClose,
  svgMarkup,
}: {
  onClose: () => void;
  svgMarkup: string;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/95 text-foreground backdrop-blur-sm"
      // Clicking the diagram must not dismiss it.
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="presentation"
    >
      <Button
        aria-label="Exit fullscreen"
        className="absolute top-4 right-4"
        onClick={onClose}
        size="icon-sm"
        title="Exit fullscreen"
        type="button"
        variant="ghost"
      >
        <FullscreenIcon />
      </Button>
      <div
        className="flex size-full items-center justify-center p-8 [&>svg]:h-auto [&>svg]:max-h-full [&>svg]:w-auto [&>svg]:max-w-full"
        // The cloned SVG is Mermaid's own trusted, already-sanitized render.
        dangerouslySetInnerHTML={{ __html: svgMarkup }}
        onClick={(e) => {
          if (e.target === e.currentTarget) onClose();
        }}
        role="presentation"
      />
    </div>,
    getEmbedRoot() ?? document.body,
  );
}

// Keeps the open diagram outside message blocks, which remount when a streamed reply settles.
// A new resetKey (the app passes its route path) closes the diagram.
export function MermaidFullscreenProvider({
  children,
  resetKey,
}: {
  children: ReactNode;
  resetKey?: string;
}) {
  const [svgMarkup, setSvgMarkup] = useState<string | null>(null);
  const [openedForKey, setOpenedForKey] = useState(resetKey);
  if (openedForKey !== resetKey) {
    setOpenedForKey(resetKey);
    setSvgMarkup(null);
  }
  // Only the latest click may open a diagram once it draws.
  const cancelPendingRef = useRef<(() => void) | null>(null);
  useEffect(() => () => cancelPendingRef.current?.(), [resetKey]);

  const openDiagram = useCallback((block: HTMLElement) => {
    cancelPendingRef.current?.();
    // The rendered diagram is Mermaid's own <svg id="mermaid-…"> — scope to it so
    // we don't clone a toolbar/zoom-control icon (all 16×16 <svg>s in the block).
    const findDiagram = () => block.querySelector('svg[id^="mermaid-"]');
    const diagram = findDiagram();
    if (diagram) {
      setSvgMarkup(diagram.outerHTML);
      return () => {};
    }
    const observer = new MutationObserver(() => {
      const rendered = findDiagram();
      if (!rendered) return;
      cancelWait();
      setSvgMarkup(rendered.outerHTML);
    });
    const timer = window.setTimeout(() => cancelWait(), DIAGRAM_RENDER_WAIT_MS);
    const cancelWait = () => {
      observer.disconnect();
      window.clearTimeout(timer);
      if (cancelPendingRef.current === cancelWait) cancelPendingRef.current = null;
    };
    observer.observe(block, { childList: true, subtree: true });
    cancelPendingRef.current = cancelWait;
    return cancelWait;
  }, []);
  const close = useCallback(() => {
    cancelPendingRef.current?.();
    setSvgMarkup(null);
  }, []);
  return (
    <MermaidFullscreenContext.Provider value={openDiagram}>
      {children}
      {svgMarkup != null && <MermaidFullscreenOverlay onClose={close} svgMarkup={svgMarkup} />}
    </MermaidFullscreenContext.Provider>
  );
}

// Reuse the sanitized inline SVG without rerunning Mermaid or copying toolbar icons.
function MermaidFullscreenButton({ blockRef }: { blockRef: RefObject<HTMLDivElement | null> }) {
  const openDiagram = useContext(MermaidFullscreenContext);
  const cancelWaitRef = useRef<(() => void) | null>(null);
  useEffect(() => () => cancelWaitRef.current?.(), []);

  const openFullscreen = useCallback(() => {
    const block = blockRef.current;
    if (!block || !openDiagram) return;
    cancelWaitRef.current = openDiagram(block);
  }, [blockRef, openDiagram]);

  return (
    <Button
      aria-label="View fullscreen"
      className="size-6 bg-sidebar/80 text-muted-foreground hover:bg-sidebar/80 hover:text-foreground dark:hover:bg-sidebar/80 supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur"
      onClick={openFullscreen}
      size="icon-sm"
      title="View fullscreen"
      type="button"
      variant="ghost"
    >
      <FullscreenIcon />
    </Button>
  );
}

// The .chat-mermaid rule reserves a slot beside Streamdown's download/copy toolbar.
export function MermaidBlock({ block }: { block: ReactNode }) {
  const blockRef = useRef<HTMLDivElement>(null);
  const hasProvider = useContext(MermaidFullscreenContext) != null;
  const content = (
    <div ref={blockRef} className="chat-mermaid relative">
      {block}
      <div className="absolute top-2 right-2 z-20 flex items-center rounded-md border border-sidebar bg-sidebar/80 px-1.5 py-1 supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur">
        <MermaidFullscreenButton blockRef={blockRef} />
      </div>
    </div>
  );
  // Isolated renders outside the app root host their own overlay.
  return hasProvider ? content : <MermaidFullscreenProvider>{content}</MermaidFullscreenProvider>;
}

function MermaidPre({ children }: ComponentProps<"pre">) {
  // Preserve Streamdown's default handling of ordinary code fences.
  const block = isValidElement(children)
    ? cloneElement(children, { "data-block": "true" } as Record<string, unknown>)
    : children;
  return isMermaidPre(children) ? <MermaidBlock block={block} /> : block;
}

// Keep the replacement toolbar and disabled native fullscreen together on every surface.
export const MERMAID_STREAMDOWN_PROPS = {
  components: { pre: MermaidPre },
  controls: { mermaid: MERMAID_NO_FULLSCREEN },
};
