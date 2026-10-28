import { afterEach, describe, expect, it } from "vitest";
import {
  CHAT_TAB_ORDER_KEY,
  readChatTabOrder,
  subagentSessionIdFromTab,
  subagentTabId,
  writeChatTabOrder,
} from "./subagentTabs";

afterEach(() => localStorage.clear());

describe("subagentTabs", () => {
  it("round-trips a sub-agent session id through its tab id", () => {
    const tabId = subagentTabId("conv_child");
    expect(tabId).not.toBe("conv_child");
    expect(subagentSessionIdFromTab(tabId)).toBe("conv_child");
  });

  it.each([
    ["a side chat", "conv_side"],
    ["a pending side chat", "pending:abc"],
    ["a prefix with no session id", subagentTabId("")],
  ])("returns null for %s", (_name, tabId) => {
    expect(subagentSessionIdFromTab(tabId)).toBeNull();
  });
});

describe("chat tab order storage", () => {
  it("saves each parent's order separately and forgets it on null", () => {
    writeChatTabOrder("conv_a", ["conv_side", subagentTabId("conv_child")]);
    writeChatTabOrder("conv_b", [subagentTabId("conv_other")]);

    expect(readChatTabOrder("conv_a")).toEqual(["conv_side", subagentTabId("conv_child")]);
    writeChatTabOrder("conv_a", null);
    expect(readChatTabOrder("conv_a")).toEqual([]);
    expect(readChatTabOrder("conv_b")).toEqual([subagentTabId("conv_other")]);
  });

  it("keeps only the 100 most recently written parents", () => {
    for (let index = 0; index <= 100; index++) writeChatTabOrder(`conv_${index}`, ["conv_side"]);

    expect(readChatTabOrder("conv_0")).toEqual([]);
    expect(readChatTabOrder("conv_1")).toEqual(["conv_side"]);
    expect(readChatTabOrder("conv_100")).toEqual(["conv_side"]);
  });

  it("ignores malformed entries and duplicate or invalid tab ids", () => {
    localStorage.setItem(
      CHAT_TAB_ORDER_KEY,
      JSON.stringify([
        null,
        { id: 7, tabs: ["conv_x"] },
        { id: "conv_a", tabs: ["conv_side", "conv_side", "", 3] },
      ]),
    );
    expect(readChatTabOrder("conv_a")).toEqual(["conv_side"]);

    localStorage.setItem(CHAT_TAB_ORDER_KEY, "{not json");
    expect(readChatTabOrder("conv_a")).toEqual([]);
  });
});
