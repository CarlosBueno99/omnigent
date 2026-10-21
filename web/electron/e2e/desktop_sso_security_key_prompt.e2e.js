// Desktop e2e: a Databricks App whose SSO asks for a security key. Electron has
// no passkey sheet, so the shell must show its own prompt while the sign-in page
// waits on WebAuthn. A CDP virtual authenticator stands in for the USB key.

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");
const { startFakeAppsFrontDoor } = require("./fixtures/fakeAppsFrontDoor");

const deps = desktopDepsAvailable();
const RECORD_DIR =
  process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
  path.join(__dirname, "recordings", "desktop-sso-security-key-prompt");
const PROMPT_PAGE = "security-key-prompt/index.html";
const PROMPT_WAIT_MS = 20_000;

const sleep = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

async function waitFor(check, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  // oxlint-disable no-await-in-loop -- polling.
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await sleep(250);
  }
  // oxlint-enable no-await-in-loop
  return null;
}

/**
 * Plug a virtual USB security key holding the sign-in page's credential into
 * the app window. `touched: false` leaves the request waiting for a touch.
 */
async function insertSecurityKey(page, { rpId, credentialId, touched }) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable", { enableUI: false });
  const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "usb",
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: touched,
    },
  });
  const { privateKey } = crypto.generateKeyPairSync("ec", { namedCurve: "P-256" });
  await cdp.send("WebAuthn.addCredential", {
    authenticatorId,
    credential: {
      credentialId: Buffer.from(credentialId, "base64url").toString("base64"),
      isResidentCredential: false,
      rpId,
      privateKey: privateKey.export({ format: "der", type: "pkcs8" }).toString("base64"),
      signCount: 0,
    },
  });
}

/** The prompt window's visibility and geometry, or null before it exists. */
function promptState(electronApp) {
  return electronApp.evaluate(({ BrowserWindow }, page) => {
    const prompt = BrowserWindow.getAllWindows().find((win) =>
      win.webContents.getURL().endsWith(page),
    );
    if (!prompt) return null;
    return {
      shown: prompt.isVisible() && !prompt.webContents.isLoading(),
      bounds: prompt.getBounds(),
      parent: prompt.getParentWindow()?.getContentBounds() ?? null,
    };
  }, PROMPT_PAGE);
}

describe(
  "desktop shell — Databricks App SSO with a security key factor",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let frontDoor;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-sso-key-"));
      server = await spawnServer(tmpDir);
      frontDoor = await startFakeAppsFrontDoor({
        upstreamUrl: server.serverUrl,
        scratchDir: tmpDir,
      });
    });

    after(async () => {
      if (frontDoor) await frontDoor.close();
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    async function connectWithKey(app, touched) {
      await app.window.locator("#url").waitFor({ state: "visible", timeout: 15_000 });
      await insertSecurityKey(app.window, {
        rpId: new URL(frontDoor.idpOrigin).hostname,
        credentialId: frontDoor.credentialId,
        touched,
      });
      await app.window.locator("#url").fill(`${frontDoor.serverUrl}/`);
      // Pause so the recording shows the entered URL.
      await sleep(2000);
      await app.window.locator("#connect").click();
    }

    const idpEvents = () => frontDoor.events.map(({ event }) => event);

    it(
      "shows a security-key prompt while sign-in waits for the key",
      { timeout: 120_000 },
      async () => {
        frontDoor.events.length = 0;
        const app = await launchDesktop({
          recordDir: RECORD_DIR,
          preload: [frontDoor.preloadPath],
          env: frontDoor.launchEnv,
        });
        try {
          await connectWithKey(app, false);
          const requested = await waitFor(() => idpEvents().includes("get_started"), 30_000);
          assert.ok(requested, "sign-in never reached the security-key step");

          const prompt = await waitFor(async () => {
            const state = await promptState(app.electronApp);
            return state?.shown ? state : null;
          }, PROMPT_WAIT_MS);
          assert.ok(
            prompt,
            `no security-key prompt within ${PROMPT_WAIT_MS / 1000}s of the WebAuthn request; ` +
              `IdP events ${JSON.stringify(idpEvents())}`,
          );
          const { bounds, parent } = prompt;
          assert.ok(
            bounds.x >= parent.x &&
              bounds.x + bounds.width <= parent.x + parent.width &&
              bounds.y > parent.y &&
              bounds.y + bounds.height <= parent.y + parent.height,
            `prompt ${JSON.stringify(bounds)} is outside the app window ${JSON.stringify(parent)}`,
          );

          const page = app.electronApp.windows().find((win) => win.url().endsWith(PROMPT_PAGE));
          await page.getByText("Use your security key").waitFor({ state: "visible" });
          const clipped = await page.evaluate(
            () => document.getElementById("card").getBoundingClientRect().bottom > innerHeight,
          );
          assert.equal(clipped, false, "the prompt's text does not fit its window");
          // Hold the prompt on screen long enough to read in the recording.
          await sleep(5000);

          await page.locator("#dismiss").click();
          const dismissed = await waitFor(
            async () => (await promptState(app.electronApp))?.shown === false,
            5000,
          );
          assert.ok(dismissed, "Dismiss did not hide the prompt");
          await sleep(3000);
          assert.deepEqual(
            idpEvents().filter((event) => event.startsWith("get_") && event !== "get_started"),
            [],
            "the WebAuthn request should still be waiting for the key",
          );
        } finally {
          // Stop the display capture first so the clip ends on the window, not teardown.
          await app.stopDisplayCapture();
          await app.electronApp.close();
          saveRecording(RECORD_DIR, "sso-security-key-prompt");
        }
      },
    );

    it(
      "completes sign-in with a touched key and leaves no prompt behind",
      { timeout: 120_000 },
      async () => {
        frontDoor.events.length = 0;
        const app = await launchDesktop({
          recordDir: RECORD_DIR,
          preload: [frontDoor.preloadPath],
          env: frontDoor.launchEnv,
        });
        try {
          await connectWithKey(app, true);
          await app.window.getByText("What should we build?").waitFor({ timeout: 30_000 });
          assert.ok(idpEvents().includes("get_resolved"), JSON.stringify(idpEvents()));
          assert.notEqual((await promptState(app.electronApp))?.shown, true);
        } finally {
          await app.stopDisplayCapture();
          await app.electronApp.close();
          saveRecording(RECORD_DIR, "sso-security-key-sign-in");
        }
      },
    );
  },
);
