"""Append-only markdown decision log for TradingAgents."""

import re
from pathlib import Path

from tradingagents.agents.utils.rating import parse_rating


class TradingMemoryLog:
    """Append-only markdown log of trading decisions and reflections."""

    # HTML comment: cannot appear in LLM prose output, safe as a hard delimiter
    _SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"
    # Precompiled patterns — avoids re-compilation on every load_entries() call
    _DECISION_RE = re.compile(
        r"DECISION:\n(.*?)(?=\n\nREVIEW \d+d @ |\nREFLECTION:|\Z)", re.DOTALL
    )
    _REFLECTION_RE = re.compile(r"REFLECTION:\n(.*?)$", re.DOTALL)
    # Interim checkpoints on a still-pending entry. A long-horizon decision is
    # graded several times before it settles, so one entry holds several
    # outcomes: N REVIEW blocks, then at most one REFLECTION.
    _REVIEW_RE = re.compile(
        r"^REVIEW (\d+)d @ (\d{4}-\d{2}-\d{2}): raw (\S+) \| alpha (\S+)\n"
        r"(.*?)(?=\n\nREVIEW \d+d @ |\n\nREFLECTION:|\Z)",
        re.DOTALL | re.MULTILINE,
    )

    def __init__(self, config: dict = None):
        cfg = config or {}
        self._log_path = None
        path = cfg.get("memory_log_path")
        if path:
            self._log_path = Path(path).expanduser()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # Optional cap on resolved entries. None disables rotation.
        self._max_entries = cfg.get("memory_log_max_entries")

    # --- Write path (Phase A) ---

    def store_decision(
        self,
        ticker: str,
        trade_date: str,
        final_trade_decision: str,
        mandate: str = "",
    ) -> None:
        """Append pending entry at end of propagate(). No LLM call.

        ``mandate`` is recorded on the tag so the deferred outcome resolution
        grades this decision on the horizon it was actually made for, even if
        the next run of the same ticker uses a different mandate.
        """
        if not self._log_path:
            return
        # Idempotency guard: fast raw-text scan instead of full parse. Any entry
        # for this ticker and date blocks another, pending or settled: a re-run
        # after the outcome landed would otherwise count the same decision twice
        # in past context and in every aggregate over the log.
        if self._log_path.exists():
            raw = self._log_path.read_text(encoding="utf-8")
            prefix = f"[{trade_date} | {ticker} |"
            for line in raw.splitlines():
                # Upstream blocks on any entry for this ticker and date, settled
                # or pending, so a re-run after the outcome landed cannot count
                # the same decision twice (#645). Scoped to the mandate here:
                # the same ticker and date under a *different* mandate is a
                # genuinely different decision -- different horizon, different
                # framing -- and gets its own entry.
                if self._tag_mandate(line.strip(), prefix) == mandate:
                    return
        rating = parse_rating(final_trade_decision)
        tag = f"[{trade_date} | {ticker} | {rating} | pending"
        if mandate:
            tag += f" | mandate:{mandate}"
        tag += "]"
        entry = f"{tag}\n\nDECISION:\n{final_trade_decision}{self._SEPARATOR}"
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(entry)

    # --- Read path (Phase A) ---

    def load_entries(self) -> list[dict]:
        """Parse all entries from log. Returns list of dicts."""
        if not self._log_path or not self._log_path.exists():
            return []
        text = self._log_path.read_text(encoding="utf-8")
        raw_entries = [e.strip() for e in text.split(self._SEPARATOR) if e.strip()]
        entries = []
        for raw in raw_entries:
            parsed = self._parse_entry(raw)
            if parsed:
                entries.append(parsed)
        return entries

    def get_pending_entries(self) -> list[dict]:
        """Return entries with outcome:pending (for Phase B)."""
        return [e for e in self.load_entries() if e.get("pending")]

    def get_past_context(
        self, ticker: str, n_same: int = 5, n_cross: int = 3, as_of: str | None = None
    ) -> str:
        """Return formatted past context string for agent prompt injection.

        When ``as_of`` (yyyy-mm-dd) is given, only lessons whose outcome was
        already known by that date are included — an entry is kept only if it
        stores a resolution date (``resolved:...``) that is on or before
        ``as_of``. This keeps a historical/backtest run from learning from
        outcomes that had not happened yet (#1251). ``as_of=None`` disables the
        filter, so live runs and pre-migration entries are unaffected.

        A *pending* entry is included once it carries at least one interim
        review that has come due. Without this a long-horizon mandate teaches
        nothing until its thesis settles -- two years, for equity_value -- which
        is precisely what review horizons exist to prevent. Only reviews whose
        own resolution date has passed are shown, so the point-in-time guarantee
        holds for checkpoints exactly as it does for final outcomes.
        """
        entries = []
        for e in self.load_entries():
            if not e.get("pending"):
                if as_of is not None and not (
                    e.get("resolved") and e["resolved"] <= as_of
                ):
                    continue
                entries.append(e)
                continue
            visible = [
                r for r in e.get("reviews", ())
                if as_of is None or r["resolved"] <= as_of
            ]
            if visible:
                entries.append({**e, "reviews": visible})
        if not entries:
            return ""

        same, cross = [], []
        for e in reversed(entries):
            if len(same) >= n_same and len(cross) >= n_cross:
                break
            if e["ticker"] == ticker and len(same) < n_same:
                same.append(e)
            elif e["ticker"] != ticker and len(cross) < n_cross:
                cross.append(e)

        if not same and not cross:
            return ""

        parts = []
        if same:
            parts.append(f"Past analyses of {ticker} (most recent first):")
            parts.extend(self._format_full(e) for e in same)
        if cross:
            parts.append("Recent cross-ticker lessons:")
            parts.extend(self._format_reflection_only(e) for e in cross)
        return "\n\n".join(parts)

    @staticmethod
    def _tag_mandate(tag_line: str, prefix: str):
        """The mandate on any entry tag matching ``prefix``, else ``None``.

        Unlike :meth:`_match_pending_tag` this accepts settled entries too, for
        the write-path idempotency guard. ``None`` means "not an entry for this
        ticker and date", which no mandate string can collide with.
        """
        if not (tag_line.startswith(prefix) and tag_line.endswith("]")):
            return None
        fields = [f.strip() for f in tag_line[1:-1].split("|")]
        if len(fields) < 4:
            return None
        return next(
            (f.split(":", 1)[1].strip() for f in fields[4:] if f.startswith("mandate:")),
            "",
        )

    @staticmethod
    def _match_pending_tag(tag_line: str, prefix: str) -> tuple[str, str] | None:
        """Return ``(rating, mandate)`` for a matching pending tag, else None.

        ``pending`` sits at field index 3 but is no longer necessarily the last
        field -- a ``mandate:`` marker may follow it -- so this matches on the
        parsed fields rather than on the line's suffix. The mandate is returned
        so it survives onto the resolved tag.
        """
        if not (tag_line.startswith(prefix) and tag_line.endswith("]")):
            return None
        fields = [f.strip() for f in tag_line[1:-1].split("|")]
        if len(fields) < 4 or fields[3] != "pending":
            return None
        mandate = next(
            (f.split(":", 1)[1].strip() for f in fields[4:] if f.startswith("mandate:")),
            "",
        )
        return fields[2], mandate

    # --- Update path (Phase B) ---

    def update_with_outcome(
        self,
        ticker: str,
        trade_date: str,
        raw_return: float,
        alpha_return: float,
        holding_days: int,
        reflection: str,
        resolution_date: str | None = None,
        mandate: str = "",
    ) -> None:
        """Replace pending tag and append REFLECTION section using atomic write.

        Finds the first pending entry matching (trade_date, ticker, mandate),
        updates
        its tag with return figures (and the ``resolution_date`` the outcome
        became known), and appends a REFLECTION section.  Uses a temp-file +
        os.replace() so a crash mid-write never corrupts the log.
        """
        if not self._log_path or not self._log_path.exists():
            return

        text = self._log_path.read_text(encoding="utf-8")
        blocks = text.split(self._SEPARATOR)

        pending_prefix = f"[{trade_date} | {ticker} |"
        raw_pct = f"{raw_return:+.1%}"
        alpha_pct = f"{alpha_return:+.1%}"

        updated = False
        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            lines = stripped.splitlines()
            tag_line = lines[0].strip()

            match = None if updated else self._match_pending_tag(tag_line, pending_prefix)
            if match is not None and match[1] == mandate:
                rating, _ = match
                new_tag = self._resolved_tag(
                    trade_date, ticker, rating, raw_pct, alpha_pct, holding_days,
                    resolution_date, mandate,
                )
                rest = "\n".join(lines[1:])
                new_blocks.append(
                    f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{reflection}"
                )
                updated = True
            else:
                new_blocks.append(block)

        if not updated:
            return

        new_blocks = self._apply_rotation(new_blocks)
        new_text = self._SEPARATOR.join(new_blocks)
        tmp_path = self._log_path.with_suffix(".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)

    def batch_append_reviews(self, reviews: list[dict]) -> None:
        """Record interim outcomes on entries that are still pending.

        A review is a checkpoint, not a verdict: the entry keeps its ``pending``
        tag and will be settled later at its mandate's primary horizon. This is
        what stops a two-year value thesis from producing no learning signal for
        two years -- each review is injectable into later runs the moment its
        own resolution date has passed.

        Each element needs: ticker, trade_date, mandate, horizon_days,
        raw_return, alpha_return, resolution_date, note.
        """
        if not self._log_path or not self._log_path.exists() or not reviews:
            return

        # Several horizons can come due for one entry in a single run (a long
        # gap between runs), so group them and append in horizon order.
        grouped: dict[tuple[str, str, str], list[dict]] = {}
        for r in reviews:
            key = (r["trade_date"], r["ticker"], r.get("mandate", ""))
            grouped.setdefault(key, []).append(r)
        for group in grouped.values():
            group.sort(key=lambda r: r["horizon_days"])

        text = self._log_path.read_text(encoding="utf-8")
        blocks = text.split(self._SEPARATOR)

        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            tag_line = stripped.splitlines()[0].strip()
            appended = False
            for key, group in list(grouped.items()):
                trade_date, ticker, mandate = key
                match = self._match_pending_tag(tag_line, f"[{trade_date} | {ticker} |")
                if match is None or match[1] != mandate:
                    continue
                body = stripped
                for r in group:
                    # Idempotency: never write the same horizon twice, even if a
                    # caller re-offers one that is already on the entry.
                    if f"REVIEW {r['horizon_days']}d @ " in body:
                        continue
                    body += "\n\n" + self._render_review(r)
                new_blocks.append(body)
                del grouped[key]
                appended = True
                break

            if not appended:
                new_blocks.append(block)

        # No rotation pass: a review resolves nothing, so the resolved-entry
        # count that rotation caps is unchanged.
        new_text = self._SEPARATOR.join(new_blocks)
        tmp_path = self._log_path.with_suffix(".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)

    def batch_update_with_outcomes(self, updates: list[dict]) -> None:
        """Apply multiple outcome updates in a single read + atomic write.

        Each element of updates must have keys: ticker, trade_date,
        raw_return, alpha_return, holding_days, reflection.
        """
        if not self._log_path or not self._log_path.exists() or not updates:
            return

        text = self._log_path.read_text(encoding="utf-8")
        blocks = text.split(self._SEPARATOR)

        # Keyed by (trade_date, ticker, mandate): the same ticker and date can
        # carry one pending entry per mandate, each with its own horizon.
        update_map = {
            (u["trade_date"], u["ticker"], u.get("mandate", "")): u for u in updates
        }

        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            lines = stripped.splitlines()
            tag_line = lines[0].strip()

            matched = False
            for (trade_date, ticker, mandate), upd in list(update_map.items()):
                pending_prefix = f"[{trade_date} | {ticker} |"
                match = self._match_pending_tag(tag_line, pending_prefix)
                if match is not None and match[1] == mandate:
                    rating, _ = match
                    raw_pct = f"{upd['raw_return']:+.1%}"
                    alpha_pct = f"{upd['alpha_return']:+.1%}"
                    new_tag = self._resolved_tag(
                        trade_date, ticker, rating, raw_pct, alpha_pct,
                        upd["holding_days"], upd.get("resolution_date"), mandate,
                    )
                    rest = "\n".join(lines[1:])
                    new_blocks.append(
                        f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{upd['reflection']}"
                    )
                    del update_map[(trade_date, ticker, mandate)]
                    matched = True
                    break

            if not matched:
                new_blocks.append(block)

        new_blocks = self._apply_rotation(new_blocks)
        new_text = self._SEPARATOR.join(new_blocks)
        tmp_path = self._log_path.with_suffix(".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)

    # --- Helpers ---

    @staticmethod
    def _is_resolved_tag(tag_line: str) -> bool:
        """Whether a tag line belongs to a settled entry.

        Parses the fields rather than suffix-matching ``| pending]``: a pending
        tag can carry trailing markers (``mandate:``), so a suffix test would
        misread a mandated pending entry as resolved and let rotation prune
        unfinished work.
        """
        if not (tag_line.startswith("[") and tag_line.endswith("]")):
            return False
        fields = [f.strip() for f in tag_line[1:-1].split("|")]
        return len(fields) >= 4 and fields[3] != "pending"

    @staticmethod
    def _render_review(r: dict) -> str:
        """One REVIEW block: the checkpoint line plus its interim note."""
        return (
            f"REVIEW {r['horizon_days']}d @ {r['resolution_date']}: "
            f"raw {r['raw_return']:+.1%} | alpha {r['alpha_return']:+.1%}\n"
            f"{r['note'].strip()}"
        )

    @staticmethod
    def _resolved_tag(
        trade_date, ticker, rating, raw_pct, alpha_pct, holding_days,
        resolution_date, mandate="",
    ) -> str:
        """Build a resolved entry tag, recording the outcome's known-by date.

        ``resolution_date`` (the date of the last price bar used for the return)
        is the point-in-time cutoff a later run filters on (#1251). Omitted when
        unavailable, keeping the legacy 6-field tag.
        """
        tag = f"[{trade_date} | {ticker} | {rating} | {raw_pct} | {alpha_pct} | {holding_days}d"
        if resolution_date:
            tag += f" | resolved:{resolution_date}"
        if mandate:
            tag += f" | mandate:{mandate}"
        return tag + "]"

    def _apply_rotation(self, blocks: list[str]) -> list[str]:
        """Drop oldest resolved blocks when their count exceeds max_entries.

        Pending blocks are always kept (they represent unprocessed work).
        Returns ``blocks`` unchanged when rotation is disabled or under cap.
        """
        if not self._max_entries or self._max_entries <= 0:
            return blocks

        # Tag each block with (kept, is_resolved) by parsing tag-line markers.
        decisions = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                decisions.append((block, False))
                continue
            tag_line = stripped.splitlines()[0].strip()
            decisions.append((block, self._is_resolved_tag(tag_line)))

        resolved_count = sum(1 for _, r in decisions if r)
        if resolved_count <= self._max_entries:
            return blocks

        to_drop = resolved_count - self._max_entries
        kept: list[str] = []
        for block, is_resolved in decisions:
            if is_resolved and to_drop > 0:
                to_drop -= 1
                continue
            kept.append(block)
        return kept

    def _parse_entry(self, raw: str) -> dict | None:
        lines = raw.strip().splitlines()
        if not lines:
            return None
        tag_line = lines[0].strip()
        if not (tag_line.startswith("[") and tag_line.endswith("]")):
            return None
        fields = [f.strip() for f in tag_line[1:-1].split("|")]
        if len(fields) < 4:
            return None
        # Fields past the rating split into positional values (alpha, holding)
        # and "key:value" markers. "resolved:" records when the outcome became
        # known, for point-in-time filtering (#1251); "mandate:" records which
        # investment style the call was made under, so the deferred resolution
        # grades it on that mandate's horizon. Both are optional, so entries
        # written by earlier versions keep parsing unchanged.
        tagged, positional = {}, []
        for f in fields[4:]:
            key = f.split(":", 1)[0] if ":" in f else None
            if key in ("resolved", "mandate"):
                tagged[key] = f.split(":", 1)[1].strip()
            else:
                positional.append(f)
        entry = {
            "date": fields[0],
            "ticker": fields[1],
            "rating": fields[2],
            "pending": fields[3] == "pending",
            "raw": fields[3] if fields[3] != "pending" else None,
            "alpha": positional[0] if positional else None,
            "holding": positional[1] if len(positional) > 1 else None,
            "resolved": tagged.get("resolved"),
            "mandate": tagged.get("mandate", ""),
        }
        body = "\n".join(lines[1:]).strip()
        decision_match = self._DECISION_RE.search(body)
        reflection_match = self._REFLECTION_RE.search(body)
        entry["decision"] = decision_match.group(1).strip() if decision_match else ""
        entry["reflection"] = reflection_match.group(1).strip() if reflection_match else ""
        # Interim outcomes, oldest horizon first. Empty for entries written
        # before review horizons existed, and for short-horizon mandates that
        # declare none.
        entry["reviews"] = [
            {
                "days": int(m.group(1)),
                "resolved": m.group(2),
                "raw": m.group(3),
                "alpha": m.group(4),
                "note": m.group(5).strip(),
            }
            for m in self._REVIEW_RE.finditer(body)
        ]
        return entry

    def _format_full(self, e: dict) -> str:
        reviews = e.get("reviews", ())
        if e.get("pending"):
            # Label it unmistakably: a future analyst must not read a checkpoint
            # as a settled verdict and conclude the thesis already worked. The
            # mandate rides along because it is what makes the elapsed days
            # interpretable -- 63 days is early for one style and late for another.
            mandate = f" | {e['mandate']}" if e.get("mandate") else ""
            tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | in progress{mandate}]"
        else:
            raw = e["raw"] or "n/a"
            alpha = e["alpha"] or "n/a"
            holding = e["holding"] or "n/a"
            tag = (
                f"[{e['date']} | {e['ticker']} | {e['rating']} | "
                f"{raw} | {alpha} | {holding}]"
            )
        parts = [tag, f"DECISION:\n{e['decision']}"]
        for r in reviews:
            parts.append(
                f"INTERIM REVIEW at {r['days']}d "
                f"(raw {r['raw']} | alpha {r['alpha']}):\n{r['note']}"
            )
        if e["reflection"]:
            parts.append(f"REFLECTION:\n{e['reflection']}")
        return "\n\n".join(parts)

    def _format_reflection_only(self, e: dict) -> str:
        reviews = e.get("reviews", ())
        if e.get("pending") and reviews:
            latest = reviews[-1]
            tag = (
                f"[{e['date']} | {e['ticker']} | {e['rating']} | "
                f"in progress, {latest['days']}d {latest['raw']}]"
            )
            return f"{tag}\n{latest['note']}"
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {e['raw'] or 'n/a'}]"
        if e["reflection"]:
            return f"{tag}\n{e['reflection']}"
        text = e["decision"][:300]
        suffix = "..." if len(e["decision"]) > 300 else ""
        return f"{tag}\n{text}{suffix}"
