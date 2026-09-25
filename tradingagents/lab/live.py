"""The lab's signals on today's screen: the same code on a small panel.

The live screen hands over the price tier's frames; they are stacked into a
panel exactly like the lab's and the signal is read at the last date on or
before the screen date, so a tuned ordering cannot drift from the one that runs.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .panel import Panel
from .replay import Context, _signal


def panel_from_frames(frames: dict[str, pd.DataFrame], as_of: str) -> Panel:
    cut = pd.Timestamp(as_of)

    def field(col: str, fallback: str | None = None) -> pd.DataFrame:
        cols = {}
        for s, f in frames.items():
            src = col if col in f else fallback
            if src is not None and src in f:
                cols[s] = f[src][~f.index.duplicated(keep="last")]
        frame = pd.DataFrame(cols).sort_index()
        return frame[frame.index <= cut].astype(np.float32)

    return Panel(field("Open"), field("Close"), field("Raw Close", "Close"), field("Volume"))


def rank(signal: str, frames: dict[str, pd.DataFrame], as_of: str,
         seed: int = 7) -> tuple[dict[str, float], list[str]]:
    """``({symbol: score}, [symbols the signal cannot place])``; higher ranks first."""
    if not frames:
        return {}, []
    p = panel_from_frames(frames, as_of)
    if p.close.empty:
        return {}, list(frames)
    ctx = Context(p, {})
    i = len(p.dates) - 1
    scores = _signal(ctx, signal, i, seed)
    placed, unplaced = {}, []
    for s, v in zip(p.symbols, scores, strict=True):
        if v is None or math.isnan(float(v)):
            unplaced.append(s)
        else:
            placed[s] = float(v)
    return placed, unplaced
