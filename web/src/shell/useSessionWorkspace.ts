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
  type DefaultWorkspaceTab,
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
  const [initial, setInitial] = useState(() => ({ sessionId, tabs: readTabs(sessionId) }));
  const [rightRailTab, setRightRailTab] = useState(initial.tabs.rightRailTab);
  const [openPanels, setOpenPanels] = useState(initial.tabs.openPanels);
  const [openFiles, setOpenFiles] = useState(initial.tabs.openFiles);
  const [selectedFilePath, setSelectedFilePath] = useState(initial.tabs.selectedFilePath);
  const [selectedTerminalKey, setSelectedTerminalKey] = useState(initial.tabs.selectedTerminalKey);
  if (initial.sessionId !== sessionId) {
    const tabs = readTabs(sessionId);
    setInitial({ sessionId, tabs });
    setRightRailTab(tabs.rightRailTab);
    setOpenPanels(tabs.openPanels);
    setOpenFiles(tabs.openFiles);
    setSelectedFilePath(tabs.selectedFilePath);
    setSelectedTerminalKey(tabs.selectedTerminalKey);
  }
  const snapshot = useMemo(
    () => ({
      sessionId,
      rightRailTab,
      openPanels,
      openFiles,
      selectedFilePath,
      selectedTerminalKey,
    }),
    [sessionId, rightRailTab, openPanels, openFiles, selectedFilePath, selectedTerminalKey],
  );
  const persisted = useRef(snapshot);
  useEffect(() => {
    const previous = persisted.current;
    persisted.current = snapshot;
    // Skip mount/session changes until the host has applied URL/default overrides.
    if (previous !== snapshot && previous.sessionId === snapshot.sessionId && snapshot.sessionId) {
      const { sessionId: ownerId, ...tabs } = snapshot;
      writeSessionWorkspaceState(ownerId, tabs);
    }
  }, [snapshot]);
  const selectRightRailTab = useCallback((next: RightRailTab | null) => {
    setRightRailTab(next);
    if (isDefaultWorkspaceTab(next) && !readPinnedWorkspaceTabs().includes(next)) {
      setOpenPanels((previous) => (previous.includes(next) ? previous : [...previous, next]));
    }
  }, []);
  const guards = useRef(new Set<FileNavigationGuard>());
  const closePanel = useCallback(
    (
      tab: DefaultWorkspaceTab,
      fallback = readInitialWorkspaceTab(),
      available?: (panel: DefaultWorkspaceTab) => boolean,
    ) => {
      setOpenPanels((panels) => {
        const remaining = panels.filter((panel) => panel !== tab);
        setRightRailTab((selected) =>
          selected === tab
            ? (remaining.filter((panel) => available?.(panel) ?? true).at(-1) ?? fallback)
            : selected,
        );
        return remaining;
      });
    },
    [],
  );
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
    rightRailTab,
    setRightRailTab,
    openPanels,
    setOpenPanels,
    openFiles,
    setOpenFiles,
    selectedFilePath,
    setSelectedFilePath,
    selectedTerminalKey,
    setSelectedTerminalKey,
    selectRightRailTab,
    closePanel,
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
  // View grants share the conversation, not files. Unknown level defers to the server gate.
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
  // The agent terminal belongs to the main view, not the workspace shell tabs.
  const railTerminals = useMemo(
    () => inventoryTerminals(terminals, terminalFirst),
    [terminals, terminalFirst],
  );
  const openTerminals = useMemo(() => railTerminals.map(terminalTabKey), [railTerminals]);
  const deleteTerminal = useDeleteTerminal(sessionId ?? "");
  const [closeSession, setCloseSession] = useState(sessionId);
  const [terminalPendingClose, setTerminalPendingClose] = useState<string | null>(null);
  if (closeSession !== sessionId) {
    setCloseSession(sessionId);
    setTerminalPendingClose(null);
  }
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
