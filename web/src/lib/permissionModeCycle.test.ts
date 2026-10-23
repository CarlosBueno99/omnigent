import { describe, expect, it } from "vitest";

import {
  CLAUDE_NATIVE_PERMISSION_MODES,
  CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
} from "./claudePermissionMode";
import { CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS } from "./codexApprovalMode";
import { codexCreateApprovalOptions } from "./codexApprovalOptions";
import {
  AGY_NATIVE_SKIP_MODES,
  CURSOR_NATIVE_EXEC_MODES,
  DEVIN_NATIVE_PERMISSION_MODES,
} from "./nativeHarnessModes";
import { isPermissionModeCycleKey, nextCycledPermissionMode } from "./permissionModeCycle";

const key = (overrides: Partial<Parameters<typeof isPermissionModeCycleKey>[0]> = {}) => ({
  key: "Tab",
  shiftKey: true,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  ...overrides,
});

describe("isPermissionModeCycleKey", () => {
  it("matches only a bare Shift+Tab", () => {
    expect(isPermissionModeCycleKey(key())).toBe(true);
    expect(isPermissionModeCycleKey(key({ shiftKey: false }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ ctrlKey: true }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ metaKey: true }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ altKey: true }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ key: "Enter" }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ nativeEvent: { isComposing: true } }))).toBe(false);
  });
});

/** Walk the cycle from *start* and return the modes visited, in order. */
function lap(harness: string, options: readonly { value: string }[], start: string | null) {
  const visited: (string | null)[] = [start];
  let current = start;
  for (let press = 0; press < options.length; press += 1) {
    current = nextCycledPermissionMode(harness, options, current);
    visited.push(current);
  }
  return visited;
}

describe("nextCycledPermissionMode", () => {
  it("walks a running Claude session's modes in menu order and wraps", () => {
    expect(lap("claude-native", CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES, "default")).toEqual([
      "default",
      "auto",
      "acceptEdits",
      "plan",
      "default",
    ]);
  });

  it("never lands on a full-access mode", () => {
    expect(lap("claude-native", CLAUDE_NATIVE_PERMISSION_MODES, "default")).not.toContain(
      "bypassPermissions",
    );
    expect(lap("codex-native", codexCreateApprovalOptions(), "default")).toEqual([
      "default",
      "read-only",
      "default",
      "read-only",
      "default",
    ]);
    expect(lap("codex-native", CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS, "ask-for-approval")).toEqual([
      "ask-for-approval",
      "approve-for-me",
      "ask-for-approval",
      "approve-for-me",
    ]);
    expect(lap("cursor-native", CURSOR_NATIVE_EXEC_MODES, "default")).not.toContain("yolo");
    expect(lap("devin-native", DEVIN_NATIVE_PERMISSION_MODES, "normal")).not.toContain("dangerous");
  });

  it("leaves a full-access mode for the next safe mode in the menu", () => {
    expect(
      nextCycledPermissionMode(
        "claude-native",
        CLAUDE_NATIVE_PERMISSION_MODES,
        "bypassPermissions",
      ),
    ).toBe("default");
    expect(nextCycledPermissionMode("antigravity-native", AGY_NATIVE_SKIP_MODES, "skip")).toBe(
      "default",
    );
  });

  it("starts from the top of the menu when the current mode is unknown", () => {
    expect(
      nextCycledPermissionMode("codex-native", CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS, null),
    ).toBe("ask-for-approval");
    expect(
      nextCycledPermissionMode("codex-native", CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS, "read-only"),
    ).toBe("ask-for-approval");
  });

  it("skips modes the session refused", () => {
    expect(
      nextCycledPermissionMode(
        "claude-native",
        CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
        "default",
        new Set(["auto"]),
      ),
    ).toBe("acceptEdits");
  });

  it("returns null when no other safe mode exists", () => {
    expect(nextCycledPermissionMode("antigravity-native", AGY_NATIVE_SKIP_MODES, "default")).toBe(
      null,
    );
    expect(nextCycledPermissionMode("claude-native", [], "default")).toBe(null);
  });
});
