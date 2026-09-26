import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "../cn";

export const HOLD_MS = 900;

/** Press and hold to confirm: deliberate without a second dialog.
 *  Works with a finger, a mouse, or a held Space/Enter key; letting go early cancels. */
export function HoldButton({
  label,
  busyLabel = "Working…",
  onConfirm,
  disabled,
  busy,
  holdMs = HOLD_MS,
}: {
  label: string;
  busyLabel?: string;
  onConfirm: () => void;
  disabled?: boolean;
  busy?: boolean;
  holdMs?: number;
}) {
  const [progress, setProgress] = useState(0);
  const start = useRef<number | null>(null);
  const frame = useRef<number | null>(null);

  const stop = useCallback(() => {
    start.current = null;
    if (frame.current) cancelAnimationFrame(frame.current);
    frame.current = null;
    setProgress(0);
  }, []);

  const tick = useCallback(() => {
    if (start.current === null) return;
    const p = Math.min(1, (performance.now() - start.current) / holdMs);
    setProgress(p);
    if (p >= 1) {
      stop();
      onConfirm();
      return;
    }
    frame.current = requestAnimationFrame(tick);
  }, [holdMs, onConfirm, stop]);

  const begin = useCallback(() => {
    if (disabled || busy || start.current !== null) return;
    start.current = performance.now();
    frame.current = requestAnimationFrame(tick);
  }, [busy, disabled, tick]);

  useEffect(() => stop, [stop]);

  return (
    <button
      type="button"
      disabled={disabled || busy}
      aria-label={`${label} (press and hold)`}
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture(e.pointerId);
        begin();
      }}
      onPointerUp={stop}
      onPointerCancel={stop}
      onKeyDown={(e) => {
        if ((e.key === " " || e.key === "Enter") && !e.repeat) {
          e.preventDefault();
          begin();
        }
      }}
      onKeyUp={(e) => {
        if (e.key === " " || e.key === "Enter") stop();
      }}
      onContextMenu={(e) => e.preventDefault()}
      className={cn(
        "relative flex h-12 w-full select-none items-center justify-center overflow-hidden rounded-xl bg-accent text-[15px] font-semibold text-accent-ink [touch-action:none] disabled:cursor-not-allowed disabled:opacity-45",
      )}
    >
      <span
        aria-hidden
        className="absolute inset-y-0 left-0 bg-white/25"
        style={{ width: `${progress * 100}%` }}
      />
      <span className="relative">{busy ? busyLabel : label}</span>
    </button>
  );
}
