"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const packageConfig = require("../package.json");

const APP_DIR = path.resolve(__dirname, "..");
const PRODUCT_NAME = packageConfig.build.productName;
const EXECUTABLE = packageConfig.name.toLowerCase();
const INSTALL_DIR = `/opt/${PRODUCT_NAME}`;
const CHROME_SANDBOX = `${INSTALL_DIR}/chrome-sandbox`;

/** Every command the post-install script runs that could modify the host. */
const HOST_TOOLS = [
  "chmod",
  "cp",
  "rm",
  "ln",
  "readlink",
  "update-alternatives",
  "update-mime-database",
  "update-desktop-database",
  "apparmor_parser",
];

const DEB_AFTER_INSTALL = path.resolve(APP_DIR, packageConfig.build.deb.afterInstall);

/** electron-builder's default deb after-install template, or null when it is not installed. */
function upstreamAfterInstallTemplate() {
  try {
    const electronBuilderDir = path.dirname(require.resolve("electron-builder/package.json"));
    const appBuilderLib = path.dirname(
      require.resolve("app-builder-lib/package.json", { paths: [electronBuilderDir] }),
    );
    return path.join(appBuilderLib, "templates", "linux", "after-install.tpl");
  } catch {
    return null;
  }
}

/** Expand the template's macros as FpmTarget does, using the release build's names. */
function renderDebScript(templatePath) {
  const options = {
    executable: EXECUTABLE,
    sanitizedProductName: PRODUCT_NAME,
    productFilename: PRODUCT_NAME,
    ...packageConfig.build.linux,
  };
  return fs.readFileSync(templatePath, "utf8").replace(/\${([a-zA-Z]+)}/g, (_match, name) => {
    if (!(name in options)) throw new Error(`Macro ${name} is not defined`);
    return options[name];
  });
}

function writeStub(dir, name, body) {
  fs.writeFileSync(path.join(dir, name), `#!/bin/bash\n${body}\n`, { mode: 0o755 });
}

/** Run the rendered script with host tools stubbed; return the recorded calls in order. */
function runPostInstall({ apparmorEnabled = false } = {}) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-deb-postinst-"));
  try {
    const bin = path.join(tmp, "bin");
    fs.mkdirSync(bin);
    const callLog = path.join(tmp, "calls.log");
    const recordCalls = (name, exitCode = 0) =>
      writeStub(bin, name, `printf '%s\\n' "${name} $*" >> '${callLog}'\nexit ${exitCode}`);
    recordCalls("unshare");
    for (const tool of HOST_TOOLS) recordCalls(tool);
    recordCalls("apparmor_status", apparmorEnabled ? 0 : 1);
    const script = path.join(tmp, "after-install.sh");
    fs.writeFileSync(script, renderDebScript(DEB_AFTER_INSTALL));
    const result = spawnSync("bash", [script], {
      encoding: "utf8",
      env: { PATH: `${bin}:/usr/bin:/bin`, LANG: "C" },
    });
    assert.equal(result.status, 0, result.stderr);
    return fs.existsSync(callLog) ? fs.readFileSync(callLog, "utf8").trim().split("\n") : [];
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

function chromeSandboxModes(calls) {
  return calls
    .filter((call) => call.startsWith("chmod ") && call.endsWith(` ${CHROME_SANDBOX}`))
    .map((call) => call.split(" ")[1]);
}

/** Script paragraphs other than the chrome-sandbox setup, without comment lines. */
function stepsBesidesSandboxSetup(script) {
  return script
    .split(/\n\s*\n/)
    .filter((paragraph) => !paragraph.includes("chrome-sandbox"))
    .map((paragraph) =>
      paragraph
        .split("\n")
        .filter((line) => !/^\s*#(?!!)/.test(line))
        .join("\n"),
    );
}

describe("Linux .deb packaging", { skip: process.platform !== "linux" }, () => {
  it("installs chrome-sandbox setuid root without probing user namespaces as root", () => {
    // Ubuntu with apparmor_restrict_unprivileged_userns=1: root's probe succeeds while the
    // desktop user's Chromium still cannot use the namespace sandbox.
    const calls = runPostInstall();
    assert.equal(chromeSandboxModes(calls).at(-1), "4755", `calls: ${JSON.stringify(calls)}`);
    assert.ok(!calls.some((call) => call.startsWith("unshare")), `calls: ${JSON.stringify(calls)}`);
  });

  it("keeps electron-builder's other post-install steps", () => {
    const calls = runPostInstall({ apparmorEnabled: true });
    for (const expected of [
      `update-alternatives --install /usr/bin/${EXECUTABLE} ${EXECUTABLE} ${INSTALL_DIR}/${EXECUTABLE} 100`,
      "update-mime-database /usr/share/mime",
      "update-desktop-database /usr/share/applications",
      `apparmor_parser --skip-kernel-load --debug ${INSTALL_DIR}/resources/apparmor-profile`,
      `cp -f ${INSTALL_DIR}/resources/apparmor-profile /etc/apparmor.d/${EXECUTABLE}`,
    ]) {
      assert.ok(calls.includes(expected), `missing ${expected} in ${JSON.stringify(calls)}`);
    }
  });

  const upstreamTemplate = upstreamAfterInstallTemplate();
  it(
    "matches electron-builder's template apart from the chrome-sandbox setup",
    { skip: !upstreamTemplate && "electron-builder is not installed" },
    () => {
      assert.ok(
        fs.existsSync(upstreamTemplate),
        `re-sync with electron-builder: ${upstreamTemplate} is gone`,
      );
      assert.deepEqual(
        stepsBesidesSandboxSetup(fs.readFileSync(DEB_AFTER_INSTALL, "utf8")),
        stepsBesidesSandboxSetup(fs.readFileSync(upstreamTemplate, "utf8")),
        `re-sync ${packageConfig.build.deb.afterInstall} with ${upstreamTemplate}`,
      );
    },
  );
});
