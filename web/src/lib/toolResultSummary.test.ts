import { describe, expect, it } from "vitest";
import {
  type ToolCall,
  editApplied,
  fileEditFailureLine,
  fileEditLineCounts,
  toolFailureLine,
} from "./toolResultSummary";

function failureOf(name: string, output: string | null, args: Record<string, unknown> = {}) {
  return toolFailureLine({ name, args, output, state: "output-available" });
}

describe("toolFailureLine", () => {
  it("has no line while running or before output arrives", () => {
    expect(
      toolFailureLine({ name: "my_tool", args: {}, output: null, state: "input-available" }),
    ).toBeNull();
    expect(failureOf("my_tool", null)).toBeNull();
  });

  it("has no line for a successful call of any kind", () => {
    expect(
      failureOf("Read", "     1\timport a\n     2\timport b", { file_path: "a.py" }),
    ).toBeNull();
    expect(failureOf("Grep", "Found 3 files\na.ts\nb.ts\nc.ts")).toBeNull();
    expect(failureOf("Edit", "The file a.py has been updated successfully.")).toBeNull();
    expect(failureOf("manage_todo_list", "Task plan updated (2 tasks).")).toBeNull();
    const issue = { id: "OMNI-12243", title: "Claude mod replaces tmux paste", state: "In Review" };
    const output = JSON.stringify([{ type: "text", text: JSON.stringify(issue) }]);
    expect(failureOf("mcp__linear__get_issue", output)).toBeNull();
    expect(failureOf("list_prs", "[1, 2, 3]")).toBeNull();
  });

  describe("shell tools", () => {
    it("has no line on success", () => {
      expect(failureOf("Bash", "README.md", { command: "ls" })).toBeNull();
      expect(failureOf("shell", "ok\n[exit code: 0]", { command: "make" })).toBeNull();
      // "[error] <message>" is an edit tool's failure form, not a shell's.
      expect(failureOf("Bash", "[error] recovered cache entry", { command: "cat log" })).toBeNull();
    });

    it("reports a failing exit code with the output's last line", () => {
      expect(
        failureOf("shell", "collecting...\nFAILED tests/test_a.py::test_x\n[exit code: 1]", {
          command: "pytest",
        }),
      ).toBe("Exit code 1: FAILED tests/test_a.py::test_x");
    });

    it("reports a failure from a shell format that carries no exit code", () => {
      // WHY: Claude's Bash output has no exit code, so its failures show only in the text.
      expect(
        failureOf("Bash", "<tool_use_error>Permission to use Bash was denied.</tool_use_error>", {
          command: "rm -rf build",
        }),
      ).toBe("Permission to use Bash was denied.");
      expect(failureOf("sys_os_shell", '{"error": "command blocked"}', { command: "x" })).toBe(
        "command blocked",
      );
    });

    it("reports a sys_os_shell timeout", () => {
      const output = JSON.stringify({ stdout: "", stderr: "", exit_code: null, timed_out: true });
      expect(failureOf("sys_os_shell", output, { command: "sleep 99" })).toBe("Timed out");
    });
  });

  it("reports each failure shape by its first line", () => {
    expect(failureOf("sys_os_write", '{"error": "permission denied"}')).toBe("permission denied");
    expect(
      failureOf("mcp__github__create_pull_request", "Error: 422 Validation Failed\nmore detail"),
    ).toBe("Error: 422 Validation Failed");
    expect(
      failureOf("github__create_pull_request", '{"result": "Error: 422 Validation Failed"}'),
    ).toBe("Error: 422 Validation Failed");
    expect(
      failureOf("Edit", "<tool_use_error>String to replace not found in file.</tool_use_error>"),
    ).toBe("String to replace not found in file.");
    const piFailure = {
      content: [{ type: "text", text: "Command exited with code 1" }],
      details: { truncation: null },
      isError: true,
    };
    expect(failureOf("bash", JSON.stringify(piFailure))).toBe("Command exited with code 1");
  });
});

const edit = (output: string | null, overrides: Partial<ToolCall> = {}): ToolCall => ({
  name: "Edit",
  args: { file_path: "a.py", old_string: "x = 1\ny = 2\nz = 3", new_string: "x = 10" },
  output,
  state: "output-available",
  ...overrides,
});

describe("fileEditFailureLine", () => {
  it("reports each harness's edit failure, including the [error] form", () => {
    expect(
      fileEditFailureLine(edit("<tool_use_error>String to replace not found.</tool_use_error>")),
    ).toBe("String to replace not found.");
    expect(
      fileEditFailureLine({
        name: "apply_patch",
        args: { changes: [{ path: "a.py", kind: { type: "add" }, diff: "x\n" }] },
        output: "Error: File change ended with status declined.\nadd a.py",
        state: "output-available",
      }),
    ).toBe("Error: File change ended with status declined.");
    expect(
      fileEditFailureLine({
        name: "edit",
        args: { filePath: "a.py", oldString: "x", newString: "y" },
        output: "[error] Error: oldString not found in content",
        state: "output-available",
      }),
    ).toBe("Error: oldString not found in content");
    const piFailure = { content: [{ type: "text", text: "Could not find the text in a.py." }] };
    expect(
      fileEditFailureLine({
        name: "edit",
        args: { path: "a.py", oldText: "x", newText: "y" },
        output: JSON.stringify({ ...piFailure, isError: true }),
        state: "output-available",
      }),
    ).toBe("Could not find the text in a.py.");
  });

  it("has no line for an applied or running edit", () => {
    expect(fileEditFailureLine(edit("The file a.py has been updated successfully."))).toBeNull();
    expect(fileEditFailureLine(edit(null, { state: "input-available" }))).toBeNull();
  });
});

describe("fileEditLineCounts", () => {
  it("counts an applied edit's added and removed lines", () => {
    expect(fileEditLineCounts(edit("The file a.py has been updated successfully."))).toEqual({
      additions: 1,
      removals: 3,
    });
  });

  it("totals a multi-file patch, wrapped or not", () => {
    const call: ToolCall = {
      name: "apply_patch",
      args: {
        changes: [
          { path: "a.py", kind: { type: "add" }, diff: "one\ntwo\n" },
          { path: "b.py", kind: { type: "update" }, diff: "@@ -1 +1,2 @@\n keep\n+three\n" },
        ],
      },
      output: "File change applied.\nadd a.py\nupdate b.py",
      state: "output-available",
    };
    expect(fileEditLineCounts(call)).toEqual({ additions: 3, removals: 0 });
    const wrapped = JSON.stringify([{ type: "text", text: call.output }]);
    expect(fileEditLineCounts({ ...call, output: wrapped })).toEqual({ additions: 3, removals: 0 });
  });

  it("has none for a failed edit", () => {
    expect(
      fileEditLineCounts(edit("<tool_use_error>String to replace not found.</tool_use_error>")),
    ).toBeNull();
  });

  const patchArgs = {
    changes: [{ path: "/repo/app.py", kind: { type: "update" }, diff: "@@\n-a\n+b\n" }],
  };
  const replaceArgs = {
    path: "a.py",
    oldText: "x",
    newText: "y",
    old_string: "x",
    new_string: "y",
  };
  it.each([
    { name: "apply_patch", args: patchArgs, output: "update /repo/app.py" },
    { name: "mcp__omnigent__apply_patch", args: patchArgs, output: "update /repo/app.py" },
    {
      name: "Edit",
      args: replaceArgs,
      output: "Error reading /repo/missing.py: [Errno 2] No such file or directory",
    },
    { name: "edit", args: replaceArgs, output: "Could not find the exact text in a.py." },
  ])("has none for $name output that does not say it applied: $output", (overrides) => {
    expect(fileEditLineCounts(edit(overrides.output, overrides))).toBeNull();
  });

  it("has none for a replace-all edit, whose output does not say how many matched", () => {
    const output = "The file a.py has been updated. All occurrences were successfully replaced.";
    const replaceAll = edit(output, {
      args: { file_path: "a.py", old_string: "x", new_string: "y", replace_all: true },
    });
    expect(fileEditLineCounts(replaceAll)).toBeNull();
    const batch = edit("Applied 2 edits to a.py:", {
      name: "MultiEdit",
      args: {
        file_path: "a.py",
        edits: [
          { old_string: "x", new_string: "y", replace_all: true },
          { old_string: "z", new_string: "w" },
        ],
      },
    });
    expect(fileEditLineCounts(batch)).toBeNull();
  });

  it("has none for an edit too large to compare line by line", () => {
    const shared = Array.from({ length: 500 }, (_, index) => `line ${index}`);
    const large = edit("The file a.py has been updated successfully.", {
      args: {
        file_path: "a.py",
        old_string: [...shared, "old end"].join("\n"),
        new_string: ["new start", ...shared].join("\n"),
      },
    });
    expect(fileEditLineCounts(large)).toBeNull();
  });

  it("has none for a patch that only renames a file", () => {
    const call: ToolCall = {
      name: "apply_patch",
      args: {
        changes: [{ path: "a.py", kind: { type: "update", move_path: "b.py" }, diff: "" }],
      },
      output: "File change applied.\nupdate a.py",
      state: "output-available",
    };
    expect(fileEditLineCounts(call)).toBeNull();
  });

  it("has none while running, after a cancelled or failed turn, or without a change", () => {
    expect(fileEditLineCounts(edit(null, { state: "input-available" }))).toBeNull();
    expect(fileEditLineCounts(edit(null, { state: "cancelled" }))).toBeNull();
    expect(fileEditLineCounts(edit("done", { state: "output-error" }))).toBeNull();
    expect(fileEditLineCounts(edit("done", { args: { file_path: "a.py" } }))).toBeNull();
  });
});

describe("editApplied", () => {
  it.each([
    ["Edit", "The file /repo/a.py has been updated successfully."],
    ["Edit", "The file /repo/a.py has been updated. All occurrences were successfully replaced."],
    ["Edit", "Replaced 1 occurrence(s) in /repo/a.py"],
    ["MultiEdit", "Applied 2 edits to /repo/a.py:\n1. Replaced ..."],
    ["edit", "Successfully replaced 1 block(s) in a.py."],
    ["edit", "Successfully replaced text in a.py."],
    ["edit", "Edit applied successfully.\n\nLSP errors detected"],
    ["edit", "The file: /repo/a.py has been updated."],
    ["edit", "Created new file: /repo/a.py with provided content."],
    ["mcp__omnigent__sys_os_edit", '{"path": "/repo/a.py", "replacements": 1, "bytes_written": 9}'],
    ["apply_patch", "File change applied.\nupdate /repo/a.py"],
    // SDK Pi stores its own tools' results whole.
    [
      "edit",
      `{"content": [{"type": "text", "text": "Successfully replaced text in a.py."}], "details": {"diff": "-1 x\\n+1 y", "firstChangedLine": 1}}`,
    ],
  ])("accepts %s output %j", (name, output) => {
    expect(editApplied(name, output)).toBe(true);
  });

  it.each([
    ["Edit", "ok"],
    ["edit", "Could not find the exact text in a.py."],
    ["sys_os_edit", '{"path": "/repo/a.py", "replacements": 0}'],
    ["sys_os_edit", "Replaced 1 occurrence(s) in /repo/a.py"],
    ["apply_patch", "update /repo/a.py"],
    ["apply_patch", "Successfully replaced text in a.py."],
    [
      "edit",
      '{"content": [{"type": "text", "text": "Successfully replaced text in a.py."}], "isError": true}',
    ],
    ["edit", '{"content": [{"type": "text", "text": "Could not find the exact text in a.py."}]}'],
  ])("rejects %s output %j", (name, output) => {
    expect(editApplied(name, output)).toBe(false);
  });
});
