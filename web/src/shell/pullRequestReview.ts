import type { FileDiffMetadata } from "@pierre/diffs";
import type { PullRequest } from "@/hooks/usePullRequests";
import { getOmnigentServerIdentity } from "@/lib/host";

export interface PatchLine {
  path: string;
  line: number;
  side: "additions" | "deletions";
  text: string;
}

/** Snapshot only fetched hunks before the renderer hydrates unchanged context. */
export function patchLines(files: Iterable<FileDiffMetadata>): PatchLine[] {
  const lines: PatchLine[] = [];
  for (const file of files) {
    for (const hunk of file.hunks) {
      for (const content of hunk.hunkContent) {
        for (const side of ["deletions", "additions"] as const) {
          const count =
            content.type === "context" ? (side === "additions" ? content.lines : 0) : content[side];
          const start =
            side === "additions" ? content.additionLineIndex : content.deletionLineIndex;
          const hunkIndex = side === "additions" ? hunk.additionLineIndex : hunk.deletionLineIndex;
          const line = side === "additions" ? hunk.additionStart : hunk.deletionStart;
          const source = side === "additions" ? file.additionLines : file.deletionLines;
          for (let offset = 0; offset < count; offset++) {
            lines.push({
              path: file.name,
              line: line + start - hunkIndex + offset,
              side,
              text: source[start + offset],
            });
          }
        }
      }
    }
  }
  return lines;
}

export function prLocalKey(
  kind: string,
  user: string | null,
  url: string | undefined,
  revision = "",
): string | null {
  const server = getOmnigentServerIdentity();
  return user && url && server
    ? `omnigent:pr:${kind}:v1:${JSON.stringify([server, user, url, revision])}`
    : null;
}

export function readViewedFiles(key: string | null): Set<string> {
  try {
    const value: unknown = key ? JSON.parse(localStorage.getItem(key) ?? "[]") : [];
    return new Set(
      Array.isArray(value) ? value.filter((path): path is string => typeof path === "string") : [],
    );
  } catch {
    return new Set();
  }
}

export function pullRequestFileUrl(
  pr: PullRequest,
  provider: string | null,
  path: string,
): string | undefined {
  if (!pr.head_sha || !pr.head_repository_url?.startsWith("https://")) return;
  const file = path.split("/").map(encodeURIComponent).join("/");
  const sha = encodeURIComponent(pr.head_sha);
  if (provider === "github") return `${pr.head_repository_url}/blob/${sha}/${file}`;
  if (provider === "gitlab") return `${pr.head_repository_url}/-/blob/${sha}/${file}`;
  if (provider === "azure_devops")
    return `${pr.head_repository_url}?${new URLSearchParams({ path: `/${path}`, version: `GC${pr.head_sha}` })}`;
  return undefined;
}
