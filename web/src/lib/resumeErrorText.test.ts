import { describe, expect, it } from "vitest";
import { RESUME_WRONG_REPLICA_TEXT, resumeErrorText } from "./resumeErrorText";
import { ApiError } from "./sessionsApi";

describe("resumeErrorText", () => {
  it("replaces the server's wrong_replica routing message with user-facing text", () => {
    const text = resumeErrorText(
      new ApiError("session runner is on another replica; retry", 400, "wrong_replica"),
    );
    expect(text).toBe(RESUME_WRONG_REPLICA_TEXT);
    expect(text).not.toMatch(/replica/i);
  });

  it("keeps other server and client messages, which already explain the failure", () => {
    expect(resumeErrorText(new ApiError("host is offline", 409, "conflict"))).toBe(
      "Couldn't resume session: host is offline",
    );
    expect(
      resumeErrorText(new Error("The session is already connected; no recovery was performed")),
    ).toBe("Couldn't resume session: The session is already connected; no recovery was performed");
  });

  it("falls back to a generic sentence without a message", () => {
    expect(resumeErrorText(new Error(""))).toBe("Couldn't resume session.");
    expect(resumeErrorText("boom")).toBe("Couldn't resume session.");
  });
});
