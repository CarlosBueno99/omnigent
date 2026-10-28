import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { writeOpenSubagentsInPanel } from "@/lib/subagentPanelPreferences";
import { SubagentPanelOpenerContext, useOpenSubagentInPanel } from "./subagentPanelOpener";

const isMobileMock = vi.fn(() => false);
vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => isMobileMock(),
}));

const PLAIN_CLICK = { button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false };

function renderWithOpener(opener: ((id: string) => boolean) | null) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <SubagentPanelOpenerContext.Provider value={opener}>
      {children}
    </SubagentPanelOpenerContext.Provider>
  );
  return renderHook(() => useOpenSubagentInPanel(), { wrapper }).result.current;
}

afterEach(() => {
  localStorage.clear();
  isMobileMock.mockReturnValue(false);
});

describe("useOpenSubagentInPanel", () => {
  it("opens a plain click in the panel when the setting is on", () => {
    writeOpenSubagentsInPanel(true);
    const opener = vi.fn(() => true);
    const openInPanel = renderWithOpener(opener);

    expect(openInPanel("conv_child", PLAIN_CLICK)).toBe(true);
    expect(opener).toHaveBeenCalledWith("conv_child");
  });

  it("returns the opener's refusal", () => {
    writeOpenSubagentsInPanel(true);
    const openInPanel = renderWithOpener(() => false);

    expect(openInPanel("conv_child", PLAIN_CLICK)).toBe(false);
  });

  it("keeps navigation when the setting is off", () => {
    const opener = vi.fn(() => true);
    const openInPanel = renderWithOpener(opener);

    expect(openInPanel("conv_child", PLAIN_CLICK)).toBe(false);
    expect(opener).not.toHaveBeenCalled();
  });

  it.each([
    ["middle", { button: 1 }],
    ["meta", { metaKey: true }],
    ["ctrl", { ctrlKey: true }],
    ["shift", { shiftKey: true }],
    ["alt", { altKey: true }],
  ])("keeps navigation for a %s click", (_name, modifiers) => {
    writeOpenSubagentsInPanel(true);
    const opener = vi.fn(() => true);
    const openInPanel = renderWithOpener(opener);

    expect(openInPanel("conv_child", { ...PLAIN_CLICK, ...modifiers })).toBe(false);
    expect(opener).not.toHaveBeenCalled();
  });

  it("keeps navigation on a phone viewport", () => {
    writeOpenSubagentsInPanel(true);
    isMobileMock.mockReturnValue(true);
    const opener = vi.fn(() => true);
    const openInPanel = renderWithOpener(opener);

    expect(openInPanel("conv_child", PLAIN_CLICK)).toBe(false);
    expect(opener).not.toHaveBeenCalled();
  });

  it("keeps navigation outside a surface that provides an opener", () => {
    writeOpenSubagentsInPanel(true);
    const openInPanel = renderWithOpener(null);

    expect(openInPanel("conv_child", PLAIN_CLICK)).toBe(false);
  });
});
