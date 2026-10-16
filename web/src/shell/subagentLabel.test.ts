import { describe, expect, it } from "vitest";

import type { ChildSessionInfo } from "@/hooks/useChildSessions";

import { childPrimaryLabel } from "./subagentLabel";

// An orchestrator names a worker via sys_session_send(title=...), so the child
// carries that handle (session_name and the title suffix) beside a generated
// task_summary; the shared label must show both so every surface names it alike.

const HANDLE = "deploy-profile-page";
const SUMMARY = "Deploy PR41 via web ops";

function child(overrides: Partial<ChildSessionInfo> = {}): ChildSessionInfo {
  return {
    id: "child-1",
    title: `worker:${HANDLE}`,
    task_summary: SUMMARY,
    tool: "worker",
    session_name: HANDLE,
    labels: {},
    current_task_status: "completed",
    last_task_error: null,
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    routed_model: null,
    ...overrides,
  };
}

describe("childPrimaryLabel", () => {
  it("leads with the summary and follows it with the handle", () => {
    expect(childPrimaryLabel(child())).toBe(`${SUMMARY} · ${HANDLE}`);
  });

  it("keeps the handle alone until a summary is generated", () => {
    expect(childPrimaryLabel(child({ task_summary: null }))).toBe(HANDLE);
  });

  it("takes the handle from the title suffix when session_name is missing", () => {
    expect(childPrimaryLabel(child({ session_name: null }))).toBe(`${SUMMARY} · ${HANDLE}`);
  });

  it("shows the summary alone when the row carries no handle", () => {
    expect(childPrimaryLabel(child({ session_name: null, title: null }))).toBe(SUMMARY);
  });

  it("does not repeat a handle that equals the summary", () => {
    expect(childPrimaryLabel(child({ task_summary: HANDLE }))).toBe(HANDLE);
  });

  it("treats whitespace-only fields as missing", () => {
    expect(childPrimaryLabel(child({ task_summary: "   " }))).toBe(HANDLE);
    expect(childPrimaryLabel(child({ session_name: " ", title: " " }))).toBe(SUMMARY);
  });

  it("uses the caller's fallback for a row with no name at all", () => {
    const nameless = child({ task_summary: null, session_name: null, title: null, tool: null });
    expect(childPrimaryLabel(nameless)).toBe("child-1");
    expect(childPrimaryLabel(nameless, "Sub-agent")).toBe("Sub-agent");
  });

  it("labels native sub-agents by their resolved tool, not the correlation id", () => {
    const native = child({
      title: "general-purpose:a09d1dd1d8dbc0151",
      session_name: "a09d1dd1d8dbc0151",
      tool: "Investigate flaky auth test",
      labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
    });
    expect(childPrimaryLabel(native)).toBe("Investigate flaky auth test");
  });
});
