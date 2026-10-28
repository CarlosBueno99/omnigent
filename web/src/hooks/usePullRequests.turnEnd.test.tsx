// The GitHub info query refetches on the focused session's turn end
// (active → idle), so a PR the agent opened during the turn appears in the
// status-line indicator without opening the tab. This is the harness-agnostic
// replacement for the old per-tool git/gh signal — mirrors the trailing-edge
// invalidate proven in useWorkspaceChangedFiles.test.tsx.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: vi.fn(),
}));

import { useSessionHostOnline, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useChatStore } from "@/store/chatStore";
import { usePullRequestInfo } from "./usePullRequests";

const onlineMock = vi.mocked(useSessionRunnerOnline);
const hostOnlineMock = vi.mocked(useSessionHostOnline);
const chatStoreMock = vi.mocked(useChatStore);
const fetchMock = vi.fn();

type StubStatus = "idle" | "running" | "waiting" | "failed";

function stubChatStore(conversationId: string | null, sessionStatus: StubStatus) {
  chatStoreMock.mockImplementation((selector: unknown) => {
    if (typeof selector === "function") {
      return (
        selector as (s: { conversationId: string | null; sessionStatus: StubStatus }) => unknown
      )({ conversationId, sessionStatus });
    }
    return undefined;
  });
}

function pullRequestInfoResponse(): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => ({ object: "session.github.info", available: true }),
  } as unknown as Response;
}

function Probe({
  id,
  poll,
  includeReviewThreads,
}: {
  id: string | undefined;
  poll?: boolean;
  includeReviewThreads?: boolean;
}) {
  usePullRequestInfo(id, { poll, includeReviewThreads });
  return null;
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(pullRequestInfoResponse());
  vi.stubGlobal("fetch", fetchMock);
  onlineMock.mockReturnValue(true);
  hostOnlineMock.mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe("usePullRequestInfo turn-end invalidate", () => {
  it("shares one poll with lightweight observers and includes threads only while requested", async () => {
    vi.useFakeTimers();
    stubChatStore(null, "idle");
    fetchMock.mockImplementation(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        object: "session.github.info",
        available: true,
        tracking_available: true,
      }),
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (include: boolean) => (
      <QueryClientProvider client={client}>
        <Probe id="conv_shared" poll includeReviewThreads={include} />
        <Probe id="conv_shared" />
      </QueryClientProvider>
    );
    const view = render(tree(false));
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    view.rerender(tree(true));
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain("include_review_threads=true");
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[2][0]).toContain("include_review_threads=true");
    view.rerender(tree(false));
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock.mock.calls[3][0]).toBe("/v1/sessions/conv_shared/resources/github");
    client.clear();
  });

  it("refetches github info when the focused session goes running → idle", async () => {
    stubChatStore("conv_live", "running");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Turn ends: the trailing invalidate fires one more github-info fetch.
    stubChatStore("conv_live", "idle");
    rerender(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][0]).toBe("/v1/sessions/conv_live/resources/github");
  });

  it("does not refetch when status stays idle across renders", async () => {
    // Guard against a spurious refetch on every render — only the
    // active → idle transition triggers the trailing edge.
    stubChatStore("conv_live", "idle");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    rerender(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await Promise.resolve();
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
