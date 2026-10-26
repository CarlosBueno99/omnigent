import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { useChatStore } from "@/store/chatStore";
import { QueueFlushProvider } from "./QueueFlushProvider";

const refresh = useChatStore.getState().refreshQueuedMessages;
afterEach(() => {
  cleanup();
  useChatStore.setState({ conversationId: null, refreshQueuedMessages: refresh });
  vi.useRealTimers();
});

it.each([false, true])(
  "polls only the viewed session when durable queue support is %s",
  (enabled) => {
    vi.useFakeTimers();
    const poll = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      conversationId: "first",
      queuedMessages: [],
      refreshQueuedMessages: poll,
    });
    const rendered = render(
      <QueryClientProvider client={new QueryClient()}>
        <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, durable_send_queue: enabled }}>
          <QueueFlushProvider>{null}</QueueFlushProvider>
        </CapabilitiesProvider>
      </QueryClientProvider>,
    );
    act(() => vi.advanceTimersByTime(2_000));
    act(() => useChatStore.setState({ conversationId: "second" }));
    act(() => vi.advanceTimersByTime(2_000));
    rendered.unmount();
    act(() => vi.advanceTimersByTime(2_000));
    expect(poll.mock.calls).toEqual(enabled ? [["first"], ["first"], ["second"], ["second"]] : []);
  },
);
