import type { SandboxLaunchStage } from "./types";

/** User-facing progress shared by the chat indicator and host composer. */
// `starting` is the sandbox host dialing back; `connecting` is its runner launch.
export const SANDBOX_STAGE_LABELS: Record<SandboxLaunchStage, string> = {
  provisioning: "Provisioning sandbox",
  cloning: "Cloning repository",
  starting: "Connecting host",
  connecting: "Starting agent",
  ready: "Connected",
  failed: "Sandbox launch failed",
};
