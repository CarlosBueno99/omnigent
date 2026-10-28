// What a collapsed tool-call row says about its result: a failure line, so a
// failed call is visible without expanding, and an edit's line counts.

import { stripAnsi } from "@/lib/ansi";
import type { ToolState } from "@/lib/renderItems";
import { type ShellToolOutput, parseShellToolOutput } from "@/lib/shellToolOutput";
import { shellToolCall, stripOmnigentPrefix } from "@/lib/toolTitle";
import {
  contentEnvelopeText,
  editErrorMessage,
  firstLine,
  isRecord,
  parseToolOutput,
  toolErrorMessage,
} from "@/lib/toolValue";
import { getFileEditDiff } from "@/lib/toolEditDiff";

/** A tool call as the transcript renders it. */
export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  /** Tool output, or `null` if not yet available / never produced. */
  output: string | null;
  state: ToolState;
}

/** Lines an edit added and removed. */
export interface LineCounts {
  additions: number;
  removals: number;
}

// Codex patch output leads with its outcome. Output without that line does not
// say whether the patch applied, so it gets no counts.
const CODEX_PATCH_APPLIED = "File change applied.";
// First lines of each harness's successful edit: Claude and Qwen, Claude
// MultiEdit, Omnigent's built-in Edit, Pi, OpenCode, and a new Qwen file.
const EDIT_APPLIED_RE =
  /^(?:The file:? .+ has been updated|Applied \d+ edits? to |Replaced \d+ occurrence|Successfully replaced |Edit applied successfully|Created new file: )/;
const FAILURE_MAX_CHARS = 300;

/**
 * The first line of a finished call's reported failure, or `null` when it
 * succeeded or has no output yet.
 */
export function toolFailureLine(call: ToolCall): string | null {
  if (call.output === null || call.state === "input-available") return null;
  const shellCall = shellToolCall(call.name, call.args);
  if (shellCall !== null) {
    const result = parseShellToolOutput(call.output, shellCall.outputFormat);
    // Formats without an exit code (Claude's Bash) still report failures in the text.
    if (result.exitCode === null && !result.timedOut) {
      const error = toolErrorMessage(parseToolOutput(call.output));
      if (error !== null) return failure(firstLine(error));
    }
    return shellFailure(result);
  }
  const error = toolErrorMessage(parseToolOutput(call.output));
  return error === null ? null : failure(firstLine(error));
}

/** A finished edit's failure line, including the `[error] ...` form edit tools use. */
export function fileEditFailureLine(call: ToolCall): string | null {
  if (call.output === null || call.state === "input-available") return null;
  const error = editErrorMessage(call.output);
  return error === null ? null : failure(firstLine(error));
}

/**
 * A finished edit's added and removed line counts, once its output says it
 * applied. Running, failed and unchanged edits (including pure renames) have
 * none, nor do a replace-all edit, whose arguments describe one match of an
 * unknown number, and counts that could overstate the change.
 */
export function fileEditLineCounts(call: ToolCall): LineCounts | null {
  if (call.state !== "output-available" || call.output === null) return null;
  if (fileEditFailureLine(call) !== null || !editApplied(call.name, call.output)) return null;
  const diff = getFileEditDiff(call.name, call.args);
  if (diff === null || diff.countsArePerMatch || diff.countsAreInexact) return null;
  if (diff.additions + diff.removals === 0) return null;
  return { additions: diff.additions, removals: diff.removals };
}

/**
 * Whether an edit's output says the change applied. Failures can lack a
 * recognized error prefix, so counts need this positive evidence.
 */
export function editApplied(name: string, output: string): boolean {
  const parsed = parseToolOutput(output);
  const tool = stripOmnigentPrefix(name);
  if (tool === "apply_patch") return firstLine(parsed.text) === CODEX_PATCH_APPLIED;
  if (tool === "sys_os_edit") {
    const record = parsed.kind === "json" && isRecord(parsed.value) ? parsed.value : null;
    const replacements = record?.replacements;
    return typeof replacements === "number" && replacements > 0;
  }
  const envelopeText = parsed.kind === "json" ? contentEnvelopeText(parsed.value) : null;
  return EDIT_APPLIED_RE.test(stripAnsi(envelopeText ?? parsed.text).trim());
}

function shellFailure(result: ShellToolOutput): string | null {
  if (result.timedOut) return "Timed out";
  if (result.exitCode === null || result.exitCode === 0) return null;
  // A failing command's last line is usually its error or test summary.
  const lines = result.text.split("\n").map((line) => line.trim());
  const lastLine = lines.filter((line) => line.length > 0).at(-1);
  const status = `Exit code ${result.exitCode}`;
  return failure(lastLine === undefined ? status : `${status}: ${firstLine(lastLine)}`);
}

function failure(text: string): string {
  return text.slice(0, FAILURE_MAX_CHARS);
}
