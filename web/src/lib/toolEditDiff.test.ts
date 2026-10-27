import { describe, expect, it } from "vitest";
import { getFileEditDiff } from "./toolEditDiff";

describe("getFileEditDiff", () => {
  it("diffs a Claude Edit's old and new strings line by line", () => {
    const diff = getFileEditDiff("Edit", {
      file_path: "/repo/config.py",
      old_string: "HOST = 'localhost'\nTIMEOUT = 30\nRETRIES = 3\n",
      new_string: "HOST = 'localhost'\nTIMEOUT = 60\nRETRIES = 3\n",
    });
    expect(diff).toEqual({
      text: " HOST = 'localhost'\n-TIMEOUT = 30\n+TIMEOUT = 60\n RETRIES = 3",
      additions: 1,
      removals: 1,
      fileCount: 1,
    });
  });

  it("does not count unchanged lines between two changes", () => {
    // WHY: trimming only the common prefix and suffix would count "b" as removed and re-added.
    const diff = getFileEditDiff("edit", { path: "x", oldText: "a\nb\nc", newText: "A\nb\nC" });
    expect(diff?.text).toBe("-a\n+A\n b\n-c\n+C");
    expect(diff).toMatchObject({ additions: 2, removals: 2 });
  });

  it("collapses a long unchanged run between changes into a gap", () => {
    const middle = ["1", "2", "3", "4", "5", "6"];
    const diff = getFileEditDiff("Edit", {
      old_string: ["a", ...middle, "z"].join("\n"),
      new_string: ["A", ...middle, "Z"].join("\n"),
    });
    expect(diff?.text).toBe("-a\n+A\n 1\n 2\n@@\n 5\n 6\n-z\n+Z");
  });

  it("joins each edit of a MultiEdit or sys_os_edit call as its own hunk", () => {
    const edits = [
      { old_string: "one", new_string: "uno" },
      { old_string: "two", new_string: "two" },
      { old_string: "three\n", new_string: "three\nfour\n" },
    ];
    expect(getFileEditDiff("MultiEdit", { file_path: "a.txt", edits })).toEqual({
      text: "@@\n-one\n+uno\n@@\n three\n+four",
      additions: 2,
      removals: 1,
      fileCount: 1,
    });
    const omnigentEdits = [{ oldText: "x = 1", newText: "x = 2" }];
    expect(
      getFileEditDiff("mcp__omnigent__sys_os_edit", { path: "a.py", edits: omnigentEdits }),
    ).toMatchObject({ text: "-x = 1\n+x = 2", additions: 1, removals: 1 });
  });

  it("reads OpenCode's camel-case keys and counts a pure insertion", () => {
    expect(
      getFileEditDiff("edit", { filePath: "a.ts", oldString: "", newString: "line 1\nline 2\n" }),
    ).toMatchObject({ text: "+line 1\n+line 2", additions: 2, removals: 0 });
  });

  it("returns null for a no-op, malformed, or non-edit call", () => {
    expect(getFileEditDiff("Edit", { old_string: "same", new_string: "same" })).toBeNull();
    expect(getFileEditDiff("Edit", { file_path: "a.py" })).toBeNull();
    expect(getFileEditDiff("Write", { file_path: "a.py", content: "x" })).toBeNull();
    expect(getFileEditDiff("mcp__linear__edit", { oldText: "a", newText: "b" })).toBeNull();
  });

  it("counts every file of a Codex apply_patch, by change kind", () => {
    const diff = getFileEditDiff("apply_patch", {
      changes: [
        {
          path: "/repo/app.py",
          kind: { type: "update", move_path: null },
          diff: "@@ -1,3 +1,3 @@\n import os\n-DEBUG = True\n+DEBUG = False\n print(os.name)\n",
        },
        { path: "/repo/new.py", kind: { type: "add" }, diff: "print('hi')\nprint('bye')\n" },
        { path: "/repo/old.py", kind: { type: "delete" }, diff: "gone\n" },
      ],
    });
    expect(diff).toMatchObject({ additions: 3, removals: 2, fileCount: 3 });
    expect(diff?.text).toBe(
      [
        "--- /repo/app.py",
        "+++ /repo/app.py",
        "@@ -1,3 +1,3 @@",
        " import os",
        "-DEBUG = True",
        "+DEBUG = False",
        " print(os.name)",
        "--- /repo/new.py",
        "+++ /repo/new.py",
        "+print('hi')",
        "+print('bye')",
        "--- /repo/old.py",
        "+++ /repo/old.py",
        "-gone",
      ].join("\n"),
    );
  });

  it("skips an update diff's own file headers when counting", () => {
    const diff = getFileEditDiff("apply_patch", {
      changes: [
        {
          path: "a.txt",
          kind: { type: "update" },
          diff: "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+y\n",
        },
      ],
    });
    expect(diff).toMatchObject({ additions: 1, removals: 1, fileCount: 1 });
  });

  it("returns null for an apply_patch without usable changes", () => {
    expect(getFileEditDiff("apply_patch", {})).toBeNull();
    expect(getFileEditDiff("apply_patch", { changes: [{ kind: { type: "add" } }] })).toBeNull();
  });
});
