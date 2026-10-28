import { useCallback, useEffect, useRef, useState } from "react";
import {
  MAX_OPEN_FILES,
  readSessionWorkspaceState,
  writeSessionWorkspaceState,
} from "@/lib/sessionWorkspaceState";
import { newPendingSideChatId } from "@/lib/sideChat";
import { readChatTabOrder, subagentSessionIdFromTab, writeChatTabOrder } from "@/lib/subagentTabs";

interface SideChatTabsState {
  /** Open side-chat child conversation ids, in open order. */
  tabs: string[];
  /** The selected side-chat tab, or null when another rail view is active. */
  selected: string | null;
}

function readSideChatTabsState(conversationId: string): SideChatTabsState {
  const saved = readSessionWorkspaceState(conversationId);
  const sideChats = saved.openSideChats ?? [];
  // An older client may have changed `openSideChats` since the order was saved.
  const ordered = readChatTabOrder(conversationId).filter(
    (id) => subagentSessionIdFromTab(id) !== null || sideChats.includes(id),
  );
  const tabs = [...ordered, ...sideChats.filter((id) => !ordered.includes(id))];
  return {
    tabs,
    selected: tabs.includes(saved.selectedSideChatId ?? "") ? saved.selectedSideChatId! : null,
  };
}

/**
 * Keep at most `MAX_OPEN_FILES` tabs, never the selected one. Oldest sub-agent
 * tabs go first, since a dropped side chat keeps running with no tab to close it.
 */
function capTabs(state: SideChatTabsState): SideChatTabsState {
  const excess = state.tabs.length - MAX_OPEN_FILES;
  if (excess <= 0) return state;
  const droppable = state.tabs.filter((id) => id !== state.selected);
  const isSubagent = (id: string) => subagentSessionIdFromTab(id) !== null;
  const dropped = new Set(
    [...droppable.filter(isSubagent), ...droppable.filter((id) => !isSubagent(id))].slice(
      0,
      excess,
    ),
  );
  return { ...state, tabs: state.tabs.filter((id) => !dropped.has(id)) };
}

/**
 * Per-session side-chat tabs for the Workspace rail — a browser-local list of
 * child conversation ids, mirroring {@link useBrowserTabs}. The references live
 * in `sessionWorkspaceState` (localStorage), never on the server, so they are
 * per-device and per-browser: they persist across app restarts and reloads but
 * do not follow the user to another device.
 *
 * Opening (`open`) just records an already-created child id as a tab — the
 * child itself is created by the server (Codex's native ephemeral fork, or the
 * generic `POST /v1/sessions/{id}/side-chat`) and surfaced here once its
 * `session_created` event arrives.
 *
 * @param conversationId The parent (main) conversation whose rail owns these tabs.
 */
export function useSideChats(conversationId: string) {
  const [state, setState] = useState(() => readSideChatTabsState(conversationId));
  // Tab changes build on this, not on storage, so they still work when a write fails.
  const latest = useRef({ conversationId, state });
  // WorkspacePanel isn't remounted when the user navigates to another
  // conversation (conversationId is a prop, not a key), so reload this
  // conversation's own tabs on change — otherwise the previous conversation's
  // side-chat tabs would linger in the new one's rail.
  useEffect(() => {
    const loaded = readSideChatTabsState(conversationId);
    latest.current = { conversationId, state: loaded };
    setState(loaded);
  }, [conversationId]);
  const update = useCallback(
    (mutate: (current: SideChatTabsState) => SideChatTabsState) => {
      const current =
        latest.current.conversationId === conversationId
          ? latest.current.state
          : readSideChatTabsState(conversationId);
      const next = capTabs(mutate(current));
      latest.current = { conversationId, state: next };
      const sideChats = next.tabs.filter((id) => subagentSessionIdFromTab(id) === null);
      // Older clients read only side chats, from the workspace store.
      writeSessionWorkspaceState(conversationId, {
        openSideChats: sideChats,
        selectedSideChatId: next.selected,
      });
      writeChatTabOrder(conversationId, sideChats.length === next.tabs.length ? null : next.tabs);
      setState(next);
    },
    [conversationId],
  );

  /** Select a side-chat tab, or clear the selection (null). */
  const select = useCallback(
    (selected: string | null) => update((current) => ({ ...current, selected })),
    [update],
  );

  /** Open an empty (not-yet-created) side-chat tab and select it, returning its
   *  local `pending:` id. The fork is created only when the user sends the
   *  first message (see WorkspacePanel's startPendingSideChat), then this tab is
   *  closed and the real child's tab takes over. */
  const openPending = useCallback((): string => {
    const id = newPendingSideChatId();
    update((current) => ({ tabs: [...current.tabs, id], selected: id }));
    return id;
  }, [update]);

  /** Add a child id as a tab and select it. Idempotent: an already-open child
   *  is re-selected, not duplicated (a create both returns the id and fires a
   *  `session_created`, so `open` can be reached twice for one child). */
  const open = useCallback(
    (childId: string) =>
      update((current) => ({
        tabs: current.tabs.includes(childId) ? current.tabs : [...current.tabs, childId],
        selected: childId,
      })),
    [update],
  );

  /** Replace a tab's id in place, preserving its position and selection. Used
   *  when a `pending:` tab's fork resolves to a real child id, so the tab does
   *  not disappear and reappear. No-op if `oldId` isn't open; if `newId` is
   *  already a tab, the pending one is just dropped. */
  const rekey = useCallback(
    (oldId: string, newId: string) =>
      update((current) => {
        const index = current.tabs.indexOf(oldId);
        if (index === -1) return current;
        const selected = current.selected === oldId ? newId : current.selected;
        if (current.tabs.includes(newId)) {
          return { tabs: current.tabs.filter((id) => id !== oldId), selected };
        }
        const tabs = [...current.tabs];
        tabs[index] = newId;
        return { tabs, selected };
      }),
    [update],
  );

  /** Close a tab. Drops only the client-side reference — an ephemeral Codex
   *  fork dies with its process, and a generic child stays hidden server-side. */
  const close = useCallback(
    (childId: string) =>
      update((current) => {
        const index = current.tabs.indexOf(childId);
        if (index === -1) return current;
        const tabs = current.tabs.filter((id) => id !== childId);
        const selected =
          current.selected === childId ? (tabs[Math.max(0, index - 1)] ?? null) : current.selected;
        return { tabs, selected };
      }),
    [update],
  );

  return { tabs: state.tabs, selected: state.selected, open, openPending, rekey, close, select };
}
