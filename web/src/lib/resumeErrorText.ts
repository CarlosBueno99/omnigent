import { WRONG_REPLICA_CODE } from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";

// Replaces the server's wrong_replica message, which is a routing instruction
// for the client rather than something the user can act on.
export const RESUME_WRONG_REPLICA_TEXT =
  "Couldn't resume session: the session's runner isn't reachable from this server right now. Try again in a moment.";

/** User-facing text for a failed session resume, rendered beside the Resume button. */
export function resumeErrorText(error: unknown): string {
  if (error instanceof ApiError && error.code === WRONG_REPLICA_CODE) {
    return RESUME_WRONG_REPLICA_TEXT;
  }
  if (error instanceof Error && error.message) return `Couldn't resume session: ${error.message}`;
  return "Couldn't resume session.";
}
