import { describe, expect, it } from "vitest";
import { browserTabLabel, browserUrlLabel, NEW_BROWSER_TAB_LABEL } from "./browserTabLabel";

describe("browserUrlLabel", () => {
  it.each([
    ["https://github.com/", "github.com"],
    ["https://www.example.com/docs", "example.com/docs"],
    ["http://localhost:3000/settings?tab=1#top", "localhost:3000/settings"],
    ["https://github.com/omnigent-ai/omnigent", "github.com/omnigent-ai/omnigent"],
    ["https://github.com/omnigent-ai/omnigent/pull/123", "github.com/…/pull/123"],
    ["https://example.com/a%20b/c/d%20e", "example.com/…/c/d e"],
    ["https://example.com/bad%E0%A4%A/x/y", "example.com/…/x/y"],
    ["file:///Users/me/report.html", "report.html"],
    ["data:text/plain,hello", "data:text/plain,hello"],
    ["not a url", "not a url"],
    ["", NEW_BROWSER_TAB_LABEL],
    ["   ", NEW_BROWSER_TAB_LABEL],
    ["about:blank", NEW_BROWSER_TAB_LABEL],
    [null, NEW_BROWSER_TAB_LABEL],
    [undefined, NEW_BROWSER_TAB_LABEL],
  ])("labels %j as %j", (url, label) => {
    expect(browserUrlLabel(url)).toBe(label);
  });
});

describe("browserTabLabel", () => {
  it("prefers a manual name and falls back to the URL when it is blank", () => {
    expect(browserTabLabel(" Dev server ", "http://localhost:3000/")).toBe("Dev server");
    expect(browserTabLabel("  ", "http://localhost:3000/")).toBe("localhost:3000");
    expect(browserTabLabel(null, null)).toBe(NEW_BROWSER_TAB_LABEL);
  });
});
