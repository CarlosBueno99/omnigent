import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";
import { authenticatedFetch } from "@/lib/identity";
import { HarnessFixDialog, type HarnessFixDialogProps } from "./HarnessFixDialog";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn().mockResolvedValue(undefined) }));
let databricksFeatures = true;
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    databricks_features: databricksFeatures,
    features: { harness_install: true },
    installable_harnesses: [
      "claude-native",
      "codex-native",
      "opencode-native",
      "pi-native",
      "qwen-native",
    ],
  }),
}));

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

const catalog = {
  data: [],
  setup_steps: {
    "codex-native": [
      {
        kind: "auth",
        title: "Sign in",
        detail: "",
        action: "command",
        command: "codex login",
        status_key: "authed",
      },
    ],
    "claude-native": [
      {
        kind: "auth",
        title: "Sign in",
        detail: "",
        action: "command",
        command: "claude auth login --claudeai",
        status_key: "authed",
      },
    ],
    "pi-native": [
      {
        kind: "auth",
        title: "Sign in",
        detail: "",
        action: "command",
        command: null,
        status_key: "authed",
      },
    ],
  },
};

const baseProps: HarnessFixDialogProps = {
  open: true,
  onOpenChange: vi.fn(),
  mode: "reconfigure",
  host: { host_id: "host/1", name: "Local laptop", owner: "me", status: "online" },
  harness: "codex-native",
  harnessLabel: "Codex",
  onFixed: vi.fn(),
};

function renderDialog(overrides: Partial<HarnessFixDialogProps> = {}) {
  const props = { ...baseProps, ...overrides };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <HarnessFixDialog {...props} />
    </QueryClientProvider>,
  );
  return props;
}

beforeEach(() => {
  databricksFeatures = true;
  vi.clearAllMocks();
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    if (String(input) === "/v1/harnesses") return json(catalog);
    throw new Error(`Unexpected URL: ${String(input)}`);
  });
});

describe("HarnessFixDialog", () => {
  it.each([
    ["install", "not installed"],
    ["update", "out of date"],
  ] as const)(
    "opens manual setup without an install request when Cursor is %s (%s)",
    async (mode, _state) => {
      renderDialog({ mode, harness: "cursor-native", harnessLabel: "Cursor" });

      expect(screen.getByRole("heading", { name: "Set up Cursor on Local laptop" })).toBeVisible();
      expect(
        screen.getByText((_, element) =>
          Boolean(
            element?.tagName === "P" &&
            element.textContent === "Run omni setup on Local laptop to finish setting up Cursor.",
          ),
        ),
      ).toBeVisible();
      expect(screen.getByRole("button", { name: "Copy omni setup" })).toBeVisible();
      expect(
        screen
          .getAllByRole("button", { name: "Close" })
          .find((button) => button.textContent === "Close"),
      ).toBeVisible();
      expect(screen.getByRole("button", { name: "Check again" })).toBeVisible();
      expect(
        vi
          .mocked(authenticatedFetch)
          .mock.calls.some(([input]) => String(input).endsWith("/install")),
      ).toBe(false);
    },
  );

  it("shows the Isaac setup fallback without calling the credential endpoint", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      throw new Error(`Unexpected URL: ${url} ${init?.method}`);
    });
    renderDialog();
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "Reconfigure Codex" })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Isaac/ })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(
      screen.getByRole("heading", { name: "Set up Isaac for Codex on Local laptop" }),
    ).toBeVisible();
    expect(
      screen.getByText("Run this in a terminal on Local laptop, then come back."),
    ).toBeVisible();
    expect(screen.getByText("omni setup")).toBeVisible();
    expect(
      screen.getByText("Choose Codex, then the Databricks Unity Gateway credential."),
    ).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Close" })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Check again" })).toBeVisible();
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.every(([input]) => !String(input).includes("/credential")),
    ).toBe(true);
  });

  it("finishes Isaac setup when Check again reports gateway readiness", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          configured_harnesses: { "codex-native": true },
          gateway_inference: { "codex-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "codex-native": true },
        credentialKind: "isaac",
        gatewayInference: { "codex-native": true },
      }),
    );
    expect(showToast).toHaveBeenCalledWith("Codex is ready on Local laptop");
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps Isaac setup open when Check again is not ready", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json({
          configured_harnesses: { "codex-native": "needs-auth" },
          gateway_inference: { "codex-native": false },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    expect(await screen.findByText("Still not signed in on Local laptop.")).toBeVisible();
    expect(props.onFixed).not.toHaveBeenCalled();
    expect(props.onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("shows Claude's server command and succeeds after Check again", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          configured_harnesses: { "claude-native": true },
          gateway_inference: null,
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ harness: "claude-native", harnessLabel: "Claude Code" });
    const user = userEvent.setup();

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(await screen.findByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(
      screen.getByRole("heading", { name: "Sign in to Claude Code on Local laptop" }),
    ).toBeVisible();
    expect(screen.getByText("claude auth login --claudeai")).toBeVisible();
    expect(
      screen.getByText(
        "Open the link it prints, then paste the code from your browser back into the terminal.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Copy login command" }));
    expect(copyText).toHaveBeenCalledWith("claude auth login --claudeai");
    expect(await screen.findByText("Copied")).toBeVisible();
    expect(showToast).toHaveBeenCalledWith("Copied to clipboard.");
    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "claude-native": true },
        credentialKind: "personal",
        gatewayInference: null,
      }),
    );
  });

  it("shows persistent guidance when copying a command fails", async () => {
    vi.mocked(copyText).mockRejectedValueOnce(new Error("clipboard unavailable"));
    renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Copy omni setup" }));

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith("Couldn't copy — select and copy manually.", {
        duration: 0,
      }),
    );
    expect(screen.getByRole("button", { name: "Copy omni setup" })).toHaveTextContent("Copy");
  });

  it("falls back to the Codex copy command when device login is unsupported", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login")) {
        return json(
          { error: { code: "login_unsupported", message: "Update the host." } },
          { status: 409 },
        );
      }
      if (url.endsWith("/harnesses/refresh")) {
        return json({
          configured_harnesses: { "codex-native": "needs-auth" },
          gateway_inference: { "codex-native": false },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(screen.getByText("codex login --device-auth")).toBeVisible();
    expect(screen.getByText("Open the link it prints and enter the one-time code.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText("Still not signed in on Local laptop.")).toBeVisible();
    expect(props.onFixed).not.toHaveBeenCalled();
    expect(props.onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("shows, polls, and completes Codex device-code sign-in", async () => {
    let loginChecks = 0;
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "login/1",
          verification_url: "https://auth.example.test/device",
          user_code: "WQ7K-3XPM",
          expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
        });
      }
      if (url.endsWith("/login/login%2F1")) {
        loginChecks += 1;
        return json(
          loginChecks === 1
            ? { status: "pending", error: null }
            : {
                status: "succeeded",
                error: null,
                configured_harnesses: { "codex-native": true },
                harness_accounts: {
                  "codex-native": {
                    kind: "chatgpt",
                    email: "codex@example.com",
                    plan: "Plus",
                  },
                },
              },
        );
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    expect(await screen.findByText("WQ7K-3XPM")).toBeVisible();
    expect(screen.getByRole("link", { name: /Open sign-in page/ })).toHaveAttribute(
      "href",
      "https://auth.example.test/device",
    );
    await user.click(screen.getByRole("button", { name: "Copy one-time code" }));
    expect(copyText).toHaveBeenCalledWith("WQ7K-3XPM");

    await waitFor(() => expect(loginChecks).toBeGreaterThanOrEqual(2), { timeout: 5_000 });
    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "codex-native": true },
        credentialKind: "personal",
        harnessAccounts: {
          "codex-native": {
            kind: "chatgpt",
            email: "codex@example.com",
            plan: "Plus",
          },
        },
      }),
    );
    expect(showToast).toHaveBeenCalledWith(
      "Codex is ready on Local laptop. Signed in as codex@example.com · Plus.",
    );
  }, 8_000);

  it("cancels an active Codex device login", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "active",
          verification_url: "https://auth.example.test/device",
          user_code: "ABCD-EFGH",
          expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
        });
      }
      if (url.endsWith("/login/active") && init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      if (url.endsWith("/login/active")) return json({ status: "pending", error: null });
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await screen.findByText("ABCD-EFGH");
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel).toBeEnabled();
    await user.click(cancel);

    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/hosts/host%2F1/harnesses/codex-native/login/active",
      { method: "DELETE" },
    );
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("shows a device-code error and retries", async () => {
    let starts = 0;
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        starts += 1;
        return json({
          login_id: `attempt-${starts}`,
          verification_url: "https://auth.example.test/device",
          user_code: "RETRY-ME",
          expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
        });
      }
      if (url.endsWith("/login/attempt-1")) {
        return json({ status: "failed", error: "The code was rejected." });
      }
      if (url.endsWith("/login/attempt-2")) return json({ status: "pending", error: null });
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    expect(await screen.findByText("The code was rejected.", {}, { timeout: 4_000 })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(starts).toBe(2));
    expect(await screen.findByText("RETRY-ME")).toBeVisible();
  }, 7_000);

  it("confirms Claude switching, logs out, and adds the email hint", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/logout") && init?.method === "POST") {
        return json({
          configured_harnesses: { "claude-native": "needs-auth" },
          harness_accounts: {},
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog({
      mode: "switch",
      harness: "claude-native",
      harnessLabel: "Claude Code",
      host: {
        ...baseProps.host,
        harness_accounts: {
          "claude-native": {
            kind: "subscription",
            email: "old@example.com",
            plan: "Pro",
          },
        },
      },
    });

    expect(screen.getByText(/Every session on Local laptop/)).toBeVisible();
    expect(screen.getByText("old@example.com · Pro")).toBeVisible();
    expect(screen.getByRole("dialog")).toHaveClass("bg-background", "text-foreground");
    const email = screen.getByRole("textbox", { name: "Account to sign in with" });
    expect(email).toHaveValue("");
    expect(email).toHaveAttribute("placeholder", "name@example.com");
    await user.type(email, "new@example.com");
    await user.click(screen.getByRole("button", { name: "Sign out and switch" }));

    expect(
      await screen.findByText("claude auth login --claudeai --email new@example.com"),
    ).toBeVisible();
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/hosts/host%2F1/harnesses/claude-native/logout",
      { method: "POST" },
    );
  });

  it("starts Codex device sign-in after switch logout", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/logout") && init?.method === "POST") {
        return json({
          configured_harnesses: { "codex-native": "needs-auth" },
          harness_accounts: {},
        });
      }
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "switched",
          verification_url: "https://auth.example.test/device",
          user_code: "NEW-LOGIN",
          expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
        });
      }
      if (url.endsWith("/login/switched")) return json({ status: "pending", error: null });
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog({ mode: "switch" });

    await user.click(screen.getByRole("button", { name: "Sign out and switch" }));
    expect(await screen.findByText("NEW-LOGIN")).toBeVisible();
  });

  it.each([
    [
      "unsupported",
      {
        error: {
          code: "refresh_unsupported",
          message: "This host cannot refresh harness readiness immediately.",
        },
      },
      409,
    ],
    ["a gateway timeout", { detail: "host readiness refresh timed out" }, 504],
  ])("falls back to the hosts query when immediate refresh is %s", async (_case, body, status) => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json(body, { status });
      }
      if (url === "/v1/hosts") {
        return json({
          hosts: [
            {
              host_id: "host/1",
              name: "Local laptop",
              owner: "me",
              status: "online",
              configured_harnesses: { "claude-native": true },
              gateway_inference: { "claude-native": false },
            },
          ],
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    const props = renderDialog({ harness: "claude-native", harnessLabel: "Claude Code" });

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "claude-native": true },
        credentialKind: "personal",
        gatewayInference: { "claude-native": false },
      }),
    );
  });

  it("labels a gateway-backed refresh as Isaac even from the personal phase", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json({
          configured_harnesses: { "claude-native": true },
          gateway_inference: { "claude-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ harness: "claude-native", harnessLabel: "Claude Code" });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "claude-native": true },
        credentialKind: "isaac",
        gatewayInference: { "claude-native": true },
      }),
    );
  });

  it("preserves the selected credential after an error and Try again", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json({ detail: "Refresh failed." }, { status: 500 });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog({ harness: "claude-native", harnessLabel: "Claude Code" });
    const user = userEvent.setup();

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(await screen.findByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));
    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(screen.getByRole("radio", { name: /Personal subscription/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("updates with a bodyless install request and accepts a null version", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          object: "harness_install",
          harness: "opencode-native",
          configured_harnesses: { "opencode-native": true },
          version: null,
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({
      mode: "update",
      harness: "opencode-native",
      harnessLabel: "OpenCode",
    });

    expect(screen.getByRole("heading", { name: "Updating OpenCode…" })).toBeVisible();
    expect(
      screen.getByText(/Updating OpenCode to the latest version on Local laptop/),
    ).toBeVisible();
    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "opencode-native": true },
        credentialKind: undefined,
      }),
    );
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/hosts/host%2F1/harnesses/opencode-native/install",
      { method: "POST" },
    );
  });

  it("installs without a body and then shows the selected Isaac setup", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          object: "harness_install",
          harness: "codex-native",
          configured_harnesses: { "codex-native": "needs-auth" },
          version: "1.0.0",
        });
      }
      if (url.endsWith("/harnesses/refresh")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          configured_harnesses: { "codex-native": true },
          gateway_inference: { "codex-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ mode: "install" });
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Install" }));
    expect(
      await screen.findByRole("heading", { name: "Set up Isaac for Codex on Local laptop" }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(props.onFixed).toHaveBeenCalled());
    expect(props.onFixed).toHaveBeenCalledWith({
      configuredHarnesses: { "codex-native": true },
      credentialKind: "isaac",
      gatewayInference: { "codex-native": true },
    });
  });

  it.each([
    ["claude-native", "Claude Code"],
    ["codex-native", "Codex"],
    ["pi-native", "Pi"],
  ])("offers and preselects Isaac for %s on Databricks", (harness, harnessLabel) => {
    renderDialog({ harness, harnessLabel });

    expect(screen.getByRole("radio", { name: /Isaac/ })).toHaveAttribute("aria-checked", "true");
  });

  it.each([
    ["claude-native", "Claude Code"],
    ["codex-native", "Codex"],
  ])("preselects Personal subscription for %s off Databricks", async (harness, harnessLabel) => {
    databricksFeatures = false;
    renderDialog({ harness, harnessLabel });

    expect(screen.queryByRole("radio", { name: /Isaac/ })).not.toBeInTheDocument();
    expect(await screen.findByRole("radio", { name: /Personal subscription/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("opens the API key and gateway form for Pi off Databricks", async () => {
    databricksFeatures = false;
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/credentials/detected")) return json({ credentials: [] });
      throw new Error(`Unexpected URL: ${url}`);
    });

    renderDialog({ harness: "pi-native", harnessLabel: "Pi" });

    expect(screen.queryByRole("radio", { name: /Isaac/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /Personal subscription/ })).not.toBeInTheDocument();
    expect(await screen.findByText("Use an API key")).toBeVisible();
    expect(screen.getByText("Connect a gateway")).toBeVisible();
  });

  it("does not offer credential forms before installing a harness", () => {
    renderDialog({ mode: "install", harness: "pi-native", harnessLabel: "Pi" });

    expect(screen.getByRole("radio", { name: /Isaac/ })).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Use an API key or gateway URL instead" }),
    ).not.toBeInTheDocument();
  });

  it("keeps API key and gateway forms reachable through the secondary link", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/credentials/detected")) return json({ credentials: [] });
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Use an API key or gateway URL instead" }));

    expect(screen.getByText("Use an API key")).toBeVisible();
    expect(screen.getByText("Connect a gateway")).toBeVisible();
    expect(screen.getByPlaceholderText("Paste an API key")).toBeVisible();
    expect(screen.getByPlaceholderText(/Gateway base URL/)).toBeVisible();
    expect(screen.queryByText("Sign in with your subscription")).not.toBeInTheDocument();
  });

  it("installs a cardless harness before showing the omni setup fallback", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          object: "harness_install",
          harness: "opencode-native",
          configured_harnesses: { "opencode-native": "needs-auth" },
          version: "1.2.3",
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog({ mode: "install", harness: "opencode-native", harnessLabel: "OpenCode" });
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "Install OpenCode" })).toBeVisible();
    expect(screen.getByText("Install OpenCode on Local laptop.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Install" }));

    expect(
      await screen.findByRole("heading", { name: "Set up OpenCode on Local laptop" }),
    ).toBeVisible();
    expect(screen.getAllByText("omni setup")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Check again" })).toBeVisible();
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.some(([input]) => String(input).endsWith("/install")),
    ).toBe(true);
  });

  it("never shows credential forms for a cardless harness", () => {
    renderDialog({ harness: "qwen-native", harnessLabel: "Qwen" });

    expect(screen.getByRole("heading", { name: "Set up Qwen on Local laptop" })).toBeVisible();
    expect(screen.getAllByText("omni setup")).toHaveLength(2);
    expect(screen.queryByText("Use an API key")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Paste an API key")).not.toBeInTheDocument();
  });

  it("routes an updated but unready harness back to credential choice", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        return json({
          object: "harness_install",
          harness: "codex-native",
          configured_harnesses: { "codex-native": "needs-auth" },
          version: "2.0.0",
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ mode: "update" });

    expect(
      await screen.findByText("Codex was updated but still needs to be set up on Local laptop."),
    ).toBeVisible();
    expect(screen.getByRole("radio", { name: /Isaac/ })).toBeVisible();
    expect(props.onFixed).not.toHaveBeenCalled();
  });

  it("passes the installed version to the ready notice", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        return json({
          object: "harness_install",
          harness: "opencode-native",
          configured_harnesses: { "opencode-native": true },
          version: "2.4.0",
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({
      mode: "update",
      harness: "opencode-native",
      harnessLabel: "OpenCode",
    });

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "opencode-native": true },
        credentialKind: undefined,
        version: "2.4.0",
      }),
    );
  });

  it("uses the credential response directly without refreshing readiness", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/credentials/detected")) return json({ credentials: [] });
      if (url.endsWith("/credential")) {
        expect(init?.method).toBe("POST");
        return json({
          object: "harness_credential",
          harness: "codex-native",
          configured_harnesses: { "codex-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Use an API key or gateway URL instead" }));
    await user.type(screen.getByPlaceholderText("Paste an API key"), "secret");
    await user.click(screen.getAllByRole("button", { name: "Save" })[0]);

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "codex-native": true },
        credentialKind: undefined,
      }),
    );
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith("Codex is ready on Local laptop.");
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.every(([input]) => !String(input).endsWith("/refresh")),
    ).toBe(true);
  });

  it("disables Check again while the readiness request is in flight", async () => {
    let resolveRefresh: ((response: Response) => void) | undefined;
    const refresh = new Promise<Response>((resolve) => {
      resolveRefresh = resolve;
    });
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) return refresh;
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    expect(screen.getByRole("button", { name: "Checking…" })).toBeDisabled();
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.filter(([input]) => String(input).endsWith("/harnesses/refresh")),
    ).toHaveLength(1);
    resolveRefresh?.(
      json({
        configured_harnesses: { "codex-native": "needs-auth" },
        gateway_inference: null,
      }),
    );
    expect(await screen.findByRole("button", { name: "Check again" })).toBeEnabled();
  });

  it("includes reversed native aliases in credential families", async () => {
    renderDialog({ harness: "native-codex", harnessLabel: "Codex" });
    expect(screen.getByRole("radio", { name: /Isaac/ })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Personal subscription/ })).toBeVisible();
  });
});
