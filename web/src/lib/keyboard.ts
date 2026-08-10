import { useEffect, useRef } from "react";

export function isEditable(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return (
    tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable
  );
}

/**
 * View-local keyboard shortcuts.
 *
 * Registered in the capture phase so the focused view sees a key before the
 * shell's global bindings, and consuming one halts the event outright —
 * `preventDefault` alone does not stop a sibling window listener, which is how
 * `]` used to advance an adjudication pair *and* cycle the global time range.
 *
 * Return true from `handler` to consume the key; anything else falls through
 * to the shell. Modifier chords are never consumed, so Cmd+K stays global.
 */
export function useViewShortcuts(handler: (e: KeyboardEvent) => boolean): void {
  const latest = useRef(handler);
  useEffect(() => {
    latest.current = handler;
  });
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (isEditable(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (!latest.current(e)) return;
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);
}
