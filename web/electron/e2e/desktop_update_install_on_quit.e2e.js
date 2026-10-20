// A downloaded update installs on quit, including when the setting is turned on after the
// download. Linux stand-in: AppImageUpdater replaces APPIMAGE with a stub payload at quit.
// Run from web/electron after `pnpm --filter web run build && pnpm --filter web run build:overlay`:
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_update_install_on_quit.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { setTimeout: delay } = require("node:timers/promises");

const {
  APP_ROOT,
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const RECORD_DIR =
  process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
  path.join(__dirname, "recordings", "desktop-update-install-on-quit");
const OVERLAY_PAGE = path.join(APP_ROOT, "overlay", "update-overlay.html");
const FEED_VERSION = "9999.0.0";
const PAYLOAD_NAME = `Omnigent-${FEED_VERSION}-x86_64.AppImage`;
const QUIT_EXIT_WINDOW_MS = 20_000;
const VIEWER_PAUSE_MS = 2_500;

function payloadScript(markerPath) {
  return [
    "#!/bin/sh",
    `printf 'installed=${FEED_VERSION} exit_after_install=%s silent=%s\\n' ` +
      `"$APPIMAGE_EXIT_AFTER_INSTALL" "$APPIMAGE_SILENT_INSTALL" > '${markerPath}'`,
    "",
  ].join("\n");
}

function startFeed(payload) {
  const sha512 = crypto.createHash("sha512").update(payload).digest("base64");
  const yml = [
    `version: ${FEED_VERSION}`,
    "files:",
    `  - url: ${PAYLOAD_NAME}`,
    `    sha512: ${sha512}`,
    `    size: ${payload.length}`,
    `path: ${PAYLOAD_NAME}`,
    `sha512: ${sha512}`,
    "releaseDate: '2026-01-01T00:00:00.000Z'",
    "",
  ].join("\n");
  const requests = [];
  return new Promise((resolve) => {
    const feed = http.createServer((req, res) => {
      const pathname = new URL(req.url, "http://feed.invalid").pathname;
      requests.push(pathname);
      if (pathname.endsWith(".yml")) {
        res.writeHead(200, { "content-type": "text/yaml" });
        res.end(yml);
      } else if (pathname === `/${PAYLOAD_NAME}`) {
        res.writeHead(200, {
          "content-type": "application/octet-stream",
          "content-length": payload.length,
        });
        res.end(payload);
      } else {
        res.writeHead(404);
        res.end();
      }
    });
    feed.listen(0, "127.0.0.1", () => {
      resolve({
        url: `http://127.0.0.1:${feed.address().port}/`,
        requests,
        close: () =>
          new Promise((r) => {
            feed.close(r);
          }),
      });
    });
  });
}

function seedProfile(userDataDir, settings) {
  for (const profile of [userDataDir, path.join(userDataDir, "Omnigent Dev")]) {
    fs.mkdirSync(profile, { recursive: true });
    fs.writeFileSync(path.join(profile, "settings.json"), JSON.stringify(settings, null, 2));
  }
}

async function findShellWindow(electronApp, firstWindow) {
  let window = firstWindow;
  /* oxlint-disable no-await-in-loop */
  for (let i = 0; i < 50 && window.url().includes("update-overlay"); i++) {
    const shell = electronApp.windows().find((p) => !p.url().includes("update-overlay"));
    if (shell) {
      window = shell;
      break;
    }
    await firstWindow.waitForTimeout(500);
  }
  /* oxlint-enable no-await-in-loop */
  assert.ok(!window.url().includes("update-overlay"), "shell window never appeared");
  return window;
}

async function publishUpdate(electronApp, window, feedUrl) {
  const updaterPath = fs.realpathSync(require.resolve("electron-updater", { paths: [APP_ROOT] }));
  let checked = "";
  /* oxlint-disable no-await-in-loop */
  for (let attempt = 0; attempt < 5 && !checked.startsWith("ok"); attempt++) {
    checked = await electronApp.evaluate(
      async (_electron, { url, updaterPath: modulePath }) => {
        const req = typeof require === "function" ? require : process.mainModule.require;
        const { autoUpdater } = req(modulePath);
        autoUpdater.setFeedURL({ provider: "generic", url });
        try {
          const res = await autoUpdater.checkForUpdates();
          return "ok " + (res && res.updateInfo && res.updateInfo.version);
        } catch (err) {
          return "err " + (err && err.message);
        }
      },
      { url: feedUrl, updaterPath },
    );
    if (!checked.startsWith("ok")) await window.waitForTimeout(2_000);
  }
  /* oxlint-enable no-await-in-loop */
  assert.equal(checked, `ok ${FEED_VERSION}`, `update check never succeeded: ${checked}`);
}

function pageMatching(electronApp, fragment) {
  return electronApp.windows().find((page) => page.url().includes(fragment));
}

async function waitForPage(electronApp, fragment, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  /* oxlint-disable no-await-in-loop */
  while (Date.now() < deadline) {
    const page = pageMatching(electronApp, fragment);
    if (page) return page;
    await delay(100);
  }
  /* oxlint-enable no-await-in-loop */
  throw new Error(`no window matching ${fragment}`);
}

async function downloadFromCard(electronApp, window) {
  const overlay = await waitForPage(electronApp, "update-overlay");
  await overlay.getByText("is available").waitFor({ state: "visible", timeout: 15_000 });
  await window.waitForTimeout(VIEWER_PAUSE_MS);
  await overlay.getByRole("button", { name: "Update now" }).click();
  const about = await waitForPage(electronApp, "about/index.html");
  await about
    .getByText(`Desktop update ${FEED_VERSION} is ready to install.`)
    .waitFor({ state: "visible", timeout: 30_000 });
  await window.waitForTimeout(VIEWER_PAUSE_MS);
  await about.getByRole("button", { name: "Close", exact: true }).click();
  await overlay.getByText("is ready to install").waitFor({ state: "visible", timeout: 15_000 });
  await window.waitForTimeout(VIEWER_PAUSE_MS);
  return overlay;
}

async function clickMenuItem(electronApp, menuLabel, itemLabel) {
  return electronApp.evaluate(
    ({ Menu, BrowserWindow }, { menuLabel: top, itemLabel: label }) => {
      const menu = Menu.getApplicationMenu();
      const submenu = menu.items.find((item) => item.label === top)?.submenu;
      const item = submenu?.items.find((entry) => entry.label === label);
      if (!item) return `missing ${top} > ${label}`;
      const shell = BrowserWindow.getAllWindows().find((w) =>
        /^https?:/.test(w.webContents.getURL()),
      );
      setImmediate(() => item.click(undefined, shell, shell?.webContents));
      return "clicked";
    },
    { menuLabel, itemLabel },
  );
}

/** Electron's Quit role (macOS Cmd+Q); the Linux application menu has no Quit item. */
async function clickQuitRole(electronApp) {
  return electronApp.evaluate(({ Menu }) => {
    const [quit] = Menu.buildFromTemplate([{ role: "quit" }]).items;
    setImmediate(() => quit.click());
    return "clicked";
  });
}

function waitForExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve({ code: child.exitCode, signal: child.signalCode, elapsedMs: 0 });
  }
  const started = Date.now();
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), timeoutMs);
    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, elapsedMs: Date.now() - started });
    });
  });
}

async function runInstallOnQuitJourney({ name, autoInstallAtLaunch, beforeQuit, quit }) {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), `omni-install-on-quit-${name}-`));
  const markerPath = path.join(tmpDir, "payload-ran.txt");
  const installedAppImage = path.join(tmpDir, "Omnigent.AppImage");
  const oldAppImage = "#!/bin/sh\necho 'Omnigent 999.0.0'\n";
  fs.writeFileSync(installedAppImage, oldAppImage, { mode: 0o755 });
  const payload = Buffer.from(payloadScript(markerPath));
  const feed = await startFeed(payload);
  const userDataDir = path.join(tmpDir, "profile");
  const settings = { server_url: server.serverUrl };
  if (autoInstallAtLaunch === false) settings.update_auto_install = false;
  seedProfile(userDataDir, settings);

  const {
    electronApp,
    window: firstWindow,
    stopDisplayCapture,
  } = await launchDesktop({
    recordDir: RECORD_DIR,
    userDataDir,
    env: { APPIMAGE: installedAppImage, XDG_CACHE_HOME: path.join(tmpDir, "cache") },
  });
  const child = electronApp.process();
  const mainLog = [];
  for (const stream of [child.stdout, child.stderr]) {
    stream?.on("data", (chunk) => mainLog.push(String(chunk)));
  }
  const result = { name };
  try {
    const window = await findShellWindow(electronApp, firstWindow);
    await window.waitForLoadState("domcontentloaded");
    await window.getByText("What should we build?").waitFor({ state: "visible", timeout: 20_000 });

    await publishUpdate(electronApp, window, feed.url);
    const overlay = await downloadFromCard(electronApp, window);
    result.cardPromisesInstallOnQuit = await overlay
      .getByText("Installs automatically on next quit.")
      .isVisible();
    if (beforeQuit) Object.assign(result, await beforeQuit({ electronApp, window, overlay }));
    result.autoInstallOnAppQuitBeforeQuit = await electronApp.evaluate(
      (_electron, updaterPath) => {
        const req = typeof require === "function" ? require : process.mainModule.require;
        return req(updaterPath).autoUpdater.autoInstallOnAppQuit;
      },
      fs.realpathSync(require.resolve("electron-updater", { paths: [APP_ROOT] })),
    );
    await window.waitForTimeout(1_000);

    result.quitAction = await quit(electronApp);
    result.exit = await waitForExit(child, QUIT_EXIT_WINDOW_MS);
    // Keep filming briefly so the clip ends on the closed app.
    await delay(VIEWER_PAUSE_MS);
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      await electronApp.close().catch(() => {});
    }
    await stopDisplayCapture();
    result.recordings = saveRecording(RECORD_DIR, name);
    await feed.close();
    result.feedRequests = feed.requests;
    result.installedAppImageNow = fs.existsSync(installedAppImage)
      ? fs.readFileSync(installedAppImage, "utf8")
      : null;
    result.appImageReplaced = result.installedAppImageNow === payload.toString();
    result.payloadMarker = fs.existsSync(markerPath) ? fs.readFileSync(markerPath, "utf8") : null;
    result.updaterLog = mainLog
      .join("")
      .split("\n")
      .filter((line) =>
        /install|update error|quit|Cannot download differentially|Downloading|downloaded/i.test(
          line,
        ),
      )
      .map((line) => line.slice(0, 300));
    fs.writeFileSync(path.join(RECORD_DIR, `${name}-main.log`), mainLog.join(""));
    fs.writeFileSync(path.join(RECORD_DIR, `${name}-result.json`), JSON.stringify(result, null, 2));
    console.log(`[${name}] ${JSON.stringify(result, null, 2)}`);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
  return result;
}

let server;
let serverTmp;

describe(
  "desktop shell — install downloaded update on quit",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    before(async () => {
      assert.ok(fs.existsSync(OVERLAY_PAGE), `overlay bundle missing at ${OVERLAY_PAGE}`);
      serverTmp = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-e2e-"));
      server = await spawnServer(serverTmp);
    });

    after(async () => {
      if (server) await server.close();
      if (serverTmp) fs.rmSync(serverTmp, { recursive: true, force: true });
    });

    it("installs the downloaded update when the app quits through the Quit role", async () => {
      const result = await runInstallOnQuitJourney({
        name: "quit-role",
        quit: clickQuitRole,
      });
      assert.equal(result.cardPromisesInstallOnQuit, true);
      assert.ok(result.exit, "app did not exit after Quit");
      assert.ok(result.appImageReplaced, `update not installed on quit: ${JSON.stringify(result)}`);
      assert.match(result.payloadMarker ?? "", /exit_after_install=true/);
    });

    it("installs the downloaded update when the last window is closed", async () => {
      const result = await runInstallOnQuitJourney({
        name: "close-window",
        quit: (electronApp) => clickMenuItem(electronApp, "Server", "Close Window"),
      });
      assert.equal(result.cardPromisesInstallOnQuit, true);
      assert.ok(result.exit, "app did not exit after closing the last window");
      assert.ok(result.appImageReplaced, `update not installed on quit: ${JSON.stringify(result)}`);
      assert.match(result.payloadMarker ?? "", /exit_after_install=true/);
    });

    it("installs on quit when the setting is turned on after the download", async () => {
      const result = await runInstallOnQuitJourney({
        name: "enable-after-download",
        autoInstallAtLaunch: false,
        beforeQuit: async ({ electronApp, window, overlay }) => {
          const consent = await electronApp.evaluate(({ dialog }) => {
            global.omnigentTestConsentPrompts = [];
            dialog.showMessageBox = async (_win, options) => {
              global.omnigentTestConsentPrompts.push(options?.message ?? String(options));
              return { response: 1, checkboxChecked: false };
            };
            return "stubbed";
          });
          await clickMenuItem(electronApp, "Server", "Settings…");
          await window.waitForURL(/\/settings/, { timeout: 15_000 });
          await window.getByTestId("settings-nav-updates").click();
          const toggle = window.getByRole("switch", {
            name: "Install downloaded updates on next quit",
          });
          await toggle.waitFor({ state: "visible", timeout: 15_000 });
          const settingBefore = await toggle.getAttribute("aria-checked");
          await window.waitForTimeout(VIEWER_PAUSE_MS);
          await toggle.click();
          await window
            .getByRole("switch", { name: "Install downloaded updates on next quit", checked: true })
            .waitFor({ timeout: 10_000 });
          await window.waitForTimeout(VIEWER_PAUSE_MS);
          return {
            consent,
            consentPrompts: await electronApp.evaluate(() => global.omnigentTestConsentPrompts),
            settingBefore,
            settingAfter: await toggle.getAttribute("aria-checked"),
            cardPromisesInstallOnQuitAfterSetting: await overlay
              .getByText("Installs automatically on next quit.")
              .isVisible(),
          };
        },
        quit: clickQuitRole,
      });
      assert.equal(result.settingAfter, "true");
      assert.ok(result.exit, "app did not exit after Quit");
      assert.ok(
        result.appImageReplaced,
        `setting is on but the update was not installed on quit: ${JSON.stringify(result)}`,
      );
    });
  },
);
