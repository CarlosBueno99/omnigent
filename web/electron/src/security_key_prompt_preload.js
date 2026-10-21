// Preload for the security-key prompt (security-key-prompt/index.html).
// The main process matches every message to the prompt window by sender, so
// this bridge is inert if it ever ends up attached to anything else.

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("omnigentSecurityKeyPrompt", {
  /** Hide the prompt; the next WebAuthn request shows it again. */
  dismiss: () => {
    ipcRenderer.send("omnigent:security-key-prompt-dismiss");
  },
});
