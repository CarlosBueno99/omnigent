// Tests for the agent-navigation URL allowlist (src/browserUrlPolicy.js),
// run with `node --test`. Pure function — no Electron needed.
//
// The security property under test: a model-issued `browser_navigate` must not
// be able to reach file://, cloud-metadata / loopback / private-range hosts,
// or any non-http(s) scheme (else it screenshots the bytes back out = SSRF +
// local-file read + exfil). Normal public https must still be allowed.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { isAgentNavigationAllowed } = require("../src/browserUrlPolicy");

describe("browserUrlPolicy — agent navigation allowlist", () => {
  it("allows only canonical loopback destinations with Arca eligibility", () => {
    for (const host of ["localhost", "127.0.0.1", "[::1]"]) {
      assert.equal(
        isAgentNavigationAllowed(`http://${host}:5173/`, { allowLocalhost: true }).ok,
        true,
      );
      assert.equal(isAgentNavigationAllowed(`https://${host}/`, { allowLocalhost: true }).ok, true);
      assert.equal(isAgentNavigationAllowed(`http://${host}/`).ok, false);
    }
    for (const url of [
      "http://app.localhost",
      "http://127.2.3.4",
      "http://0.0.0.0",
      "http://[::]",
      "http://169.254.169.254",
      "http://10.0.0.1",
      "http://172.16.0.1",
      "http://192.168.1.1",
      "file:///tmp/a",
      "data:text/html,test",
    ]) {
      assert.equal(isAgentNavigationAllowed(url, { allowLocalhost: true }).ok, false, url);
    }
  });
  it("allows ordinary public http(s) URLs", () => {
    for (const url of [
      "https://example.com/",
      "http://example.com/path?q=1",
      "https://sub.domain.example.org/a/b",
      "https://example.com:8443/x", // non-loopback host with a port is fine
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, true, `expected allow: ${url}`);
    }
  });

  it("rejects non-http(s) schemes (file/chrome/devtools/data/blob/javascript/about)", () => {
    for (const url of [
      "file:///home/user/.ssh/id_rsa",
      "file:///etc/passwd",
      "chrome://settings",
      "devtools://devtools/bundled/inspector.html",
      "data:text/html,<script>alert(1)</script>",
      "blob:https://example.com/uuid",
      "javascript:alert(document.cookie)",
      "about:blank",
    ]) {
      const v = isAgentNavigationAllowed(url);
      assert.equal(v.ok, false, `expected reject: ${url}`);
      assert.match(v.error, /navigation blocked/);
    }
  });

  it("blocks cloud metadata + link-local (169.254.0.0/16)", () => {
    for (const url of [
      "http://169.254.169.254/latest/meta-data/",
      "http://169.254.169.254/",
      "http://169.254.0.1/",
      "https://169.254.169.254/latest/meta-data/iam/security-credentials/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("blocks loopback: localhost, *.localhost, 127.0.0.0/8, ::1", () => {
    for (const url of [
      "http://localhost/",
      "http://localhost:6767/health",
      "http://app.localhost/",
      "http://127.0.0.1/",
      "http://127.0.0.1:8080/admin",
      "http://127.5.6.7/",
      "http://[::1]/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("blocks RFC-1918 private ranges (10/8, 172.16/12, 192.168/16)", () => {
    for (const url of [
      "http://10.0.0.1/",
      "http://10.255.255.255/",
      "http://172.16.0.1/",
      "http://172.20.10.5/",
      "http://172.31.255.255/",
      "http://192.168.0.1/",
      "http://192.168.1.100/internal",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("allows the 172.x hosts that are NOT in the private /12 (172.15, 172.32)", () => {
    assert.equal(isAgentNavigationAllowed("http://172.15.0.1/").ok, true);
    assert.equal(isAgentNavigationAllowed("http://172.32.0.1/").ok, true);
  });

  it("canonicalizes obfuscated IPv4 forms before checking (integer/hex/octal)", () => {
    // WHATWG URL normalizes these to 127.0.0.1 — the allowlist must still block.
    for (const url of [
      "http://2130706433/", // 127.0.0.1 as a 32-bit int
      "http://0x7f000001/", // 127.0.0.1 as hex
      "http://0177.0.0.1/", // 127.0.0.1 with an octal first octet
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("rejects empty / malformed / non-string input without throwing", () => {
    for (const bad of ["", "   ", "not a url", "://missing-scheme", null, undefined, 42]) {
      const v = isAgentNavigationAllowed(bad);
      assert.equal(v.ok, false);
    }
  });
  it("blocks IPv6 spellings of loopback, private, and metadata addresses", () => {
    for (const url of [
      "http://[::ffff:127.0.0.1]:8080/", // serialized as [::ffff:7f00:1]
      "http://[::ffff:7f00:1]/",
      "http://[::ffff:10.0.0.1]/",
      "http://[::ffff:192.168.1.1]/",
      "http://[::ffff:169.254.169.254]/latest/meta-data/",
      "http://[::ffff:100.100.100.200]/", // CGNAT-range metadata
      "http://[::127.0.0.1]/", // IPv4-compatible, serialized as [::7f00:1]
      "http://[::]/",
      "http://[0:0:0:0:0:0:0:1]/",
      "http://[64:ff9b::127.0.0.1]/", // NAT64
      "http://[2002:7f00:1::]/", // 6to4
      "http://[2001::1]/", // Teredo
      "http://[fc00::1]/", // unique-local
      "http://[fd12::1]/",
      "http://[fe80::1]/",
      "http://[febf::1]/", // top of fe80::/10
      "http://[fec0::1]/", // site-local
      "http://[ff02::1]/", // multicast
      "http://[2001:db8::1]/",
      "http://[2001:3::1]/", // conservative protocol-assignment exclusions
      "http://[2001:4:112::1]/",
      "http://[2001:1ff:ffff::1]/", // top of 2001::/23
      "http://[3fff::1]/", // documentation; global to ssrf.py, blocked here on purpose
      "http://[3fff:fff:ffff::1]/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("blocks shared, reserved, multicast and documentation IPv4 ranges", () => {
    for (const url of [
      "http://100.64.0.1/",
      "http://100.100.100.200/",
      "http://100.127.255.255/",
      "http://192.0.0.1/",
      "http://192.0.0.9/", // anycast; global to ssrf.py, blocked here on purpose
      "http://192.0.0.255/",
      "http://192.0.2.1/",
      "http://198.18.0.1/",
      "http://198.51.100.1/",
      "http://203.0.113.1/",
      "http://224.0.0.1/",
      "http://240.0.0.1/",
      "http://255.255.255.255/",
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, `expected reject: ${url}`);
    }
  });

  it("allows public IPv4 and IPv6 literals next to the blocked ranges", () => {
    for (const url of [
      "http://8.8.8.8/",
      "http://100.63.255.255/",
      "http://100.128.0.1/",
      "http://169.253.0.1/",
      "http://192.0.1.1/",
      "http://198.20.0.1/",
      "http://223.255.255.254/",
      "http://[2606:4700:4700::1111]/",
      "https://[2001:4860:4860::8888]/",
      "http://[2001:200::1]/", // first address past 2001::/23
      "http://[3fff:1000::1]/", // first address past 3fff::/20
      "http://[::ffff:8.8.8.8]/",
      "http://[64:ff9b::8.8.8.8]/", // NAT64 to a public IPv4
    ]) {
      assert.equal(isAgentNavigationAllowed(url).ok, true, `expected allow: ${url}`);
    }
  });

  it("blocks localhost names written with a trailing dot", () => {
    for (const url of ["http://localhost./", "http://app.localhost./"]) {
      assert.equal(isAgentNavigationAllowed(url).ok, false, url);
    }
  });

  it("keeps the Arca loopback exception to its exact hosts", () => {
    for (const url of ["http://[::ffff:127.0.0.1]:5173/", "http://[::127.0.0.1]/"]) {
      assert.equal(isAgentNavigationAllowed(url, { allowLocalhost: true }).ok, false, url);
    }
  });
});
