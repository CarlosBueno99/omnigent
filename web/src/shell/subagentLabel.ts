// Display label for a child (sub-agent) session, shared by the Agents rail
// rows and the approval cards a sub-agent's prompts mirror into, so a card
// names the sub-agent exactly as the rail lists it.

import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { nativeCodingAgentForSubagentWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";

/**
 * Pick the primary label for a child-session row.
 *
 * The generated ``task_summary`` leads so sibling rows read by what they do; the
 * orchestrator-chosen handle (``session_name`` or the ``"{tool}:{name}"`` suffix)
 * follows it, e.g. ``"Deploy PR41 via web ops · deploy-profile-page"``.
 *
 * @param child - One child-session summary from the poll or stream.
 * @param fallback - Label for a row with no name at all; defaults to the id.
 * @returns The label shown beside the child icon.
 */
export function childPrimaryLabel(child: ChildSessionInfo, fallback: string = child.id): string {
  const trimmed = (value: string | null | undefined) => value?.trim() || null;
  // User-added rows use the reserved "ui:<agent>:<name>" title sentinel;
  // LLM-spawned titles cannot start with "ui:" because the spec validator
  // rejects "ui" as a sub-agent name.
  const isUserAdded = child.title?.startsWith("ui:") ?? false;
  // Native sub-agent titles are ``"<role>:<opaque id>"``: the generic path
  // below would show that id, so every registered native wrapper uses ``tool``.
  const isNativeSubagent =
    nativeCodingAgentForSubagentWrapper(child.labels?.[WRAPPER_LABEL_KEY]) !== undefined;
  if (isNativeSubagent && !isUserAdded) {
    return trimmed(child.tool) ?? trimmed(child.title) ?? fallback;
  }
  // Strip the "ui:" sentinel first so the suffix is the human name, as the
  // composer tray does.
  const titleSource = isUserAdded ? child.title?.slice(3) : child.title;
  let titleTask: string | null = null;
  if (titleSource?.includes(":")) {
    const titleSuffix = titleSource.split(":").slice(1).join(":");
    if (titleSuffix) titleTask = titleSuffix;
  }
  const handle = trimmed(child.session_name) ?? trimmed(titleTask);
  const summary = trimmed(child.task_summary);
  if (summary && handle && handle !== summary) return `${summary} · ${handle}`;
  return summary ?? handle ?? trimmed(child.title) ?? trimmed(child.tool) ?? fallback;
}
