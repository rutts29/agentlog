import { useEffect, useRef, useState } from "react";

export function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * 250ms ease-out count-up, run once per value change (range switches).
 * First render and reduced-motion render the final value immediately.
 */
export function useCountUp(target: number, durationMs = 250): number {
  const [shown, setShown] = useState(target);
  const first = useRef(true);
  const raf = useRef<number>();

  useEffect(() => {
    if (first.current || prefersReducedMotion()) {
      first.current = false;
      setShown(target);
      return;
    }
    const from = shown;
    if (from === target) return;
    const t0 = performance.now();
    const step = (now: number) => {
      const t = Math.min(1, (now - t0) / durationMs);
      const eased = 1 - (1 - t) * (1 - t);
      setShown(Math.round(from + (target - from) * eased));
      if (t < 1) raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
    return () => {
      if (raf.current) cancelAnimationFrame(raf.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, durationMs]);

  return shown;
}
