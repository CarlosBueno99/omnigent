// Shift+Tab in a chat composer steps the permission chip to its next mode, the
// way Claude Code's and Cursor's own Shift+Tab does. The shortcut follows the
// chip's menu order but never lands on a mode that removes approvals entirely:
// those stay a deliberate menu pick.

import { permissionModeConcept } from "@/lib/nativeHarnessModes";

/** The key fields the cycle check reads from a React or DOM keyboard event. */
export interface PermissionModeCycleKeyEvent {
  key: string;
  shiftKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  nativeEvent?: { isComposing?: boolean };
}

/**
 * True for a bare Shift+Tab: no Ctrl (browser tab switching), Cmd, or Alt,
 * and not mid-IME composition.
 */
export function isPermissionModeCycleKey(event: PermissionModeCycleKeyEvent): boolean {
  return (
    event.key === "Tab" &&
    event.shiftKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !event.nativeEvent?.isComposing
  );
}

/** Whether Shift+Tab may select *mode*; full-access modes are menu-only. */
export function isCyclablePermissionMode(
  harness: string | null | undefined,
  mode: string,
): boolean {
  return permissionModeConcept(harness, mode) !== "full-access";
}

/**
 * The mode Shift+Tab moves to: the next cyclable option after *current* in menu
 * order, wrapping around. An unknown current mode starts from the top.
 *
 * @param harness Harness id used to classify modes, e.g. `"claude-native"`.
 * @param options The chip's menu options in display order.
 * @param current The selected mode value, or `null` when unknown.
 * @param unavailable Modes to skip, e.g. ones this session already refused.
 * @returns The next mode value, or `null` when no other mode is reachable.
 */
export function nextCycledPermissionMode(
  harness: string | null | undefined,
  options: readonly { value: string }[],
  current: string | null | undefined,
  unavailable: ReadonlySet<string> = new Set(),
): string | null {
  const start = options.findIndex((option) => option.value === current);
  for (let step = 1; step <= options.length; step += 1) {
    const candidate = options[(start + step) % options.length]!.value;
    if (candidate === current) continue;
    if (unavailable.has(candidate) || !isCyclablePermissionMode(harness, candidate)) continue;
    return candidate;
  }
  return null;
}
