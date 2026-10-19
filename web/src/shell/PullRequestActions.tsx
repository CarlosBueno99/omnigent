import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { useSession } from "@/hooks/useSession";
import type { PullRequest } from "@/hooks/usePullRequests";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { MessageResponse } from "@/components/ai-elements/message";
import { prLocalKey } from "./pullRequestReview";

interface Draft {
  title?: string;
  body?: string;
}
interface WriteAccess {
  actor: string;
  can_edit: boolean;
  can_request_reviewers: boolean;
}

function readDraft(key: string | null): Draft {
  try {
    const value = key ? JSON.parse(localStorage.getItem(key) ?? "{}") : {};
    return {
      title: typeof value.title === "string" ? value.title : undefined,
      body: typeof value.body === "string" ? value.body : undefined,
    };
  } catch {
    return {};
  }
}

async function responseData(response: Response) {
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message ?? body.detail ?? "GitHub action failed");
  return body;
}

export function PullRequestActions({
  conversationId,
  pr,
  viewer,
}: {
  conversationId: string;
  pr: PullRequest;
  viewer: string | null;
}) {
  const { session } = useSession(conversationId);
  const owner = !!session && isOwnerLevel(session.permissionLevel ?? null);
  const client = useQueryClient();
  const base = `/v1/sessions/${encodeURIComponent(conversationId)}/resources/github`;
  const access = useQuery<WriteAccess>({
    queryKey: ["pr-write-access", conversationId, pr.url, viewer],
    queryFn: async () =>
      responseData(
        await authenticatedFetch(`${base}/write-access?${new URLSearchParams({ pr_url: pr.url })}`),
      ),
    enabled: owner,
    retry: false,
    staleTime: 30_000,
  });
  const key = prLocalKey("draft", viewer, pr.url);
  const [draft, setDraft] = useState(() => readDraft(key));
  const [editing, setEditing] = useState<keyof Draft | null>(() =>
    draft.title !== undefined ? "title" : draft.body !== undefined ? "body" : null,
  );
  const [preview, setPreview] = useState(false);
  const [storageError, setStorageError] = useState(false);
  const [reviewer, setReviewer] = useState("");
  const [message, setMessage] = useState("");
  const changeDraft = (next: Draft) => {
    setDraft(next);
    try {
      if (!key) throw new Error("Identity unavailable");
      localStorage.setItem(key, JSON.stringify(next));
      setStorageError(false);
    } catch {
      setStorageError(true);
    }
  };
  const mutation = useMutation({
    retry: false,
    mutationFn: async (changes: Draft & { reviewer?: string }) =>
      responseData(
        await authenticatedFetch(`${base}/edit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pr_url: pr.url, expected_actor: access.data?.actor, ...changes }),
        }),
      ),
    onSuccess: (_, changes) => {
      if (changes.reviewer) {
        setReviewer("");
        setMessage("Reviewer requested.");
      } else {
        const next = { ...draft };
        if (changes.title !== undefined) delete next.title;
        if (changes.body !== undefined) delete next.body;
        changeDraft(next);
        setEditing(null);
        setMessage("PR updated.");
      }
      void client.invalidateQueries({ queryKey: ["github-info", conversationId] });
      void client.invalidateQueries({ queryKey: ["session-prs", conversationId] });
    },
  });
  if (!owner)
    return (
      <p className="text-xs text-muted-foreground">
        Only the session and host owner can edit this PR or request reviewers.
      </p>
    );
  return (
    <section
      aria-label="Manage pull request"
      className="space-y-2 border-b border-border p-3 text-ui"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span>
          {access.data
            ? `Acting as @${access.data.actor} (host GitHub account)`
            : "Checking GitHub access…"}
        </span>
        <Button
          size="sm"
          variant="ghost"
          disabled={access.isFetching || mutation.isPending}
          onClick={() => void access.refetch()}
        >
          Refresh access
        </Button>
      </div>
      {access.error && <p role="alert">{access.error.message}</p>}
      {access.data && !access.data.can_edit && (
        <p>This GitHub account does not have permission to edit the PR.</p>
      )}
      <div className="flex gap-2">
        {(["title", "body"] as const).map((field) => (
          <Button
            key={field}
            size="sm"
            variant="outline"
            disabled={!access.data?.can_edit || mutation.isPending}
            onClick={() => {
              setEditing(field);
              setPreview(false);
              setMessage("");
              mutation.reset();
            }}
          >
            Edit {field === "body" ? "description" : "title"}
          </Button>
        ))}
      </div>
      {editing && (
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault();
            const value = draft[editing] ?? (editing === "title" ? pr.title : (pr.body ?? ""));
            mutation.mutate({ [editing]: value });
          }}
        >
          {editing === "title" ? (
            <Input
              aria-label="PR title"
              maxLength={256}
              required
              disabled={mutation.isPending}
              value={draft.title ?? pr.title}
              onChange={(event) => changeDraft({ ...draft, title: event.target.value })}
            />
          ) : preview ? (
            <MessageResponse mode="static">{draft.body ?? pr.body ?? ""}</MessageResponse>
          ) : (
            <Textarea
              aria-label="PR description"
              maxLength={65536}
              disabled={mutation.isPending}
              value={draft.body ?? pr.body ?? ""}
              onChange={(event) => changeDraft({ ...draft, body: event.target.value })}
            />
          )}
          {editing === "body" && (
            <Button type="button" size="sm" variant="ghost" onClick={() => setPreview(!preview)}>
              {preview ? "Write" : "Preview"}
            </Button>
          )}
          <div className="flex gap-2">
            <Button
              type="submit"
              disabled={
                mutation.isPending ||
                !access.data?.can_edit ||
                draft[editing] === undefined ||
                draft[editing] === (editing === "title" ? pr.title : (pr.body ?? ""))
              }
            >
              {mutation.isPending ? "Saving…" : "Save"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={mutation.isPending}
              onClick={() => {
                const next = { ...draft };
                next[editing] = undefined;
                changeDraft(next);
                setEditing(null);
                mutation.reset();
              }}
            >
              Cancel
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">Unsaved edits are kept on this device.</p>
        </form>
      )}
      {storageError && (
        <p role="alert">
          Could not save the draft on this device. Keep this panel open until you save.
        </p>
      )}
      <form
        className="flex flex-wrap gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          setMessage("");
          mutation.mutate({ reviewer: reviewer.trim() });
        }}
      >
        <Input
          className="min-w-24 flex-1"
          aria-label="Reviewer username"
          placeholder="Exact GitHub username"
          value={reviewer}
          onChange={(event) => setReviewer(event.target.value)}
          disabled={!access.data?.can_request_reviewers || mutation.isPending}
          required
          maxLength={39}
        />
        <Button
          type="submit"
          disabled={!access.data?.can_request_reviewers || mutation.isPending || !reviewer.trim()}
        >
          {mutation.isPending ? "Submitting…" : "Request reviewer"}
        </Button>
      </form>
      {mutation.error && <p role="alert">{mutation.error.message}</p>}
      {message && <p role="status">{message}</p>}
    </section>
  );
}
