import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import type { ReactNode } from "react";

import { useChatStore } from "@/store/chatStore";
import { useServerInfo } from "@/lib/CapabilitiesContext";

/**
 * Refresh the active server queue and drain legacy browser queues when their
 * sessions become idle. Server-owned rows are never dispatched by the browser.
 */
export function QueueFlushProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const info = useServerInfo();
  const conversationId = useChatStore((s) => s.conversationId);
  const refreshQueuedMessages = useChatStore((s) => s.refreshQueuedMessages);
  const queuedMessages = useChatStore((s) => s.queuedMessages);
  const flushBackgroundQueues = useChatStore((s) => s.flushBackgroundQueues);

  // Polling also observes changes made through another server replica.
  useEffect(() => {
    if (info === "loading" || !info.durable_send_queue || conversationId === null) return;
    const refresh = () => {
      void refreshQueuedMessages(conversationId);
    };
    refresh();
    const timer = window.setInterval(refresh, 2_000);
    window.addEventListener("focus", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
    };
  }, [conversationId, info, refreshQueuedMessages]);

  // Re-evaluate whenever the queue itself changes (e.g. a message enqueued in
  // another conversation, or one drained here).
  useEffect(() => {
    flushBackgroundQueues();
  }, [queuedMessages, flushBackgroundQueues]);

  // Re-evaluate whenever the sidebar/conversations cache changes — this is how
  // a navigated-away conversation's idle transition (WS overlay or poll)
  // reaches us without its live SSE stream.
  useEffect(() => {
    const cache = queryClient.getQueryCache();
    const unsubscribe = cache.subscribe((event) => {
      const key = event.query.queryKey;
      if (Array.isArray(key) && key[0] === "conversations") {
        flushBackgroundQueues();
      }
    });
    return unsubscribe;
  }, [queryClient, flushBackgroundQueues]);

  return children;
}
