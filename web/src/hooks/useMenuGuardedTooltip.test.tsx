// The guard's contract: a tooltip sharing its trigger with a menu stays closed
// while the menu is open, swallows the open request that the closing menu's
// focus handoff produces, and reopens on fresh hover intent or once the guard
// window has elapsed.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MENU_CLOSE_TOOLTIP_GUARD_MS, useMenuGuardedTooltip } from "./useMenuGuardedTooltip";

function renderGuard(initialMenuOpen = false) {
  return renderHook(({ menuOpen }: { menuOpen: boolean }) => useMenuGuardedTooltip(menuOpen), {
    initialProps: { menuOpen: initialMenuOpen },
  });
}

describe("useMenuGuardedTooltip", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("follows open requests while no menu is open", () => {
    const { result } = renderGuard();
    act(() => result.current.onOpenChange(true));
    expect(result.current.open).toBe(true);
    act(() => result.current.onOpenChange(false));
    expect(result.current.open).toBe(false);
  });

  it("closes the tooltip and ignores open requests while the menu is open", () => {
    const { result, rerender } = renderGuard();
    act(() => result.current.onOpenChange(true));
    expect(result.current.open).toBe(true);

    rerender({ menuOpen: true });
    expect(result.current.open).toBe(false);
    act(() => result.current.onOpenChange(true));
    expect(result.current.open).toBe(false);

    // Pointer entry while the menu is open is not fresh hover intent.
    act(() => result.current.triggerProps.onPointerEnter());
    act(() => result.current.onOpenChange(true));
    expect(result.current.open).toBe(false);
  });

  it("swallows open requests after the menu closes until the guard window elapses", () => {
    const { result, rerender } = renderGuard(true);
    rerender({ menuOpen: false });

    act(() => result.current.onOpenChange(true));
    expect(result.current.open).toBe(false);

    act(() => vi.advanceTimersByTime(MENU_CLOSE_TOOLTIP_GUARD_MS - 1));
    act(() => result.current.onOpenChange(true));
    expect(result.current.open).toBe(false);

    act(() => vi.advanceTimersByTime(1));
    act(() => result.current.onOpenChange(true));
    expect(result.current.open).toBe(true);
  });

  it("lets the pointer re-entering the trigger reopen the tooltip right after close", () => {
    const { result, rerender } = renderGuard(true);
    rerender({ menuOpen: false });

    act(() => result.current.triggerProps.onPointerEnter());
    act(() => result.current.onOpenChange(true));
    expect(result.current.open).toBe(true);
  });
});
