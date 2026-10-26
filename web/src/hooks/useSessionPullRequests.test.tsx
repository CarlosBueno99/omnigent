import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import {
  sessionPullRequestsKey,
  useSessionPullRequests,
  useUnlinkSessionPr,
} from "./useSessionPullRequests";

afterEach(() => vi.unstubAllGlobals());

const one = "https://github.com/example/one/pull/42";
const two = {
  url: "https://github.com/example/two/pull/7",
  host: "github.com",
  repository: "example/two",
  number: 7,
  relationship: "attached",
};

function wrapper(client: QueryClient, stored: boolean) {
  return ({ children }: { children: ReactNode }) => (
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, session_pull_requests: stored }}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </CapabilitiesProvider>
  );
}

it.each([
  { server: "current", stored: true },
  { server: "older", stored: false },
])("unlinks on the $server server and stores what remains", async ({ stored }) => {
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new URL(String(input), "http://localhost");
      requests.push(`${init?.method ?? "GET"} ${request.pathname}`);
      if (init?.method === "DELETE") {
        expect(request.searchParams.get("url")).toBe(one);
        return new Response(
          JSON.stringify({ object: "list", data: [two], last_id: two.url, has_more: false }),
          { status: 200 },
        );
      }
      expect(JSON.parse(String(init?.body))).toEqual({ url: one, action: "remove" });
      return new Response(
        JSON.stringify({ object: "session.github.info", available: true, prs: [two] }),
        { status: 200 },
      );
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(() => useUnlinkSessionPr("conv_1"), {
    wrapper: wrapper(client, stored),
  });

  result.current.mutate(one);

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual([two]);
  expect(client.getQueryData(sessionPullRequestsKey("conv_1"))).toEqual([two]);
  expect(requests).toEqual(
    stored
      ? ["DELETE /v1/sessions/conv_1/pull-requests"]
      : ["POST /v1/sessions/conv_1/resources/github/prs"],
  );
});

it("reads every page of a current server's list and nothing from an older one", async () => {
  const first = { ...two, url: one, number: 42 };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const after = new URL(String(input), "http://localhost").searchParams.get("after");
    const page = after
      ? { object: "list", data: [two], last_id: two.url, has_more: false }
      : { object: "list", data: [first], last_id: one, has_more: true };
    return new Response(JSON.stringify(page), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);

  const older = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderHook(() => useSessionPullRequests("conv_1"), { wrapper: wrapper(older, false) });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(() => useSessionPullRequests("conv_1"), {
    wrapper: wrapper(client, true),
  });

  await waitFor(() => expect(result.current.data).toEqual([first, two]));
  expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
    "/v1/sessions/conv_1/pull-requests?limit=100",
    `/v1/sessions/conv_1/pull-requests?limit=100&after=${encodeURIComponent(one)}`,
  ]);
});
