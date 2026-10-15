// Desktop-shell lane: a loopback URL typed into the embedded browser's Address
// bar must be refused by the main-process URL policy (alert, no view), whatever
// intent the renderer claims. Run instructions: e2e/README.md.

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");
const { startDesignBackend } = require("./desktopDesignPromptHarness");

const deps = desktopDepsAvailable();
const RECORD_DIR =
  process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
  path.join(__dirname, "recordings", "desktop-url-bar-policy");
const headlessLinux =
  process.platform === "linux" && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY;
function skipReason() {
  if (!deps.ok) return `missing deps: ${deps.missing.join(", ")}`;
  if (headlessLinux) return "no display (run under xvfb-run)";
  return false;
}
const skip = skipReason();

/** Timeline label for what the poll observed. */
function outcomeLabel(loopback) {
  if (loopback.alert) return "navigation blocked alert shown";
  if (loopback.loaded) return "loopback page loaded in pane";
  return "no load or alert within the window";
}

const LOOPBACK_PAGE = `<!doctype html><title>Internal service</title>
<body style="margin:0;background:#d9480f;color:#fff;font:28px sans-serif">
<div style="padding:40px"><h1>INTERNAL SERVICE</h1>
<p>Served on loopback (127.0.0.1); the agent URL policy rejects this host.</p></div></body>`;

/** A test-owned loopback site standing in for an internal service. */
function serveLoopback() {
  return new Promise((resolve, reject) => {
    const server = http.createServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(LOOPBACK_PAGE);
    });
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      resolve({
        url: `http://127.0.0.1:${server.address().port}/`,
        close: () =>
          new Promise((done) => {
            server.close(done);
          }),
      });
    });
  });
}

/** Poll the view's committed URL and the pane's alert until the URL reaches
 *  `target` or an alert appears (or the window elapses). */
async function observeNavigation(window, pane, viewId, target, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  const alerts = pane.getByRole("alert");
  // An alert that re-renders between the count and the read counts as "no alert yet".
  const readAlert = async () =>
    (await alerts.count()) > 0
      ? await alerts
          .first()
          .innerText()
          .catch(() => null)
      : null;
  for (;;) {
    // oxlint-disable-next-line no-await-in-loop -- Poll the bridge sequentially.
    const view = await window.evaluate((id) => window.omnigentDesktop.browserHasView(id), viewId);
    // oxlint-disable-next-line no-await-in-loop -- Read the alert after the bridge probe.
    const alert = await readAlert();
    const loaded = !!(view.exists && view.url && view.url.startsWith(target));
    if (loaded || alert || Date.now() > deadline) {
      return {
        viewExists: !!view.exists,
        viewUrl: view.url ?? null,
        alert,
        loaded,
        timedOut: !loaded && !alert,
      };
    }
    // oxlint-disable-next-line no-await-in-loop -- Wait before the next probe.
    await window.waitForTimeout(250);
  }
}

/** Start time of the newest composited display capture, parsed from its raw filename. */
function displayCaptureStartMs(recordDir) {
  const starts = fs
    .readdirSync(recordDir)
    .filter((f) => /^display@\d+\.webm$/.test(f))
    .map((f) => Number(f.slice("display@".length, -".webm".length)))
    .sort((a, b) => b - a);
  return starts.length > 0 ? starts[0] : null;
}

describe("desktop shell — embedded browser address bar applies the URL policy", { skip }, () => {
  let tmpDir;
  let backend;
  let loopback;

  before(
    async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-url-policy-"));
      loopback = await serveLoopback();
      backend = await startDesignBackend(tmpDir);
    },
    { timeout: 120_000 },
  );

  after(async () => {
    if (backend) await backend.close();
    if (loopback) await loopback.close();
    if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("rejects a loopback URL typed into the address bar", { timeout: 240_000 }, async () => {
    const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
      recordDir: RECORD_DIR,
      serverUrl: backend.serverUrl,
      env: {
        OMNIGENT_CONFIG_HOME: path.join(tmpDir, "config"),
        OMNIGENT_DATA_DIR: path.join(tmpDir, "config", "data"),
      },
    });
    const observations = {
      sessionId: backend.sessionId,
      loopbackUrl: loopback.url,
      captureStartedAtMs: displayCaptureStartMs(RECORD_DIR),
      timeline: [],
    };
    const mark = (event) => observations.timeline.push({ event, atMs: Date.now() });
    try {
      const nativeWindow = await electronApp.browserWindow(window);
      await nativeWindow.evaluate((win) => {
        win.setPosition(0, 0);
        win.setSize(1260, 960);
        win.show();
        win.focus();
      });
      await window.goto(`${backend.serverUrl}/c/${backend.sessionId}`);
      await window
        .getByRole("button", { name: /^(Expand|Collapse) right panel$/ })
        .waitFor({ timeout: 30_000 });
      mark("session page open");
      const expand = window.getByRole("button", { name: "Expand right panel", exact: true });
      if (await expand.isVisible()) await expand.click();
      await window.waitForTimeout(1_500);
      await window.getByRole("button", { name: "Open new", exact: true }).click();
      mark("workspace + menu open");
      await window.waitForTimeout(1_500);
      await window.getByRole("menuitem", { name: "Browser", exact: true }).click();
      const pane = window.locator("[data-browser-pane-conversation]");
      await pane.waitFor({ timeout: 15_000 });
      mark("browser tab open");
      const viewId = await pane.getAttribute("data-browser-pane-conversation");
      observations.viewId = viewId;
      const address = window.getByRole("textbox", { name: "Address bar", exact: true });
      await window.waitForTimeout(1_500);

      await address.click();
      await address.pressSequentially(loopback.url, { delay: 40 });
      mark("loopback url typed");
      await window.waitForTimeout(1_000);
      await address.press("Enter");
      mark("enter pressed");
      observations.loopback = await observeNavigation(window, pane, viewId, loopback.url);
      mark(outcomeLabel(observations.loopback));
      // Hold the outcome on film before closing.
      await window.waitForTimeout(5_000);
    } finally {
      fs.mkdirSync(RECORD_DIR, { recursive: true });
      fs.writeFileSync(
        path.join(RECORD_DIR, "observations.json"),
        JSON.stringify(observations, null, 2),
      );
      // Stop filming while the pane still shows the outcome, then close; a
      // capture failure must not leak the shell.
      await stopDisplayCapture().catch(() => {});
      await electronApp.close().catch(() => {});
      try {
        saveRecording(RECORD_DIR, "url-bar-policy");
      } finally {
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
    }

    assert.equal(observations.loopback.timedOut, false, "neither a load nor an alert within 15 s");
    assert.equal(
      observations.loopback.viewExists,
      false,
      "a view was created for the rejected URL",
    );
    assert.equal(
      observations.loopback.loaded,
      false,
      `loopback URL typed into the address bar loaded in the pane (${observations.loopback.viewUrl})`,
    );
    assert.match(
      observations.loopback.alert ?? "",
      /navigation blocked/,
      'no "navigation blocked" alert under the toolbar',
    );
  });
});
