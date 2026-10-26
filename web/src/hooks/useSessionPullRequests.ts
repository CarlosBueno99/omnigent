// A session's linked PRs as the server stores them, which answer while the host is offline.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ServerInfo } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { GitProviderDisplay } from "@/lib/gitProviders";
import { authenticatedFetch } from "@/lib/identity";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  updateSessionPr,
  type PullRequestAssociation,
  type PullRequestInfo,
} from "@/hooks/usePullRequests";

/** A session's first linked PR, as `SessionListItem.pull_request` carries it. */
export interface SessionPullRequestSummary {
  url: string;
  provider: string;
  provider_display?: GitProviderDisplay | null;
  repository: string;
  number: number;
  title?: string | null;
  /** "OPEN" | "MERGED" | "CLOSED" as last fetched; null before the host fetched it. */
  state?: string | null;
  is_draft?: boolean;
}

interface SessionPullRequestList {
  object: "list";
  data: PullRequestAssociation[];
  last_id: string | null;
  has_more: boolean;
}

/** Whether the server stores session PRs; older servers have no such routes. */
export function storesSessionPullRequests(info: ServerInfo | "loading"): boolean {
  return info !== "loading" && info.session_pull_requests === true;
}

export function sessionPullRequestsKey(conversationId: string | undefined) {
  return ["session-pull-requests", conversationId] as const;
}

async function errorMessage(response: Response): Promise<Error> {
  try {
    const body = (await response.json()) as { error?: { message?: string }; detail?: unknown };
    if (body?.error?.message) return new Error(body.error.message);
    if (typeof body?.detail === "string" && body.detail) return new Error(body.detail);
  } catch {
    // Non-JSON body (gateway error) — keep the status line.
  }
  return new Error(`${response.status} ${response.statusText}`);
}

/** Fetch all of the session's linked PRs from the server, in display order. */
export async function fetchSessionPullRequests(
  conversationId: string,
): Promise<PullRequestAssociation[]> {
  const prs: PullRequestAssociation[] = [];
  let after: string | null = null;
  // Each page needs the previous page's cursor; the server caps a session at a few pages.
  /* oxlint-disable no-await-in-loop */
  do {
    const query = new URLSearchParams({ limit: "100", ...(after ? { after } : {}) });
    const response = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(conversationId)}/pull-requests?${query}`,
    );
    if (!response.ok) throw await errorMessage(response);
    const page = (await response.json()) as SessionPullRequestList;
    prs.push(...page.data);
    after = page.has_more ? page.last_id : null;
  } while (after);
  /* oxlint-enable no-await-in-loop */
  return prs;
}

/** The session's linked PRs from the server, in display order. Never waits on the host. */
export function useSessionPullRequests(rawConversationId: string | undefined) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  const stored = storesSessionPullRequests(useServerInfo());
  return useQuery({
    queryKey: sessionPullRequestsKey(conversationId),
    queryFn: () => fetchSessionPullRequests(conversationId!),
    enabled: !!conversationId && stored,
    staleTime: 30_000,
  });
}

/**
 * Unlink a PR on the server, which works while the host is offline; the host
 * applies it with its next request. Returns the remaining PRs.
 */
export function useUnlinkSessionPr(conversationId: string) {
  const queryClient = useQueryClient();
  const stored = storesSessionPullRequests(useServerInfo());
  const infoKey = ["github-info", conversationId];
  return useMutation({
    mutationFn: async (url: string): Promise<PullRequestAssociation[]> => {
      // @deprecated server-pr-tracking compat, remove in 0.24.0 (~2026-11-24): servers
      // before 0.20.0 lack this route, so unlink through the runner or host instead.
      if (!stored) {
        return (await updateSessionPr(conversationId, { url, action: "remove" })).prs ?? [];
      }
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(conversationId)}/pull-requests?${new URLSearchParams({ url })}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw await errorMessage(response);
      return ((await response.json()) as SessionPullRequestList).data;
    },
    onSuccess: async (remaining, url) => {
      queryClient.setQueryData(sessionPullRequestsKey(conversationId), remaining);
      // A poll that started before the unlink would bring the PR back.
      await queryClient.cancelQueries({ queryKey: infoKey });
      queryClient.setQueriesData<PullRequestInfo>({ queryKey: infoKey }, (info) =>
        info?.prs
          ? {
              ...info,
              prs: info.prs.filter((pr) => pr.url !== url),
              ...(info.selected_pr_url === url ? { selected_pr_url: undefined, pr: null } : {}),
            }
          : info,
      );
      // The removed PR's own query stays put until the panel switches selection.
      queryClient.invalidateQueries({
        queryKey: infoKey,
        predicate: (query) => query.queryKey[2] !== url,
      });
    },
  });
}
