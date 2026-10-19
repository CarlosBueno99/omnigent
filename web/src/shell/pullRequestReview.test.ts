import { describe, expect, it } from "vitest";
import { parsePatchFiles } from "@pierre/diffs";
import { patchLines, readViewedFiles, pullRequestFileUrl } from "./pullRequestReview";
import type { PullRequest } from "@/hooks/usePullRequests";

describe("PR review", () => {
  it("searches fetched context, additions and deletions with real line numbers", () => {
    const patch =
      "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -10,2 +10,2 @@\n same\n-old\n+new\n";
    expect(patchLines(parsePatchFiles(patch).flatMap((part) => part.files))).toEqual([
      { path: "a.ts", line: 10, side: "additions", text: "same\n" },
      { path: "a.ts", line: 11, side: "deletions", text: "old\n" },
      { path: "a.ts", line: 11, side: "additions", text: "new\n" },
    ]);
  });
  it("validates persisted viewed paths and handles corrupted storage", () => {
    localStorage.setItem("viewed-test", "bad");
    expect([...readViewedFiles("viewed-test")]).toEqual([]);
    localStorage.setItem("viewed-test", JSON.stringify(["a.ts", 1]));
    expect([...readViewedFiles("viewed-test")]).toEqual(["a.ts"]);
    localStorage.removeItem("viewed-test");
  });
  it("opens the fork's exact head and encodes file paths", () => {
    const pr = {
      head_sha: "abc",
      head_repository_url: "https://github.com/fork/repo",
    } as PullRequest;
    expect(pullRequestFileUrl(pr, "github", "src/a #.ts")).toBe(
      "https://github.com/fork/repo/blob/abc/src/a%20%23.ts",
    );
    expect(pullRequestFileUrl({ ...pr, head_sha: undefined }, "github", "a.ts")).toBeUndefined();
  });
});
