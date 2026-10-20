import type * as UseTerminalsModule from "@/hooks/useTerminals";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { useSessionWorkspace, useWorkspaceResources } from "./useSessionWorkspace";

const resources = vi.hoisted(() => ({
  environment: vi.fn(),
  changes: vi.fn(),
  terminals: vi.fn(),
  remove: vi.fn(),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: resources.environment,
  useWorkspaceChangedFiles: resources.changes,
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: resources.terminals,
  useDeleteTerminal: () => ({ mutate: resources.remove }),
}));
afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.resetAllMocks();
});

it("isolates persisted tabs across session switches and chains file guards", () => {
  writeSessionWorkspaceState("first", { openFiles: ["one.md"], selectedFilePath: "one.md" });
  writeSessionWorkspaceState("second", {
    openFiles: ["two.md"],
    selectedTerminalKey: "terminal:shell",
  });
  const { result, rerender } = renderHook(({ id }) => useSessionWorkspace(id), {
    initialProps: { id: "first" },
  });
  act(() => result.current.setOpenFiles((files) => [...files, "draft.md"]));
  rerender({ id: "second" });
  expect(result.current.openFiles).toEqual(["two.md"]);
  expect(result.current.selectedTerminalKey).toBe("terminal:shell");
  expect(readSessionWorkspaceState("first").openFiles).toEqual(["one.md", "draft.md"]);
  const applied = vi.fn();
  const first = vi.fn((_path, _options, proceed) => proceed());
  const second = vi.fn();
  const unregister = result.current.registerNavigationGuard(first);
  const unblock = result.current.registerNavigationGuard(second);
  result.current.navigateWorkspace("next.md", { line: 4 }, applied);
  expect(first).toHaveBeenCalledWith("next.md", { line: 4 }, expect.any(Function));
  expect(second).toHaveBeenCalledOnce();
  expect(applied).not.toHaveBeenCalled();
  unblock();
  unregister();
  result.current.navigateWorkspace("next.md", undefined, applied);
  expect(applied).toHaveBeenCalledOnce();
  act(() => result.current.selectRightRailTab("github"));
  expect(readSessionWorkspaceState("second").rightRailTab).toBe("github");
  rerender({ id: "first" });
  expect(result.current.openFiles).toEqual(["one.md", "draft.md"]);
});

it("gates workspace reads and keeps agent terminals separate from user shells", () => {
  resources.environment.mockReturnValue({ data: { available: true } });
  resources.changes.mockReturnValue({ data: { data: [{ path: "changed.md" }] } });
  resources.terminals.mockReturnValue({
    terminals: [
      { id: "terminal_codex_main", name: "codex", session: "main", running: true },
      { id: "one", name: "sh", session: "one", running: true },
      { id: "two", name: "sh", session: "two", running: true },
    ],
  });
  const { result, rerender } = renderHook(
    ({ id, shared }) => {
      const workspace = useSessionWorkspace(id);
      const resource = useWorkspaceResources(
        id,
        {
          permissionLevel: 1,
          shareWorkspaceFiles: shared,
          terminalFirst: true,
          terminalPending: true,
        },
        workspace.setSelectedTerminalKey,
      );
      return { workspace, resource };
    },
    { initialProps: { id: "first", shared: false } },
  );
  expect(resources.environment).toHaveBeenLastCalledWith("first", { enabled: false });
  expect(resources.changes).toHaveBeenLastCalledWith("first", { enabled: false });
  expect(result.current.resource.showFilesPanel).toBe(false);
  expect(resources.terminals).toHaveBeenLastCalledWith("first", { reconcileWhilePending: true });
  expect(result.current.resource.agentTerminal?.id).toBe("terminal_codex_main");
  expect(result.current.resource.openTerminals).toEqual(["terminal:one", "terminal:two"]);
  act(() => {
    result.current.workspace.setSelectedTerminalKey("terminal:one");
    result.current.resource.setTerminalPendingClose("terminal:one");
  });
  act(() => result.current.resource.confirmCloseTerminal());
  expect(resources.remove).toHaveBeenCalledWith("one");
  expect(result.current.workspace.selectedTerminalKey).toBe("terminal:two");
  act(() => result.current.resource.setTerminalPendingClose("terminal:one"));
  act(() => result.current.resource.confirmCloseTerminal());
  expect(result.current.workspace.selectedTerminalKey).toBe("terminal:two");
  act(() => result.current.resource.setTerminalPendingClose("terminal:two"));
  rerender({ id: "second", shared: true });
  expect(result.current.resource.terminalPendingClose).toBeNull();
  expect(resources.environment).toHaveBeenLastCalledWith("second", { enabled: true });
  expect(resources.changes).toHaveBeenLastCalledWith("second", { enabled: true });
  expect(result.current.resource.isChangedPath("changed.md")).toBe(true);
});
