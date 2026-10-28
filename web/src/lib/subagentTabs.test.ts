import { describe, expect, it } from "vitest";
import { subagentSessionIdFromTab, subagentTabId } from "./subagentTabs";

describe("subagentTabs", () => {
  it("round-trips a sub-agent session id through its tab id", () => {
    const tabId = subagentTabId("conv_child");
    expect(tabId).not.toBe("conv_child");
    expect(subagentSessionIdFromTab(tabId)).toBe("conv_child");
  });

  it("returns null for side-chat tabs", () => {
    expect(subagentSessionIdFromTab("conv_side")).toBeNull();
    expect(subagentSessionIdFromTab("pending:abc")).toBeNull();
  });

  it("returns null for a prefix with no session id", () => {
    expect(subagentSessionIdFromTab(subagentTabId(""))).toBeNull();
  });
});
