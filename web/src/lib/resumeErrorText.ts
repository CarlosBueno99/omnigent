import { WRONG_REPLICA_CODE } from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";

/**
 * Shown when a sharded server answers a resume with ``wrong_replica``. The
 * server's own message is a routing instruction for the client, not the user.
 */
export const RESUME_WRONG_REPLICA_TEXT =
  "Couldn't resume session: the session's runner isn't reachable from this server right now. Try again in a moment.";

/**
 * User-facing text for a failed session resume in the terminal views.
 *
 * :param error: The resume rejection — an :class:`ApiError` from
 *     ``retrySession`` or a client-authored ``Error`` meant for the user.
 * :returns: A single sentence to render beside the Resume button.
 */
export function resumeErrorText(error: unknown): string {
  if (error instanceof ApiError && error.code === WRONG_REPLICA_CODE) {
    return RESUME_WRONG_REPLICA_TEXT;
  }
  if (error instanceof Error && error.message) return `Couldn't resume session: ${error.message}`;
  return "Couldn't resume session.";
}
