// Tests for PullRequestStrip — the multi-PR card row. Per-PR info is mocked;
// jsdom has no layout, so overflow is stubbed through the element's sizes.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PullRequest, PullRequestAssociation } from "@/hooks/usePullRequests";

const state = vi.hoisted(() => ({ details: {} as Record<string, PullRequest> }));

vi.mock("@/hooks/usePullRequests", () => ({
  usePullRequestInfos: vi.fn((_conversationId: string, urls: readonly string[]) =>
    urls.map((url) => ({ data: state.details[url] && { pr: state.details[url] } })),
  ),
}));

import { usePullRequestInfos } from "@/hooks/usePullRequests";
import { PullRequestStrip } from "./PullRequestStrip";

const ONE = "https://github.com/acme/app/pull/1";
const TWO = "https://github.com/acme/app/pull/2";

function association(url: string, number: number, title: string): PullRequestAssociation {
  return {
    url,
    host: "github.com",
    repository: "acme/app",
    number,
    title,
    relationship: "created",
  };
}

function details(
  url: string,
  over: Omit<Partial<PullRequest>, "checks"> & { checks?: Partial<PullRequest["checks"]> } = {},
): PullRequest {
  const { checks, ...rest } = over;
  return {
    number: 0,
    title: "",
    state: "OPEN",
    url,
    is_draft: false,
    author: null,
    base_ref: "main",
    head_ref: "topic",
    ...rest,
    checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [], ...checks },
  };
}

function renderStrip(onSelect = vi.fn()) {
  render(
    <PullRequestStrip
      conversationId="conv_1"
      prs={[association(ONE, 1, "Fix streaming"), association(TWO, 2, "Improve empty states")]}
      selectedUrl={ONE}
      onSelect={onSelect}
    />,
  );
  return within(screen.getByRole("group", { name: "Session pull requests" }));
}

beforeEach(() => {
  state.details = {};
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PullRequestStrip", () => {
  it("shows each PR's state glyph and non-zero check counts once its details load", async () => {
    state.details = {
      [ONE]: details(ONE, { checks: { failing: 1, passing: 100, total: 101 } }),
      [TWO]: details(TWO, { state: "CLOSED", checks: { pending: 36, passing: 42, total: 78 } }),
    };
    const strip = renderStrip();
    expect(usePullRequestInfos).toHaveBeenLastCalledWith("conv_1", [ONE, TWO]);
    const one = strip.getByRole("button", { name: "acme/app #1 — Fix streaming" });
    const two = strip.getByRole("button", { name: "acme/app #2 — Improve empty states" });
    expect(one).toHaveTextContent(/^#1·1100Fix streaming$/);
    expect(one.querySelector("svg")).toHaveClass("lucide-git-pull-request");
    expect(two).toHaveTextContent(/^#2·3642Improve empty states$/);
    expect(two.querySelector("svg")).toHaveClass("lucide-git-pull-request-closed");
    await userEvent.hover(two);
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("acme/app #2 — Improve empty states");
    expect(tooltip).toHaveTextContent("Closed · 36 pending · 42 passed");
  });

  it("shows a neutral glyph and no counts before a PR's details load", () => {
    const strip = renderStrip();
    const one = strip.getByRole("button", { name: "acme/app #1 — Fix streaming" });
    expect(one).toHaveTextContent(/^#1Fix streaming$/);
    expect(one.querySelector("svg")).toHaveClass(
      "lucide-git-pull-request",
      "text-muted-foreground",
    );
  });

  it("marks the selected card and selects another on click", async () => {
    const onSelect = vi.fn();
    const strip = renderStrip(onSelect);
    expect(strip.getByRole("button", { name: /#1/ })).toHaveAttribute("aria-current", "true");
    expect(strip.getByRole("button", { name: /#2/ })).not.toHaveAttribute("aria-current");
    await userEvent.click(strip.getByRole("button", { name: /#2/ }));
    expect(onSelect).toHaveBeenCalledWith(TWO);
  });

  it("offers chevrons only while the row overflows, paging it by most of its width", async () => {
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(200);
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(600);
    const setScrollLeft = vi.spyOn(Element.prototype, "scrollLeft", "set");
    renderStrip();
    expect(screen.getByRole("button", { name: "Scroll pull requests left" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Scroll pull requests right" }));
    expect(setScrollLeft).toHaveBeenLastCalledWith(160);
  });

  it("hides the chevrons when every card fits", () => {
    renderStrip();
    expect(screen.queryByRole("button", { name: /Scroll pull requests/ })).toBeNull();
  });
});
