import { useEffect, useMemo, useState } from "react";
import { getBrowserPaneBridge } from "@/components/BrowserPane/browserPaneBridge";
import { browserViewId } from "@/hooks/useBrowserTabs";

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
    const bridge = getBrowserPaneBridge();
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
    const forget = (viewId: string) =>
      setUrlsByViewId((current) => {
        if (!(viewId in current)) return current;
        return Object.fromEntries(
          Object.entries(current).filter(([knownViewId]) => knownViewId !== viewId),
        );
      });
    const unsubscribeClosed = bridge.onBrowserViewClosed?.(({ conversationId: viewId }) => {
      if (!viewIds.has(viewId)) return;
      updatedByEvent.add(viewId);
      forget(viewId);
    });
    for (const viewId of viewIds) {
      void bridge
        .browserHasView?.(viewId)
        .then((view) => {
          if (cancelled || updatedByEvent.has(viewId)) return;
          // A reopened tab id must not keep the URL of its previous view.
          if (!view?.exists || !view.url) {
            forget(viewId);
            return;
          }
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
