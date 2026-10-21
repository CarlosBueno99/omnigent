import { useEffect, useMemo, useState } from "react";
import { browserViewId } from "@/hooks/useBrowserTabs";
import { supportsBrowser } from "@/lib/nativeBridge";

/** Subset of the desktop bridge used to follow each browser view's URL. */
interface BrowserUrlBridge {
  browserHasView?: (viewId: string) => Promise<{ exists: boolean; url?: string }>;
  onBrowserUrlChanged?: (
    callback: (payload: { conversationId: string; url: string }) => void,
  ) => () => void;
  onBrowserViewClosed?: (callback: (payload: { conversationId: string }) => void) => () => void;
}

function urlBridge(): BrowserUrlBridge | null {
  if (!supportsBrowser()) return null;
  return (window as unknown as { omnigentDesktop?: BrowserUrlBridge }).omnigentDesktop ?? null;
}

/**
 * Current page URL of every open browser tab in a session, keyed by tab id.
 * Covers unselected tabs too, whose panes are not mounted. A tab with no
 * native view yet has no entry.
 */
export function useBrowserTabUrls(
  conversationId: string,
  tabIds: readonly string[],
): Record<string, string> {
  const [urlsByViewId, setUrlsByViewId] = useState<Record<string, string>>({});
  const tabKey = tabIds.join("\n");

  useEffect(() => {
    setUrlsByViewId({});
  }, [conversationId]);

  useEffect(() => {
    const bridge = urlBridge();
    if (!bridge) return;
    const viewIds = new Set(
      tabKey === "" ? [] : tabKey.split("\n").map((tabId) => browserViewId(conversationId, tabId)),
    );
    // A navigation event is newer than any snapshot still in flight.
    const updatedByEvent = new Set<string>();
    let cancelled = false;

    const unsubscribeUrl = bridge.onBrowserUrlChanged?.(({ conversationId: viewId, url }) => {
      if (!viewIds.has(viewId)) return;
      updatedByEvent.add(viewId);
      setUrlsByViewId((current) =>
        current[viewId] === url ? current : { ...current, [viewId]: url },
      );
    });
    const unsubscribeClosed = bridge.onBrowserViewClosed?.(({ conversationId: viewId }) => {
      if (!viewIds.has(viewId)) return;
      updatedByEvent.add(viewId);
      setUrlsByViewId((current) => {
        if (!(viewId in current)) return current;
        return Object.fromEntries(
          Object.entries(current).filter(([knownViewId]) => knownViewId !== viewId),
        );
      });
    });
    for (const viewId of viewIds) {
      void bridge
        .browserHasView?.(viewId)
        .then((view) => {
          if (cancelled || updatedByEvent.has(viewId) || !view?.exists || !view.url) return;
          const url = view.url;
          setUrlsByViewId((current) =>
            current[viewId] === url ? current : { ...current, [viewId]: url },
          );
        })
        .catch(() => {});
    }
    return () => {
      cancelled = true;
      unsubscribeUrl?.();
      unsubscribeClosed?.();
    };
  }, [conversationId, tabKey]);

  return useMemo(() => {
    const urls: Record<string, string> = {};
    for (const tabId of tabKey === "" ? [] : tabKey.split("\n")) {
      const url = urlsByViewId[browserViewId(conversationId, tabId)];
      if (url) urls[tabId] = url;
    }
    return urls;
  }, [conversationId, tabKey, urlsByViewId]);
}
