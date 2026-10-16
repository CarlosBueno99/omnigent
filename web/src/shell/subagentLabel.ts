// Display label for a child (sub-agent) session, shared by the Agents rail
// rows and the approval cards a sub-agent's prompts mirror into, so a card
// names the sub-agent exactly as the rail lists it.

import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER,
  CLAUDE_NATIVE_SUBAGENT_WRAPPER,
  CODEX_NATIVE_SUBAGENT_WRAPPER,
  OPENCODE_NATIVE_SUBAGENT_WRAPPER,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";

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
  // User-added rows use the reserved "ui:<agent>:<name>" title sentinel;
  // LLM-spawned titles cannot start with "ui:" because the spec validator
  // rejects "ui" as a sub-agent name.
  const isUserAdded = child.title?.startsWith("ui:") ?? false;
  const childWrapper = child.labels?.[WRAPPER_LABEL_KEY];
  // agy child titles are ``"<role>:<cascade id>"``: the generic path below
  // would show the cascade UUID, so native sub-agents use ``tool`` instead.
  const isNativeSubagent =
    childWrapper === CODEX_NATIVE_SUBAGENT_WRAPPER ||
    childWrapper === OPENCODE_NATIVE_SUBAGENT_WRAPPER ||
    childWrapper === ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER ||
    childWrapper === CLAUDE_NATIVE_SUBAGENT_WRAPPER;
  if (isNativeSubagent && !isUserAdded) {
    return child.tool ?? child.title ?? fallback;
  }
  const trimmed = (value: string | null | undefined) => value?.trim() || null;
  let titleTask: string | null = null;
  if (child.title?.includes(":")) {
    const titleSuffix = child.title.split(":").slice(1).join(":");
    if (titleSuffix) titleTask = titleSuffix;
  }
  const handle = trimmed(child.session_name) ?? trimmed(titleTask);
  const summary = trimmed(child.task_summary);
  if (summary && handle && handle !== summary) return `${summary} · ${handle}`;
  return summary ?? handle ?? trimmed(child.title) ?? trimmed(child.tool) ?? fallback;
}
