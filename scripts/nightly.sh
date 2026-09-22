#!/bin/bash
# Nightly trade-agents run, for launchd.
#
# Two steps, in this order and for a reason:
#
#   1. `screen` costs no model calls, so it runs every night regardless. It is
#      the cheap half and the one that goes stale -- a shortlist is about a
#      particular day's prices.
#   2. `screen-run --max-names N` adjudicates at most N names. Unbounded, a full
#      sweep of the saved screens is tens of hours and hundreds of dollars of
#      model time; it is resumable, so a bounded run each night works through
#      the backlog instead of trying to clear it in one sitting.
#
# Everything is logged. An unattended run that fails quietly is worse than no
# run at all, so this never discards output and never hides a non-zero exit.

set -uo pipefail

REPO="/Users/jeremysmith/GIT/trade-agents"
LOG_DIR="$HOME/.tradingagents/logs/nightly"
TRADINGAGENTS="$HOME/.local/bin/tradingagents"

MANDATE="${NIGHTLY_MANDATE:-equity_value}"
MAX_NAMES="${NIGHTLY_MAX_NAMES:-3}"
PICKS="${NIGHTLY_PICKS:-8}"
CONTROLS="${NIGHTLY_CONTROLS:-4}"
BUDGET="${NIGHTLY_BUDGET:-60}"

mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="$LOG_DIR/$STAMP.log"

# Colour codes in a log file are noise; width keeps tables from wrapping.
export NO_COLOR=1
export COLUMNS=200

exec > >(tee -a "$LOG") 2>&1

echo "=== nightly run $STAMP ==="
echo "mandate=$MANDATE max_names=$MAX_NAMES picks=$PICKS controls=$CONTROLS budget=$BUDGET"
echo

if [ ! -x "$TRADINGAGENTS" ]; then
    echo "FATAL: $TRADINGAGENTS is missing or not executable."
    exit 127
fi

cd "$REPO" || { echo "FATAL: cannot enter $REPO"; exit 1; }

echo "--- step 1: screen (no model calls) ---"
if ! "$TRADINGAGENTS" screen --mandate "$MANDATE" --picks "$PICKS" \
        --controls "$CONTROLS" --budget "$BUDGET" < /dev/null; then
    echo "screen failed; not starting the agent loop on a stale shortlist."
    exit 1
fi

echo
# Scoped to $MANDATE, not every saved screen. Without this the job screens for
# one mandate and then adjudicates whatever is oldest across all of them -- so a
# job called "the value job" would quietly spend its budget on momentum names.
echo "--- step 2: adjudicate at most $MAX_NAMES $MANDATE names ---"
"$TRADINGAGENTS" screen-run --all --mandate "$MANDATE" --max-names "$MAX_NAMES" < /dev/null
STATUS=$?

echo
echo "--- summary ---"
"$TRADINGAGENTS" screen-review --mandate "$MANDATE" < /dev/null || true

echo
echo "=== finished $(date +%H:%M:%S), screen-run exit=$STATUS ==="
echo "Read the reports with: tradingagents report"

# Keep a month of logs; they are small and the history is how you notice a job
# that has been failing since Tuesday.
find "$LOG_DIR" -name '*.log' -mtime +30 -delete 2>/dev/null

exit $STATUS
