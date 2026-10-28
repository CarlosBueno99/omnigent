// Sub-agent tabs share the Workspace panel's tab list with side chats. The
// prefix tells the rail to show the whole session and not stop it on close.

const SUBAGENT_TAB_PREFIX = "subagent:";

/** The chat-tab id for a sub-agent session. */
export function subagentTabId(childSessionId: string): string {
  return `${SUBAGENT_TAB_PREFIX}${childSessionId}`;
}

/** The sub-agent session behind a chat-tab id, or null for a side-chat tab. */
export function subagentSessionIdFromTab(tabId: string): string | null {
  if (!tabId.startsWith(SUBAGENT_TAB_PREFIX)) return null;
  const sessionId = tabId.slice(SUBAGENT_TAB_PREFIX.length);
  return sessionId.length > 0 ? sessionId : null;
}

// The mixed side-chat and sub-agent tab order, per parent session. It has its
// own key because older clients rewrite the workspace store without fields they
// don't know, which would drop every saved sub-agent tab.
export const CHAT_TAB_ORDER_KEY = "omnigent:chat-tab-order";
// Same bound as the workspace store; the least-recently-written parents go first.
const MAX_PARENTS = 100;

type ChatTabOrderStore = { id: string; tabs: string[] }[];

function readChatTabOrderStore(): ChatTabOrderStore {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(CHAT_TAB_ORDER_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((entry: unknown) => {
      if (typeof entry !== "object" || entry === null) return [];
      const { id, tabs } = entry as Record<string, unknown>;
      if (typeof id !== "string" || !Array.isArray(tabs)) return [];
      const valid = tabs.filter((tab): tab is string => typeof tab === "string" && tab !== "");
      return [{ id, tabs: [...new Set(valid)] }];
    });
  } catch {
    return [];
  }
}

/** The saved tab order for `parentId`, or an empty list when none is saved. */
export function readChatTabOrder(parentId: string): string[] {
  return readChatTabOrderStore().find((entry) => entry.id === parentId)?.tabs ?? [];
}

/** Save `parentId`'s tab order, or forget it when `tabs` is null. */
export function writeChatTabOrder(parentId: string, tabs: readonly string[] | null): void {
  const store = readChatTabOrderStore().filter((entry) => entry.id !== parentId);
  if (tabs !== null) store.push({ id: parentId, tabs: [...tabs] });
  try {
    window.localStorage.setItem(CHAT_TAB_ORDER_KEY, JSON.stringify(store.slice(-MAX_PARENTS)));
  } catch {
    // Storage quota/access errors must not break panel interactions.
  }
}
