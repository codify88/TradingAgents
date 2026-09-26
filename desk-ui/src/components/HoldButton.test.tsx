import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HoldButton } from "./HoldButton";

describe("HoldButton", () => {
  let now = 0;
  beforeEach(() => {
    now = 0;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => setTimeout(() => cb(now), 16) as unknown as number);
    vi.stubGlobal("cancelAnimationFrame", (id: number) => clearTimeout(id));
    vi.useFakeTimers();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const advance = (ms: number) =>
    act(() => {
      now += ms;
      vi.advanceTimersByTime(ms);
    });

  it("confirms only after a full hold", () => {
    const onConfirm = vi.fn();
    render(<HoldButton label="Hold to approve" onConfirm={onConfirm} holdMs={900} />);
    const button = screen.getByRole("button", { name: /Hold to approve/ });
    fireEvent.keyDown(button, { key: "Enter" });
    advance(400);
    fireEvent.keyUp(button, { key: "Enter" }); // let go early
    advance(1000);
    expect(onConfirm).not.toHaveBeenCalled();
    fireEvent.keyDown(button, { key: " " });
    advance(1000);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("does nothing while disabled", () => {
    const onConfirm = vi.fn();
    render(<HoldButton label="Hold" onConfirm={onConfirm} disabled />);
    fireEvent.keyDown(screen.getByRole("button"), { key: "Enter" });
    advance(2000);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
