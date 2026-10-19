import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type {
  PullRequestAssociation,
  PullRequestChecks,
  PullRequestInfo,
} from "@/hooks/usePullRequests";
import * as pullRequestHooks from "@/hooks/usePullRequests";
import {
  PULL_REQUEST_CONCURRENCY,
  PULL_REQUEST_REFRESH_MS,
  PULL_REQUEST_RETRY_MS,
  PullRequestQueue,
  usePullRequests,
} from "./pullRequests";

vi.mock("@/hooks/usePullRequests", () => ({
  fetchPullRequestInfo: vi.fn(),
  fetchSessionPullRequests: vi.fn(),
}));
const feature = vi.hoisted(() => ({ enabled: false, loading: false }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () =>
    feature.loading ? "loading" : { features: { pull_request_management: feature.enabled } },
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function session(id: string, gitBranch: string | null): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 1,
    updated_at: 1,
    labels: {},
    permission_level: null,
    git_branch: gitBranch,
  };
}

const NO_CHECKS: PullRequestChecks = { passing: 0, failing: 0, pending: 0, total: 0, runs: [] };

const FORGE_DISPLAY = {
  id: "example_forge",
  display_name: "Example Forge",
  request_name: "pull request",
  number_prefix: "!",
};

function info(pr: PullRequestInfo["pr"], extra: Partial<PullRequestInfo> = {}): PullRequestInfo {
  return { object: "session.github.info", available: true, pr, ...extra };
}

const FORGE_URL = "https://forge.example.test/acme/proj/_git/repo/pullrequest/7";

function openPr(url: string): NonNullable<PullRequestInfo["pr"]> {
  return {
    number: 7,
    title: "Ship it",
    state: "OPEN",
    url,
    is_draft: false,
    author: null,
    base_ref: null,
    head_ref: null,
    checks: NO_CHECKS,
  };
}

function association(overrides: Partial<PullRequestAssociation> = {}): PullRequestAssociation {
  return {
    url: FORGE_URL,
    host: "forge.example.test",
    repository: "acme/proj/repo",
    number: 7,
    relationship: "created",
    ...overrides,
  };
}

describe("PullRequestQueue", () => {
  it("runs at most the configured number of tasks at once and drops replaced pending work", async () => {
    const queue = new PullRequestQueue(2);
    const gates = [deferred<void>(), deferred<void>(), deferred<void>()];
    const started: string[] = [];
    const task = (key: string, index: number) => ({
      key,
      run: () => {
        started.push(key);
        return gates[index].promise;
      },
    });
    queue.replace([task("a", 0), task("b", 1), task("c", 2)]);
    expect(started).toEqual(["a", "b"]);

    // Pending "c" is dropped; the active tasks keep running and "d" queues behind them.
    queue.replace([task("d", 2)]);
    gates[0].resolve();
    await waitFor(() => expect(started).toEqual(["a", "b", "d"]));
    gates[1].resolve();
    gates[2].resolve();
    await Promise.resolve();
    expect(started).not.toContain("c");
  });
});

describe("usePullRequests", () => {
  beforeEach(() => {
    vi.mocked(pullRequestHooks.fetchPullRequestInfo).mockReset();
    vi.mocked(pullRequestHooks.fetchSessionPullRequests).mockReset();
    feature.enabled = false;
    feature.loading = false;
  });
  afterEach(() => vi.useRealTimers());

  function wrapper({ children }: { children: ReactNode }) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return createElement(QueryClientProvider, { client }, children);
  }

  it("waits for capabilities and ignores an old mode's in-flight results", async () => {
    feature.loading = true;
    const legacy = deferred<PullRequestInfo>();
    vi.mocked(pullRequestHooks.fetchPullRequestInfo).mockReturnValue(legacy.promise);
    vi.mocked(pullRequestHooks.fetchSessionPullRequests).mockResolvedValue({
      prs: [association()],
    });
    const sessions = [session("s", "main")];
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result, rerender } = renderHook(() => usePullRequests(sessions), {
      wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children),
    });
    expect(pullRequestHooks.fetchPullRequestInfo).not.toHaveBeenCalled();
    feature.loading = false;
    rerender();
    await waitFor(() => expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(1));
    feature.enabled = true;
    rerender();
    await waitFor(() => expect(result.current.s?.count).toBe(1));
    await act(async () => legacy.resolve(info(null)));
    expect(result.current.s?.count).toBe(1);
  });

  it("accepts only safe visible associations and prunes removed sessions", async () => {
    feature.enabled = true;
    const pending = deferred<{ prs: PullRequestAssociation[] }>();
    vi.mocked(pullRequestHooks.fetchSessionPullRequests).mockReturnValue(pending.promise);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result, rerender } = renderHook(({ sessions }) => usePullRequests(sessions), {
      initialProps: { sessions: [session("s", null)] },
      wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children),
    });
    act(() => {
      client.setQueryData(["session-prs", "other"], { prs: [association()] });
      client.setQueryData(["session-prs", "s"], {
        prs: [association({ url: "javascript:alert(1)" }), association()],
      });
    });
    expect(Object.keys(result.current)).toEqual(["s"]);
    expect(result.current.s?.url).toBe(FORGE_URL);
    rerender({ sessions: [] });
    await act(async () => pending.resolve({ prs: [association()] }));
    expect(result.current).toEqual({});
  });

  it("loads flagged branchless sessions, retries failures and reflects association mutations", async () => {
    feature.enabled = true;
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(pullRequestHooks.fetchSessionPullRequests)
      .mockRejectedValueOnce(new Error("Disconnected"))
      .mockResolvedValue({ prs: [association(), association({ number: 8 })] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const sessions = [session("plain", null)];
    const { result } = renderHook(() => usePullRequests(sessions), {
      wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children),
    });
    await waitFor(() => expect(pullRequestHooks.fetchSessionPullRequests).toHaveBeenCalledTimes(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_RETRY_MS + 50);
    });
    await waitFor(() =>
      expect(result.current.plain).toMatchObject({ number: 7, count: 2, state: "UNKNOWN" }),
    );
    expect(pullRequestHooks.fetchPullRequestInfo).not.toHaveBeenCalled();
    act(() => {
      client.setQueryData(["session-prs", "plain"], { prs: [] });
    });
    expect(result.current.plain).toBeNull();
    vi.useRealTimers();
  });

  it("looks up only branch-bearing sessions, bounded by the concurrency limit", async () => {
    const gates = new Map<string, ReturnType<typeof deferred<PullRequestInfo>>>();
    vi.mocked(pullRequestHooks.fetchPullRequestInfo).mockImplementation((id) => {
      const gate = deferred<PullRequestInfo>();
      gates.set(id, gate);
      return gate.promise;
    });
    const sessions = [
      ...Array.from({ length: PULL_REQUEST_CONCURRENCY + 2 }, (_, index) =>
        session(`branch_${index}`, `feat/${index}`),
      ),
      session("plain", null),
    ];

    const { result } = renderHook(() => usePullRequests(sessions), { wrapper });

    await waitFor(() =>
      expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(PULL_REQUEST_CONCURRENCY),
    );
    expect(pullRequestHooks.fetchPullRequestInfo).not.toHaveBeenCalledWith("plain");

    gates.get("branch_0")!.resolve(
      info({
        number: 7,
        title: "Ship it",
        state: "OPEN",
        url: "https://github.com/acme/repo/pull/7",
        is_draft: false,
        author: null,
        base_ref: null,
        head_ref: null,
        checks: NO_CHECKS,
      }),
    );
    await waitFor(() =>
      expect(result.current.branch_0).toEqual({
        number: 7,
        title: "Ship it",
        state: "OPEN",
        url: "https://github.com/acme/repo/pull/7",
      }),
    );
    // Freeing one slot starts the next queued lookup.
    await waitFor(() =>
      expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(
        PULL_REQUEST_CONCURRENCY + 1,
      ),
    );

    gates.get("branch_1")!.resolve(info(null));
    await waitFor(() => expect(result.current.branch_1).toBeNull());
  });

  it("retries a failed lookup after the retry window, not the full refresh window", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(pullRequestHooks.fetchPullRequestInfo)
      .mockRejectedValueOnce(new Error("runner offline"))
      .mockResolvedValue(
        info({
          number: 3,
          title: "Back",
          state: "OPEN",
          url: "https://github.com/acme/repo/pull/3",
          is_draft: false,
          author: null,
          base_ref: null,
          head_ref: null,
          checks: NO_CHECKS,
        }),
      );
    const sessions = [session("s", "main")];
    const { result } = renderHook(() => usePullRequests(sessions), { wrapper });
    await waitFor(() => expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_RETRY_MS + 50);
    });
    await waitFor(() => expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.s).toMatchObject({ number: 3 }));

    // A successful lookup is not repeated until the full refresh window elapses.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_RETRY_MS + 50);
    });
    expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_REFRESH_MS);
    });
    await waitFor(() => expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(3));
    vi.useRealTimers();
  });

  it("ignores pull requests without an https URL", async () => {
    vi.mocked(pullRequestHooks.fetchPullRequestInfo).mockResolvedValue(
      info({
        number: 1,
        title: "Local",
        state: "OPEN",
        url: "javascript:alert(1)",
        is_draft: false,
        author: null,
        base_ref: null,
        head_ref: null,
        checks: NO_CHECKS,
      }),
    );
    const { result } = renderHook(() => usePullRequests([session("s", "main")]), { wrapper });
    await waitFor(() => expect(result.current.s).toBeNull());
  });

  interface ProviderCase {
    name: string;
    prs?: PullRequestAssociation[];
    provider?: string | null;
    provider_display?: PullRequestInfo["provider_display"];
    expected: string | null | undefined;
  }

  it.each<ProviderCase>([
    {
      name: "the PR's own provider over the session's",
      prs: [association({ provider: "example_forge" })],
      provider: "github",
      expected: "example_forge",
    },
    {
      name: "the PR's own GitHub provider over an Example Forge session",
      prs: [association({ provider: "github" })],
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      expected: "github",
    },
    {
      name: "the session's provider when the PR names none",
      prs: [association()],
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      expected: "example_forge",
    },
    {
      name: "the session's provider for an untracked PR",
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      expected: "example_forge",
    },
    { name: "no provider when the host names none", provider: null, expected: null },
    { name: "no provider from a host that predates the field", expected: undefined },
  ])(
    "carries the provider through: $name",
    async ({ prs, provider, provider_display, expected }) => {
      vi.mocked(pullRequestHooks.fetchPullRequestInfo).mockResolvedValue(
        info(openPr(FORGE_URL), { prs, provider, provider_display }),
      );
      const { result } = renderHook(() => usePullRequests([session("s", "main")]), { wrapper });
      await waitFor(() => expect(result.current.s).toMatchObject({ number: 7 }));
      expect(result.current.s?.provider).toBe(expected);
    },
  );

  it("updates the card when a refresh changes only the provider", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(pullRequestHooks.fetchPullRequestInfo)
      .mockResolvedValueOnce(info(openPr(FORGE_URL), { provider: "github" }))
      .mockResolvedValue(info(openPr(FORGE_URL), { provider: "example_forge" }));
    const { result } = renderHook(() => usePullRequests([session("s", "main")]), { wrapper });
    await waitFor(() => expect(result.current.s?.provider).toBe("github"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_REFRESH_MS + PULL_REQUEST_RETRY_MS + 50);
    });
    await waitFor(() => expect(result.current.s?.provider).toBe("example_forge"));
    vi.useRealTimers();
  });
});
