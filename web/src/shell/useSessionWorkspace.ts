import { useCallback, useEffect, useMemo, useRef, useState, type SetStateAction } from "react";
import {
  findAgentTerminal,
  inventoryTerminals,
  terminalTabKey,
  useDeleteTerminal,
  useTerminals,
} from "@/hooks/useTerminals";
import {
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
} from "@/hooks/useWorkspaceChangedFiles";
import { isEditorLevel } from "@/lib/permissionsApi";
import {
  readSessionWorkspaceState,
  writeSessionWorkspaceState,
  type SessionWorkspaceState,
} from "@/lib/sessionWorkspaceState";
import {
  isDefaultWorkspaceTab,
  readInitialWorkspaceTab,
  readPinnedWorkspaceTabs,
} from "@/lib/workspaceTabPreferences";
import type { FileNavigationGuard, OpenFileOptions } from "./FileViewerContext";
import type { RightRailTab } from "./railTabs";

type WorkspaceTabs = Required<
  Pick<
    SessionWorkspaceState,
    "rightRailTab" | "openPanels" | "openFiles" | "selectedFilePath" | "selectedTerminalKey"
  >
>;

function readTabs(sessionId: string | undefined): WorkspaceTabs {
  const saved = sessionId ? readSessionWorkspaceState(sessionId) : {};
  return {
    rightRailTab: sessionId ? (saved.rightRailTab ?? readInitialWorkspaceTab()) : null,
    openPanels: saved.openPanels ?? [],
    openFiles: saved.openFiles ?? [],
    selectedFilePath: saved.selectedFilePath ?? null,
    selectedTerminalKey: saved.selectedTerminalKey ?? null,
  };
}

/** Session-owned tabs and guarded navigation; layout and URL state belong to the host. */
export function useSessionWorkspace(sessionId: string | undefined) {
  const [state, setState] = useState(() => ({ sessionId, tabs: readTabs(sessionId) }));
  // Reset before committing, so an outgoing session cannot overwrite the incoming one's tabs.
  if (state.sessionId !== sessionId) setState({ sessionId, tabs: readTabs(sessionId) });
  const persisted = useRef(state);
  useEffect(() => {
    const previous = persisted.current;
    persisted.current = state;
    // Let the host apply URL/default overrides before writing a restored session.
    if (previous !== state && previous.sessionId === state.sessionId && state.sessionId) {
      writeSessionWorkspaceState(state.sessionId, state.tabs);
    }
  }, [state]);
  const setters = useMemo(() => {
    const setter =
      <K extends keyof WorkspaceTabs>(key: K) =>
      (value: SetStateAction<WorkspaceTabs[K]>) => {
        setState((previous) => {
          const next = typeof value === "function" ? value(previous.tabs[key]) : value;
          return Object.is(next, previous.tabs[key])
            ? previous
            : {
                ...previous,
                tabs: { ...previous.tabs, [key]: next },
              };
        });
      };
    return {
      setRightRailTab: setter("rightRailTab"),
      setOpenPanels: setter("openPanels"),
      setOpenFiles: setter("openFiles"),
      setSelectedFilePath: setter("selectedFilePath"),
      setSelectedTerminalKey: setter("selectedTerminalKey"),
    };
  }, []);
  const selectRightRailTab = useCallback(
    (next: RightRailTab | null) => {
      setters.setRightRailTab(next);
      if (isDefaultWorkspaceTab(next) && !readPinnedWorkspaceTabs().includes(next)) {
        setters.setOpenPanels((previous) =>
          previous.includes(next) ? previous : [...previous, next],
        );
      }
    },
    [setters],
  );
  const guards = useRef(new Set<FileNavigationGuard>());
  const registerNavigationGuard = useCallback((guard: FileNavigationGuard) => {
    guards.current.add(guard);
    return () => {
      guards.current.delete(guard);
    };
  }, []);
  const navigateWorkspace = useCallback(
    (path: string, options: OpenFileOptions | undefined, apply: () => void) => {
      const pending = [...guards.current];
      const proceed = (index: number) => {
        const guard = pending[index];
        if (guard) guard(path, options, () => proceed(index + 1));
        else apply();
      };
      proceed(0);
    },
    [],
  );
  return {
    ...state.tabs,
    ...setters,
    selectRightRailTab,
    registerNavigationGuard,
    navigateWorkspace,
  };
}

/** Workspace reads and terminal inventory share the same access and lifecycle rules in both hosts. */
export function useWorkspaceResources(
  sessionId: string | undefined,
  {
    permissionLevel,
    shareWorkspaceFiles,
    terminalFirst,
    terminalPending,
  }: {
    permissionLevel: number | null;
    shareWorkspaceFiles?: boolean;
    terminalFirst: boolean;
    terminalPending: boolean;
  },
  setSelectedTerminalKey: (value: SetStateAction<string | null>) => void,
) {
  const canBrowseWorkspace = isEditorLevel(permissionLevel) || shareWorkspaceFiles === true;
  const environmentQuery = useWorkspaceEnvironment(sessionId, { enabled: canBrowseWorkspace });
  const changedFilesQuery = useWorkspaceChangedFiles(sessionId, { enabled: canBrowseWorkspace });
  const changedFilePaths = useMemo(
    () => new Set(changedFilesQuery.data?.data.map((file) => file.path) ?? []),
    [changedFilesQuery.data],
  );
  const isChangedPath = useCallback(
    (path: string) => changedFilePaths.has(path),
    [changedFilePaths],
  );
  const { terminals } = useTerminals(sessionId ?? null, { reconcileWhilePending: terminalPending });
  const agentTerminal = useMemo(() => findAgentTerminal(terminals), [terminals]);
  const railTerminals = useMemo(
    () => inventoryTerminals(terminals, terminalFirst),
    [terminals, terminalFirst],
  );
  const openTerminals = useMemo(() => railTerminals.map(terminalTabKey), [railTerminals]);
  const deleteTerminal = useDeleteTerminal(sessionId ?? "");
  const [terminalPendingClose, setTerminalPendingClose] = useState<string | null>(null);
  useEffect(() => setTerminalPendingClose(null), [sessionId]);
  const confirmCloseTerminal = useCallback(() => {
    const key = terminalPendingClose;
    if (key === null) return;
    setTerminalPendingClose(null);
    setSelectedTerminalKey((active) => {
      if (active !== key) return active;
      const index = openTerminals.indexOf(key);
      const remaining = openTerminals.filter((candidate) => candidate !== key);
      return remaining[index - 1] ?? remaining[index] ?? remaining[0] ?? null;
    });
    const terminal = terminals.find((candidate) => terminalTabKey(candidate) === key);
    if (terminal && sessionId) deleteTerminal.mutate(terminal.id);
  }, [
    terminalPendingClose,
    openTerminals,
    terminals,
    sessionId,
    deleteTerminal,
    setSelectedTerminalKey,
  ]);
  return {
    canBrowseWorkspace,
    environmentQuery,
    changedFilesQuery,
    changedCount: changedFilesQuery.data?.data.length ?? 0,
    isChangedPath,
    showFilesPanel: canBrowseWorkspace && environmentQuery.data?.available !== false,
    terminals,
    agentTerminal,
    railTerminals,
    openTerminals,
    terminalPendingClose,
    setTerminalPendingClose,
    confirmCloseTerminal,
    closingTerminalKey:
      deleteTerminal.isPending && deleteTerminal.variables
        ? `terminal:${deleteTerminal.variables}`
        : null,
  };
}
