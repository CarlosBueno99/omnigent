import { useEffect, useRef, useState } from "react";

/** How long open requests stay ignored after the menu closes, past its focus handoff. */
export const MENU_CLOSE_TOOLTIP_GUARD_MS = 600;

/**
 * Controlled-tooltip state for a trigger that also opens a menu. Radix opens a
 * tooltip on any focus, including the programmatic focus a closing menu hands
 * back to its trigger. The tooltip stays closed while the menu is open and
 * ignores open requests briefly after it closes, unless the pointer re-enters
 * the trigger. Spread `triggerProps` onto the element wrapped by TooltipTrigger.
 */
export function useMenuGuardedTooltip(menuOpen: boolean) {
  const [wantsOpen, setWantsOpen] = useState(false);
  // Epoch millis until which open requests are ignored; Infinity while the
  // menu is open. A ref, not state: it is only read when Radix requests an
  // open, so changing it never needs a re-render.
  const suppressedUntil = useRef(0);
  useEffect(() => {
    if (menuOpen) {
      setWantsOpen(false);
      suppressedUntil.current = Number.POSITIVE_INFINITY;
    } else if (suppressedUntil.current === Number.POSITIVE_INFINITY) {
      suppressedUntil.current = Date.now() + MENU_CLOSE_TOOLTIP_GUARD_MS;
    }
  }, [menuOpen]);
  return {
    open: wantsOpen && !menuOpen,
    onOpenChange: (next: boolean) =>
      setWantsOpen(next && !menuOpen && Date.now() >= suppressedUntil.current),
    triggerProps: {
      onPointerEnter: () => {
        if (!menuOpen) suppressedUntil.current = 0;
      },
    },
  } as const;
}
