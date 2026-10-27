import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { useSession } from "@/hooks/useSession";
import * as sessionsApi from "@/lib/sessionsApi";
import * as identity from "@/lib/identity";
import type { Session, NativeModelOption } from "@/lib/types";
import type * as AgentLabels from "@/lib/agentLabels";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useChatStore } from "@/store/chatStore";
import { SideChatSettings } from "./SideChatSettings";

vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabels>()),
  useHarnessEffortFamilies: () => ({ "claude-sdk": "anthropic", codex: "openai" }),
}));

const models: NativeModelOption[] = [
  {
    id: "gpt-5.5",
    displayName: "GPT 5.5",
    defaultReasoningEffort: "medium",
    supportedReasoningEfforts: [{ reasoningEffort: "medium" }, { reasoningEffort: "high" }],
  },
  {
    id: "gpt-6-sol",
    displayName: "GPT 6",
    defaultReasoningEffort: "low",
    supportedReasoningEfforts: [{ reasoningEffort: "low" }, { reasoningEffort: "max" }],
  },
];
const parent = {
  id: "parent",
  modelOverride: "gpt-5.5",
  reasoningEffort: "high",
  codexModelOptions: models,
} as Session;
const child = {
  agentId: "agent",
  agentName: "Codex",
  status: "idle",
  createdAt: 0,
  title: "Side chat",
  items: [],
  subAgentName: null,
  kind: "sub_agent",
  id: "side",
  parentSessionId: "parent",
  harness: "codex-native",
  permissionLevel: 4,
  modelOverride: "gpt-5.5",
  reasoningEffort: "high",
  labels: {
    "omnigent.wrapper": "codex-native-ui-subagent",
    "omnigent.codex_native.agent_nickname": "Side chat",
  },
} as Session;
let cache: QueryClient;

function Pane() {
  const { session } = useSession("side");
  return session ? <SideChatSettings session={session} /> : null;
}

function mount(session = child) {
  cache.setQueryData(["session", "side"], session);
  cache.setQueryData(["session", "parent"], parent);
  render(
    <QueryClientProvider client={cache}>
      <Pane />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  useChatStore.setState({ conversationId: "parent", sideChatComposers: {} });
  vi.spyOn(toast, "error").mockReturnValue("error");
  vi.spyOn(sessionsApi, "updateSession").mockImplementation(
    async (id, updates) =>
      ({ ...cache.getQueryData<Session>(["session", id]), ...updates }) as Session,
  );
});

afterEach(() => {
  cleanup();
  cache.clear();
  conversationRegistry.clear();
  vi.restoreAllMocks();
});

it("changes only the side session and uses the selected model's effort ladder", async () => {
  mount();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "GPT 6" }));
  await waitFor(() =>
    expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", {
      modelOverride: "gpt-6-sol",
      reasoningEffort: "low",
    }),
  );
  await waitFor(() => expect(screen.getByRole("menuitemcheckbox", { name: "Max" })).toBeEnabled());
  expect(screen.queryByRole("menuitemcheckbox", { name: "High" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Max" }));
  await waitFor(() =>
    expect(sessionsApi.updateSession).toHaveBeenLastCalledWith("side", { reasoningEffort: "max" }),
  );
  expect(cache.getQueryData(["session", "parent"])).toEqual(parent);
  expect(useChatStore.getState().conversationId).toBe("parent");
});

it("keeps the saved selection when the server rejects a change", async () => {
  vi.mocked(sessionsApi.updateSession).mockRejectedValue(new Error("Model unavailable"));
  mount();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "GPT 6" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Model unavailable");
  expect(cache.getQueryData(["session", "side"])).toEqual(child);
  expect(screen.getByRole("menuitemcheckbox", { name: "GPT 5.5" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
});

it("offers SDK effort controls without inventing a model catalog", async () => {
  mount({
    ...child,
    parentSessionId: null,
    harness: "claude-sdk",
    labels: {},
    modelOverride: "claude-sonnet",
    reasoningEffort: "high",
  });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  expect(screen.queryByTestId("side-chat-models")).not.toBeInTheDocument();
  await user.click(screen.getByRole("menuitemcheckbox", { name: "Low" }));
  expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", { reasoningEffort: "low" });
});

it("keeps compatible effort when changing a configured generic side chat model", async () => {
  mount({
    ...child,
    parentSessionId: null,
    harness: "codex",
    labels: {},
    inferenceConfigured: true,
    codexModelOptions: models,
  });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "GPT 6" }));
  await waitFor(() =>
    expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", {
      modelOverride: "gpt-6-sol",
      reasoningEffort: "high",
    }),
  );
  cleanup();
  render(
    <QueryClientProvider client={cache}>
      <Pane />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toHaveTextContent(
    "GPT 6High",
  );
  expect(cache.getQueryData(["session", "parent"])).toEqual(parent);
});

it("changes only the model on a harness without effort controls", async () => {
  mount({
    ...child,
    parentSessionId: null,
    harness: "cursor-native",
    labels: { "omnigent.wrapper": "cursor-native-ui" },
    codexModelOptions: models,
    reasoningEffort: null,
  });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  expect(screen.queryByTestId("side-chat-efforts")).not.toBeInTheDocument();
  await user.click(screen.getByRole("menuitemcheckbox", { name: "GPT 6" }));
  expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", { modelOverride: "gpt-6-sol" });
});

it("does not present a catalog default as the side chat's inherited effort", () => {
  mount({ ...child, reasoningEffort: null });
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toHaveTextContent(
    "GPT 5.5",
  );
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).not.toHaveTextContent(
    "medium",
  );
});

it.each([1, 4])("disables settings for a viewer or a closed side chat", (permissionLevel) => {
  mount({
    ...child,
    permissionLevel,
    labels: { ...child.labels, ...(permissionLevel === 4 ? { "omnigent.closed": "true" } : {}) },
  });
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toBeDisabled();
});

function DraftPane({ id }: { id: string }) {
  const draft = useChatStore((state) => state.sideChatComposers[id]);
  return (
    <SideChatSettings
      session={{
        ...child,
        id: "parent",
        parentSessionId: null,
        codexModelOptions: models,
        labels: { "omnigent.wrapper": "codex-native-ui" },
      }}
      draft={{
        settings: draft?.settings,
        disabled: draft?.starting ?? false,
        onChange: (updates) =>
          useChatStore.getState().updateSideChatComposer(id, (current) => ({
            ...current,
            settings: { ...current.settings, ...updates },
          })),
      }}
    />
  );
}

it("keeps unsent settings per draft across remounts without updating the parent", async () => {
  const user = userEvent.setup();
  const view = render(
    <QueryClientProvider client={cache}>
      <DraftPane id="pending:one" />
    </QueryClientProvider>,
  );
  await user.click(screen.getByRole("button", { name: "Side chat model and effort" }));
  await user.click(screen.getByTestId("side-chat-model-edit"));
  await user.click(screen.getByRole("menuitemcheckbox", { name: "GPT 6" }));
  expect(useChatStore.getState().sideChatComposers["pending:one"].settings).toEqual({
    modelOverride: "gpt-6-sol",
    reasoningEffort: "low",
  });
  expect(sessionsApi.updateSession).not.toHaveBeenCalled();
  view.unmount();
  const remount = render(
    <QueryClientProvider client={cache}>
      <DraftPane id="pending:one" />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toHaveTextContent(
    "GPT 6Low",
  );
  act(() =>
    useChatStore
      .getState()
      .updateSideChatComposer("pending:one", (draft) => ({ ...draft, starting: true })),
  );
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toBeDisabled();
  remount.rerender(
    <QueryClientProvider client={cache}>
      <DraftPane id="pending:two" />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Side chat model and effort" })).toHaveTextContent(
    "GPT 5.5High",
  );
});

it.each([false, true])(
  "uses the host catalog when session models are empty (draft=%s)",
  async (draft) => {
    const source = { ...parent, hostId: "host", codexModelOptions: [] };
    cache.setQueryData(["session", "parent"], source);
    const fetch = vi
      .spyOn(identity, "authenticatedFetch")
      .mockResolvedValue(new Response(JSON.stringify({ models }), { status: 200 }));
    const onChange = vi.fn();
    const session = draft
      ? { ...child, id: "parent", parentSessionId: null, hostId: "host", labels: {} }
      : child;
    const view = render(
      <QueryClientProvider client={cache}>
        <SideChatSettings
          session={session}
          draft={draft ? { onChange, disabled: false } : undefined}
        />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Side chat model and effort" }));
    await user.click(screen.getByTestId("side-chat-model-edit"));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "GPT 6" }));
    expect(fetch).toHaveBeenCalledWith("/v1/hosts/host/harnesses/codex-native/model-options");
    if (draft) {
      expect(onChange).toHaveBeenCalledWith({ modelOverride: "gpt-6-sol", reasoningEffort: "low" });
      expect(sessionsApi.updateSession).not.toHaveBeenCalled();
    } else {
      expect(sessionsApi.updateSession).toHaveBeenCalledWith("side", {
        modelOverride: "gpt-6-sol",
        reasoningEffort: "low",
      });
    }
    expect(cache.getQueryData(["session", "parent"])).toEqual(source);
    view.rerender(
      <QueryClientProvider client={cache}>
        <SideChatSettings session={{ ...session, codexModelOptions: [models[0]] }} />
      </QueryClientProvider>,
    );
    expect(screen.queryByRole("menuitemcheckbox", { name: "GPT 6" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitemcheckbox", { name: "GPT 5.5" })).toBeInTheDocument();
  },
);
