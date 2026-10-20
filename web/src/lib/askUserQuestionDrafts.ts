import type { ClaudeQuestion } from "./askUserQuestion";

/** Unsent answers on a pending AskUserQuestion card. */
export interface AskUserQuestionDraft {
  currentIndex: number;
  selections: Record<string, string | string[]>;
  customSelected: Record<string, boolean>;
  customInputs: Record<string, string>;
}

const MAX_DRAFTS = 20;

// In memory only, so typed answers never reach browser storage.
const drafts = new Map<string, { questions: string; draft: AskUserQuestionDraft }>();

/**
 * Return the draft saved for a pending question card.
 *
 * Harness elicitation ids can recur for different questions, so a draft
 * restores only onto the exact questions it was saved with.
 *
 * @param elicitationId Pending elicitation id, e.g. ``"elicit_claude_0f3a..."``.
 * @param questions The card's current questions.
 * @returns The saved draft, or ``undefined`` when none matches.
 */
export function getAskUserQuestionDraft(
  elicitationId: string,
  questions: ClaudeQuestion[],
): AskUserQuestionDraft | undefined {
  const entry = drafts.get(elicitationId);
  return entry?.questions === JSON.stringify(questions) ? entry.draft : undefined;
}

/**
 * Remember a card's unsent answers so a remounted card can restore them.
 *
 * Only the most recently saved drafts are kept.
 *
 * @param elicitationId Pending elicitation id the answers belong to.
 * @param questions The questions the answers were given for.
 * @param draft The card's current carousel position and answers.
 */
export function saveAskUserQuestionDraft(
  elicitationId: string,
  questions: ClaudeQuestion[],
  draft: AskUserQuestionDraft,
): void {
  drafts.delete(elicitationId);
  drafts.set(elicitationId, { questions: JSON.stringify(questions), draft });
  if (drafts.size > MAX_DRAFTS) drafts.delete(drafts.keys().next().value as string);
}

/** Forget a card's draft once the user submits or cancels it. */
export function clearAskUserQuestionDraft(elicitationId: string): void {
  drafts.delete(elicitationId);
}
