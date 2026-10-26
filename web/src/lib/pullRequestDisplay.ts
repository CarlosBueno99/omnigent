// Display helpers shared by every surface that names a session's pull requests:
// the PR panel's picker and strip, and the sidebar session tooltip.

import {
  GitMergeIcon,
  GitPullRequestClosedIcon,
  GitPullRequestDraftIcon,
  GitPullRequestIcon,
  type LucideIcon,
} from "lucide-react";
import type { PullRequest, PullRequestAssociation } from "@/hooks/usePullRequests";
import { gitProviderCopy } from "@/lib/gitProviders";

/** GitHub's glyph, label and tint for a PR state; gh reports a draft as OPEN + is_draft. */
export function pullRequestStatus(pr: Pick<PullRequest, "state" | "is_draft">): {
  icon: LucideIcon;
  label: string;
  className: string;
} {
  const state = pr.state.toUpperCase();
  if (state === "MERGED") {
    return {
      icon: GitMergeIcon,
      label: "Merged",
      className: "text-purple-600 dark:text-purple-400",
    };
  }
  if (state === "CLOSED") {
    return {
      icon: GitPullRequestClosedIcon,
      label: "Closed",
      className: "text-red-600 dark:text-red-400",
    };
  }
  if (pr.is_draft) {
    return { icon: GitPullRequestDraftIcon, label: "Draft", className: "text-muted-foreground" };
  }
  return {
    icon: GitPullRequestIcon,
    label: "Open",
    className: "text-green-600 dark:text-green-400",
  };
}

/** A PR's picker label, in its own provider's copy (else the session's). */
export function pullRequestLabel(
  pr: PullRequestAssociation,
  sessionProvider?: string | null,
): string {
  const copy = gitProviderCopy(pr.provider ?? sessionProvider, undefined, pr.provider_display);
  const host = pr.host === copy.defaultHost ? "" : `${pr.host}/`;
  const inferred = pr.relationship === "inferred" ? " (from branch)" : "";
  const identity = `${host}${pr.repository} ${copy.prNumberPrefix}${pr.number}${inferred}`;
  const title = pr.title?.trim();
  return title ? `${identity} — ${title}` : identity;
}
