import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import type { PullRequest } from "@/hooks/usePullRequests";
import { PullRequestActions } from "./PullRequestActions";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/host", () => ({ getOmnigentServerIdentity: () => "test-server" }));
const state = vi.hoisted(() => ({ permissionLevel: 4 }));
vi.mock("@/hooks/useSession", () => ({ useSession: () => ({ session: state }) }));
vi.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({ children }: { children: string }) => <p>{children}</p>,
}));
const pr = {
  url: "https://github.com/acme/app/pull/1",
  title: "Original",
  body: "Description",
} as PullRequest;
const access = { actor: "octocat", can_edit: true, can_request_reviewers: true };
const requests: Record<string, string>[] = [];
let fail = false;

beforeEach(() => {
  localStorage.clear();
  state.permissionLevel = 4;
  requests.length = 0;
  fail = false;
  vi.mocked(authenticatedFetch)
    .mockReset()
    .mockImplementation(async (_url, init) => {
      if (!init?.method) return Response.json(access);
      requests.push(JSON.parse(String(init.body)));
      return fail
        ? Response.json({ detail: "Refresh before retrying" }, { status: 400 })
        : Response.json({ updated: true });
    });
});

function mount(value = pr, viewer = "owner") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const element = (next: PullRequest) => (
    <QueryClientProvider client={client}>
      <PullRequestActions conversationId="session" pr={next} viewer={viewer} />
    </QueryClientProvider>
  );
  return { ...render(element(value)), element, client };
}

it("recovers drafts after navigation, keeps polling separate, and sends only the saved field", async () => {
  const first = mount();
  await screen.findByText("Acting as @octocat (host GitHub account)");
  fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
  fireEvent.change(screen.getByRole("textbox", { name: "PR description" }), {
    target: { value: "Unsaved draft" },
  });
  first.rerender(first.element({ ...pr, body: "Updated on GitHub" }));
  expect(screen.getByRole("textbox", { name: "PR description" })).toHaveValue("Unsaved draft");
  first.unmount();
  const second = mount();
  expect(screen.getByRole("textbox", { name: "PR description" })).toHaveValue("Unsaved draft");
  await screen.findByText("Acting as @octocat (host GitHub account)");
  fireEvent.click(screen.getByRole("button", { name: "Preview" }));
  expect(screen.getByText("Unsaved draft")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Write" }));
  fail = true;
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Refresh before retrying");
  expect(screen.getByRole("textbox", { name: "PR description" })).toHaveValue("Unsaved draft");
  fail = false;
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("PR updated.");
  expect(requests).toEqual(
    Array(2).fill({ pr_url: pr.url, expected_actor: "octocat", body: "Unsaved draft" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
  fireEvent.change(screen.getByRole("textbox", { name: "PR title" }), {
    target: { value: "Renamed" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("PR updated.");
  expect(requests.at(-1)).toEqual({ pr_url: pr.url, expected_actor: "octocat", title: "Renamed" });
  second.unmount();
  mount();
  expect(screen.queryByRole("textbox", { name: "PR title" })).toBeNull();
  expect(screen.queryByRole("textbox", { name: "PR description" })).toBeNull();
});

it("keeps another user's drafts separate, cancels edits, and refreshes reviewers after success", async () => {
  const first = mount();
  await screen.findByText("Acting as @octocat (host GitHub account)");
  fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
  fireEvent.change(screen.getByRole("textbox", { name: "PR title" }), {
    target: { value: "Private draft" },
  });
  first.unmount();
  const second = mount(pr, "other");
  await screen.findByText("Acting as @octocat (host GitHub account)");
  expect(screen.queryByRole("textbox", { name: "PR title" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
  fireEvent.change(screen.getByRole("textbox", { name: "PR title" }), {
    target: { value: "Discard this" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
  expect(screen.getByRole("textbox", { name: "PR title" })).toHaveValue("Original");
  const invalidate = vi.spyOn(second.client, "invalidateQueries");
  fireEvent.change(screen.getByRole("textbox", { name: "Reviewer username" }), {
    target: { value: "reviewer" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Request reviewer" }));
  await screen.findByText("Reviewer requested.");
  expect(requests).toEqual([{ pr_url: pr.url, expected_actor: "octocat", reviewer: "reviewer" }]);
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["github-info", "session"] });
});

it("does not resolve host credentials for collaborators and disables provider-denied actions", async () => {
  state.permissionLevel = 2;
  const first = mount();
  expect(screen.queryByRole("button", { name: "Edit title" })).toBeNull();
  expect(authenticatedFetch).not.toHaveBeenCalled();
  first.unmount();
  state.permissionLevel = 4;
  vi.mocked(authenticatedFetch).mockResolvedValue(
    Response.json({ ...access, can_edit: false, can_request_reviewers: false }),
  );
  mount();
  await waitFor(() => expect(screen.getByRole("button", { name: "Edit title" })).toBeDisabled());
  expect(screen.getByRole("button", { name: "Request reviewer" })).toBeDisabled();
});
