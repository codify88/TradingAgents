#!/bin/bash
# Keep a wake scheduled before each trade-agents job. Runs as root, from a
# root-owned copy installed by scripts/install-platform.sh; it never runs code
# from the repo. It reads a plain list of HH:MM wake times, written by
# `tradingagents schedule export`, and makes sure `pmset` has a wake set for the
# next occurrence of each.
#
# Idempotent: run at load and every 30 minutes, it adds a wake only where none
# exists at that time -- a repeating wake you set yourself (pmset repeat) counts
# -- and cancels only wakes it set itself (owner "tradingagents") that the
# schedule no longer asks for. It never changes or removes a pmset repeat.
#
# Test hooks (not used in production): PMSET (a stand-in pmset), NOW_EPOCH
# (a fixed "now"), WAKE_SCHEDULE (the schedule path), DRY_RUN=1 (print only).

set -uo pipefail

SCHEDULE="${WAKE_SCHEDULE:-__SCHEDULE_PATH__}"
PMSET="${PMSET:-/usr/bin/pmset}"
OWNER="tradingagents"
NOW="${NOW_EPOCH:-$(date +%s)}"
# A wake closer than this is left alone: it is about to fire, or just did.
MIN_AHEAD=120

log() { echo "$(date -r "$NOW" '+%Y-%m-%d %H:%M:%S') $*"; }

run() {
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "DRY: $*"
    else
        "$@"
    fi
}

if [ ! -r "$SCHEDULE" ]; then
    log "no schedule at $SCHEDULE; nothing to do"
    exit 0
fi

SCHED_OUT="$("$PMSET" -g sched 2>/dev/null)"

# "wakepoweron at 1:55AM every day" -> 01:55
repeat_hhmm() {
    echo "$SCHED_OUT" | sed -nE 's/.*wake(poweron)? at ([0-9]{1,2}):([0-9]{2})(AM|PM) every day.*/\2 \3 \4/p' |
    while read -r h m ap; do
        h=$((10#$h))
        if [ "$ap" = "PM" ] && [ "$h" -ne 12 ]; then h=$((h + 12)); fi
        if [ "$ap" = "AM" ] && [ "$h" -eq 12 ]; then h=0; fi
        printf '%02d:%s\n' "$h" "$m"
    done
}
REPEATS="$(repeat_hhmm)"

WANTED=""
while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(echo "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    # Strict format: anything else is ignored, never passed to pmset.
    if ! [[ "$line" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
        log "ignoring malformed line: $line"
        continue
    fi
    if echo "$REPEATS" | grep -qx "$line"; then
        continue  # a repeating wake already covers it
    fi
    today="$(date -r "$NOW" +%Y-%m-%d)"
    at="$(date -j -f '%Y-%m-%d %H:%M:%S' "$today $line:00" +%s)"
    if [ $((at - NOW)) -lt "$MIN_AHEAD" ]; then
        at=$((at + 86400))
        # Recompute from the calendar date so a DST change keeps the wall clock.
        tomorrow="$(date -r "$at" +%Y-%m-%d)"
        at="$(date -j -f '%Y-%m-%d %H:%M:%S' "$tomorrow $line:00" +%s)"
    fi
    shown="$(date -r "$at" '+%m/%d/%Y %H:%M:%S')"
    WANTED="$WANTED$shown"$'\n'
    if echo "$SCHED_OUT" | grep -q "wake at $shown"; then
        continue  # already scheduled, by us or anyone
    fi
    run "$PMSET" schedule wake "$(date -r "$at" '+%m/%d/%y %H:%M:%S')" "$OWNER" &&
        log "scheduled wake at $shown for $line"
done < "$SCHEDULE"

# Cancel wakes we set that the schedule no longer wants.
echo "$SCHED_OUT" | sed -nE "s/.*wake at ([0-9]{2}\/[0-9]{2}\/[0-9]{4} [0-9:]{8}) by '$OWNER'.*/\1/p" |
while read -r d t; do
    shown="$d $t"
    if ! printf '%s' "$WANTED" | grep -qx "$shown"; then
        short="$(date -j -f '%m/%d/%Y %H:%M:%S' "$shown" '+%m/%d/%y %H:%M:%S')"
        run "$PMSET" schedule cancel wake "$short" "$OWNER" && log "cancelled stale wake at $shown"
    fi
done
exit 0
