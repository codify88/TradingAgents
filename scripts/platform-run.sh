#!/bin/bash
# The nightly chain, for launchd: the nightly run, then the harvest, in one job
# kept awake by caffeinate so the Mac cannot drop back to sleep between steps.
#
# Each step logs to its own file (nightly.sh keeps its own log). A failed step
# does not skip the next -- the harvest makes no model calls and is worth
# running after a failed adjudication -- but the chain returns the first
# failure, so launchd and the health check see it.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/.tradingagents/logs/platform"

# Re-exec under caffeinate once: -i holds off idle sleep for the whole chain.
if [ -z "${TA_CAFFEINATED:-}" ]; then
    export TA_CAFFEINATED=1
    exec /usr/bin/caffeinate -i "$0" "$@"
fi

mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="$LOG_DIR/$STAMP.log"
FIRST_FAILURE=0

step() {
    local name="$1"; shift
    echo "$(date +%H:%M:%S) start $name" >> "$LOG"
    "$@" < /dev/null
    local status=$?
    echo "$(date +%H:%M:%S) end $name exit=$status" >> "$LOG"
    if [ "$status" -ne 0 ] && [ "$FIRST_FAILURE" -eq 0 ]; then
        FIRST_FAILURE=$status
    fi
}

step nightly "$REPO/scripts/nightly.sh"
if [ -x "$REPO/scripts/harvest.sh" ]; then
    step harvest "$REPO/scripts/harvest.sh"
else
    echo "$(date +%H:%M:%S) skip harvest (scripts/harvest.sh not installed yet)" >> "$LOG"
fi

find "$LOG_DIR" -name '*.log' -mtime +30 -delete 2>/dev/null
exit "$FIRST_FAILURE"
