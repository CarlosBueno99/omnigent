// Drives the real IPC handler (src/browserIpc.js) and registry
// (src/browserViewRegistry.js) over a stub WebContentsView, run with
// `node --test`. The address bar sends `{ force: true }` with no agent flag
// (BrowserPane's submitUrl) and main cannot verify a renderer's claim of user
// intent, so the URL policy must gate every option shape the renderer can send.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { registerBrowserIpc } = require("../src/browserIpc");
const { createBrowserViewRegistry } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");

/** A fake ipcMain that records `handle(channel, fn)` registrations and lets a
 *  test invoke a channel with a synthetic event + args. */
function makeIpcMain() {
  const handlers = new Map();
  return {
    handle(channel, fn) {
      handlers.set(channel, fn);
    },
    invoke(channel, event, args) {
      const fn = handlers.get(channel);
      if (!fn) throw new Error(`no handler for ${channel}`);
      return fn(event, args);
    },
  };
}

function makeRealRegistryIpc({ isArcaAgentContext, getAgentContextForEvent } = {}) {
  const loaded = [];
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => ({
      setBounds() {},
      setVisible() {},
      webContents: {
        loadURL(url) {
          loaded.push(url);
        },
        getURL: () => loaded[loaded.length - 1] ?? "",
        close() {},
        on() {},
        removeListener() {},
        setWindowOpenHandler() {},
      },
    }),
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer() {},
    ...(isArcaAgentContext ? { isArcaAgentContext } : {}),
  });
  const ipcMain = makeIpcMain();
  registerBrowserIpc({
    ipcMain,
    isPinnedOriginSender: () => true,
    getRegistryForEvent: () => registry,
    ...(getAgentContextForEvent ? { getAgentContextForEvent } : {}),
  });
  const navigate = (url, opts) =>
    ipcMain.invoke(
      "omnigent:browser-open-or-navigate",
      { sender: { send() {} } },
      { conversationId: "browser-tab:conv_1:tab", url, opts },
    );
  return { navigate, loaded };
}

describe("browser IPC — URL policy at the renderer boundary", () => {
  it("rejects blocked destinations whatever intent the renderer claims, without loading them", async () => {
    const { navigate, loaded } = makeRealRegistryIpc();
    const accepted = [];
    // A renderer-supplied `mainOwned` must not opt out either.
    for (const opts of [
      undefined,
      {},
      { agent: false },
      { force: true },
      { agent: true },
      { mainOwned: true },
    ]) {
      for (const url of ["http://127.0.0.1:8080/", "file:///etc/passwd"]) {
        // oxlint-disable-next-line no-await-in-loop -- Sequential requests against one view.
        const r = await navigate(url, opts);
        if (r.ok) accepted.push(`${JSON.stringify(opts)} ${url}`);
        else assert.match(r.error ?? "", /navigation blocked/, url);
      }
    }
    assert.deepEqual(accepted, [], `accepted: ${accepted.join(", ")}`);
    assert.deepEqual(loaded, []);
  });

  it("still loads a public https URL typed into the address bar", async () => {
    const { navigate, loaded } = makeRealRegistryIpc();
    assert.equal((await navigate("https://example.com/", { force: true })).ok, true);
    assert.deepEqual(loaded, ["https://example.com/"]);
  });

  it("grants the Arca loopback exception by verified host, never by renderer flag", async () => {
    const { arcaTarget, isArcaAgentContext } = require("../src/arcaIdentity");
    const identity = {
      serverUrl: arcaTarget("https://workspace.example.com"),
      hostId: "a".repeat(32),
    };
    const { navigate, loaded } = makeRealRegistryIpc({
      isArcaAgentContext: (context) =>
        isArcaAgentContext(identity, context, {
          enabled: true,
          managed: true,
          serverTarget: identity.serverUrl,
        }),
      // Mirrors main.js: the renderer may name its session host; main owns the target.
      getAgentContextForEvent: (_event, sourceHostId) => ({
        serverTarget: identity.serverUrl,
        sourceHostId: typeof sourceHostId === "string" ? sourceHostId : null,
      }),
    });
    const url = "http://localhost:5173/";
    // The address bar supplies no host, so it never qualifies, flagged or not.
    assert.equal((await navigate(url, { force: true })).ok, false);
    assert.equal((await navigate(url, { agent: true })).ok, false);
    assert.deepEqual(loaded, []);
    // A relay request naming the captured Arca host does; other hosts stay blocked.
    assert.equal((await navigate(url, { force: true, sourceHostId: identity.hostId })).ok, true);
    assert.deepEqual(loaded, [url]);
    assert.equal((await navigate("http://10.0.0.1/", { sourceHostId: identity.hostId })).ok, false);
  });
});
