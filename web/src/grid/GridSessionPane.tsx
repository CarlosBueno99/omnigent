import { useCallback, useMemo, useState, type ComponentProps } from "react";
import { createPortal } from "react-dom";
import { useDraggable, useDroppable } from "@dnd-kit/core";
import { cn } from "@/lib/utils";
import { Maximize2Icon, XIcon } from "lucide-react";
import { SideChatPane } from "@/components/chat/SideChatPane";
import { Button } from "@/components/ui/button";
import { useBrowserAgentRelay } from "@/hooks/useBrowserAgentRelay";
import { useChildSessions } from "@/hooks/useChildSessions";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { useCanEdit } from "@/hooks/usePermissions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useSession, useRootSessionId } from "@/hooks/useSession";
import { livenessRowFromSession, useSessionLiveness } from "@/hooks/useSessionLiveness";
import { terminalTabKey } from "@/hooks/useTerminals";
import { useSessionWorkspace, useWorkspaceResources } from "@/shell/useSessionWorkspace";
import { supportsBrowser } from "@/lib/nativeBridge";
import { Link } from "@/lib/routing";
import { CloseShellDialog } from "@/shell/CloseShellDialog";
import {
  FileViewerContext,
  type FilePosition,
  type OpenFileOptions,
} from "@/shell/FileViewerContext";
import type { RightRailTab } from "@/shell/railTabs";
import type { ChangedSort } from "@/shell/FlatFileList";
import { WorkspacePanel } from "@/shell/WorkspacePanel";
import { useGridSessions } from "./GridSessionsContext";

export type GridWorkspacePanelProps = Pick<
  ComponentProps<typeof WorkspacePanel>,
  "open" | "width" | "handleProps" | "maximized" | "onToggleMaximized"
>;

export function GridSessionPane({
  sessionId,
  area,
  selected,
  onActivate,
  workspaceTarget,
  workspacePanel,
  onOpenWorkspace,
}: {
  sessionId: string;
  area?: string;
  selected: boolean;
  onActivate: () => void;
  workspaceTarget: HTMLDivElement | null;
  workspacePanel: GridWorkspacePanelProps;
  onOpenWorkspace: () => void;
}) {
  const { session } = useSession(sessionId);
  const state = useConversationEntryState(sessionId);
  const canEdit = useCanEdit(sessionId);
  const permissionLevel = session?.permissionLevel ?? (canEdit ? null : 1);
  const grids = useGridSessions()!;
  const title = session?.title || "Session";
  const {
    attributes,
    listeners,
    setNodeRef: setDragNodeRef,
    setActivatorNodeRef,
    isDragging,
  } = useDraggable({ id: sessionId, data: { title } });
  const {
    setNodeRef: setDropNodeRef,
    active,
    isOver,
  } = useDroppable({ id: sessionId, data: { title } });
  const setPaneRef = useCallback(
    (node: HTMLElement | null) => {
      setDragNodeRef(node);
      setDropNodeRef(node);
    },
    [setDragNodeRef, setDropNodeRef],
  );
  const healthSessions = useMemo(() => [{ id: sessionId }], [sessionId]);
  useRunnerHealthRegistration(healthSessions);
  useBrowserAgentRelay(sessionId);
  const liveness = useSessionLiveness(sessionId, livenessRowFromSession(session));
  const rootSessionId = useRootSessionId(sessionId, session?.parentSessionId);
  const { children: childSessions } = useChildSessions(rootSessionId);
  const workspace = useSessionWorkspace(sessionId);
  const {
    setSelectedFilePath,
    setSelectedTerminalKey,
    setOpenFiles,
    closePanel,
    selectRightRailTab,
    registerNavigationGuard,
    navigateWorkspace,
  } = workspace;
  const {
    canBrowseWorkspace: canBrowse,
    environmentQuery: environment,
    showFilesPanel: showFiles,
    changedCount,
    isChangedPath,
    terminals,
    openTerminals,
    terminalPendingClose: pendingClose,
    setTerminalPendingClose: setPendingClose,
    closingTerminalKey,
    confirmCloseTerminal,
  } = useWorkspaceResources(
    sessionId,
    {
      permissionLevel,
      shareWorkspaceFiles: session?.shareWorkspaceFiles,
      terminalFirst: session?.labels?.["omnigent.ui"] === "terminal",
      terminalPending: state.terminalPending,
    },
    setSelectedTerminalKey,
  );
  const terminalToClose = terminals.find((terminal) => terminalTabKey(terminal) === pendingClose);
  const [sort, setSort] = useState<ChangedSort>("recent");
  const [showHidden, setShowHidden] = useState(false);
  const [position, setPosition] = useState<FilePosition>();
  const openFile = useCallback(
    (path: string, options?: OpenFileOptions) => {
      navigateWorkspace(path, options, () => {
        onOpenWorkspace();
        setPosition(
          options?.line === undefined ? undefined : { line: options.line, column: options.column },
        );
        setSelectedFilePath(path);
        setSelectedTerminalKey(null);
        setOpenFiles((previous) => (previous.includes(path) ? previous : [...previous, path]));
      });
    },
    [navigateWorkspace, onOpenWorkspace, setSelectedFilePath, setSelectedTerminalKey, setOpenFiles],
  );
  const selectTab = useCallback(
    (rightRailTab: RightRailTab | null, reveal = false) => {
      navigateWorkspace("", undefined, () => {
        selectRightRailTab(rightRailTab);
        setSelectedFilePath(null);
        setSelectedTerminalKey(null);
        if (reveal) onOpenWorkspace();
      });
    },
    [
      navigateWorkspace,
      selectRightRailTab,
      setSelectedFilePath,
      setSelectedTerminalKey,
      onOpenWorkspace,
    ],
  );
  const closeFile = useCallback(
    (path: string) => {
      navigateWorkspace("", undefined, () => {
        setOpenFiles((files) => files.filter((file) => file !== path));
        setSelectedFilePath((selectedPath) => (selectedPath === path ? null : selectedPath));
      });
    },
    [navigateWorkspace, setOpenFiles, setSelectedFilePath],
  );
  const openTerminal = useCallback(
    (key: string) => {
      navigateWorkspace("", undefined, () => {
        onOpenWorkspace();
        setSelectedFilePath(null);
        setSelectedTerminalKey(key);
      });
    },
    [navigateWorkspace, setSelectedFilePath, setSelectedTerminalKey, onOpenWorkspace],
  );
  const fileContext = useMemo(
    () => ({
      openFile,
      registerNavigationGuard,
      openGithubTab: () => selectTab("github", true),
      conversationId: sessionId,
      workspaceRoot: environment.data?.root ?? null,
      workspaceHome: environment.data?.home ?? null,
      sessionHostId: session?.hostId,
      isChangedPath,
    }),
    [
      openFile,
      registerNavigationGuard,
      selectTab,
      sessionId,
      environment.data,
      session?.hostId,
      isChangedPath,
    ],
  );
  const rightRailTab =
    !showFiles && ["files", "changes", "github"].includes(workspace.rightRailTab ?? "")
      ? "subagents"
      : (workspace.rightRailTab ?? null);
  return (
    <FileViewerContext.Provider value={fileContext}>
      <section
        ref={setPaneRef}
        style={{ gridArea: area }}
        data-grid-session={sessionId}
        data-active={selected}
        aria-label={`${title}${selected ? " (active)" : ""}`}
        onClickCapture={onActivate}
        onFocusCapture={onActivate}
        className={cn(
          "relative flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border bg-background",
          selected && "border-brand-accent/50 ring-1 ring-brand-accent/50",
          isDragging && "opacity-50",
        )}
      >
        <header
          className={cn(
            "flex h-8 shrink-0 items-center border-b px-1",
            selected && "bg-brand-accent/50",
          )}
        >
          <button
            type="button"
            ref={setActivatorNodeRef}
            {...attributes}
            {...listeners}
            aria-label={`Move ${title}`}
            title="Drag to another cell to swap positions"
            className="h-full min-w-0 flex-1 touch-none cursor-grab truncate px-1 text-left text-sm leading-normal font-medium select-none active:cursor-grabbing focus-visible:outline-2 focus-visible:outline-ring"
          >
            {title}
          </button>
          <Button asChild variant="ghost" size="icon-xs">
            <Link draggable={false} to={`/c/${sessionId}`} aria-label={`Open ${title} full screen`}>
              <Maximize2Icon className="size-3.5" />
            </Link>
          </Button>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Remove ${title} from grid`}
            onClick={() => grids.closePane(sessionId)}
          >
            <XIcon className="size-4" />
          </Button>
        </header>
        <div className="min-h-0 min-w-0 flex-1 overflow-hidden">
          <SideChatPane childId={sessionId} fullSession readOnly={!canEdit} />
        </div>
        {isOver && active?.id !== sessionId && (
          <div
            data-testid="grid-move-ghost"
            className="pointer-events-none absolute inset-0 z-50 flex items-center justify-center rounded-lg border-2 border-dashed border-brand-accent bg-brand-accent/25"
          />
        )}
        <CloseShellDialog
          open={pendingClose !== null}
          shellLabel={terminalToClose?.name ?? null}
          onCancel={() => setPendingClose(null)}
          onConfirm={confirmCloseTerminal}
        />
      </section>
      {/* Keep inactive views mounted so switching cells preserves unsaved edits. */}
      {workspaceTarget &&
        createPortal(
          <div className={selected && workspacePanel.open ? "contents" : "hidden"}>
            <WorkspacePanel
              contained
              conversationId={sessionId}
              sessionHarness={state.sessionHarness}
              agentId={state.boundAgentId ?? session?.agentId ?? null}
              {...workspacePanel}
              open={selected && workspacePanel.open}
              inert={!selected || !workspacePanel.open}
              rightRailTab={rightRailTab}
              onRightRailTabChange={selectTab}
              openPanels={workspace.openPanels ?? []}
              onClosePanel={(tab) => {
                navigateWorkspace("", undefined, () => closePanel(tab));
              }}
              showFilesPanel={showFiles}
              showGithubTab={showFiles}
              showBrowserTab={supportsBrowser()}
              onReveal={onOpenWorkspace}
              changedCount={changedCount}
              subagentsWorking={childSessions.filter((child) => child.busy).length}
              agentCount={childSessions.length + 1}
              rootSessionId={rootSessionId}
              selectedFilePath={canBrowse ? (workspace.selectedFilePath ?? null) : null}
              filePosition={position}
              openFiles={canBrowse ? (workspace.openFiles ?? []) : []}
              openFileViewer={openFile}
              onCloseFile={closeFile}
              onShowScopeView={() => selectTab(rightRailTab)}
              onCommentsOpenChange={() => {}}
              openTerminalTab={openTerminal}
              openTerminals={openTerminals}
              selectedTerminalKey={workspace.selectedTerminalKey ?? null}
              closingTerminalKey={closingTerminalKey}
              onCloseTerminal={setPendingClose}
              maximized={selected && workspacePanel.maximized}
              permissionLevel={permissionLevel}
              filesPanelSort={sort}
              onSortChange={setSort}
              filesPanelShowHidden={showHidden}
              onShowHiddenChange={setShowHidden}
              liveness={liveness}
            />
          </div>,
          workspaceTarget,
        )}
    </FileViewerContext.Provider>
  );
}
