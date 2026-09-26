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
MAX_NAMES="${NIGHTLY_MAX_NAMES:-5}"
BACKLOG="${NIGHTLY_BACKLOG:-2}"
PICKS="${NIGHTLY_PICKS:-8}"
CONTROLS="${NIGHTLY_CONTROLS:-4}"
BUDGET="${NIGHTLY_BUDGET:-60}"
# The standard (no-mandate, 5-day) strategy: its screen is free and always runs;
# its agent decisions cost model calls, so they are off until a count is chosen.
STANDARD_NAMES="${NIGHTLY_STANDARD_NAMES:-0}"
STANDARD_CONTROLS="${NIGHTLY_STANDARD_CONTROLS:-3}"

mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="$LOG_DIR/$STAMP.log"

# Colour codes in a log file are noise; width keeps tables from wrapping.
export NO_COLOR=1
export COLUMNS=200

exec > >(tee -a "$LOG") 2>&1

echo "=== nightly run $STAMP ==="
echo "mandate=$MANDATE max_names=$MAX_NAMES backlog=$BACKLOG picks=$PICKS controls=$CONTROLS budget=$BUDGET"
echo "standard: names=$STANDARD_NAMES controls=$STANDARD_CONTROLS"
echo

if [ ! -x "$TRADINGAGENTS" ]; then
    echo "FATAL: $TRADINGAGENTS is missing or not executable."
    exit 127
fi

cd "$REPO" || { echo "FATAL: cannot enter $REPO"; exit 1; }

echo "--- step 1: screen (no model calls) [$(date +%H:%M:%S)] ---"
if ! "$TRADINGAGENTS" screen --mandate "$MANDATE" --picks "$PICKS" \
        --controls "$CONTROLS" --budget "$BUDGET" < /dev/null; then
    echo "screen failed; not starting the agent loop on a stale shortlist."
    exit 1
fi

echo
# Scoped to $MANDATE, not every saved screen. Without this the job screens for
# one mandate and then adjudicates whatever is oldest across all of them -- so a
# job called "the value job" would quietly spend its budget on momentum names.
# The queue splits the names between the screen backlog and active trials
# (decision 5: 5 names, 2 backlog, 3 trials; unused slots go to the other side).
echo "--- step 2: adjudicate at most $MAX_NAMES $MANDATE names [$(date +%H:%M:%S)] ---"
"$TRADINGAGENTS" nightly-queue --mandate "$MANDATE" --max-names "$MAX_NAMES" --backlog "$BACKLOG" < /dev/null
STATUS=$?

echo
echo "--- summary [$(date +%H:%M:%S)] ---"
"$TRADINGAGENTS" screen-review --mandate "$MANDATE" < /dev/null || true

echo
# Reuses tonight's stored prices, so it costs almost no requests.
echo "--- step 3: standard screen (no model calls) [$(date +%H:%M:%S)] ---"
"$TRADINGAGENTS" screen --mandate none --controls "$STANDARD_CONTROLS" < /dev/null \
    || echo "standard screen failed; no standard picks today."

echo
# The market's state (trend and shape) for Desk and the lab: the price panel
# rebuilt from tonight's stored histories (no requests but a new month's
# listing), then the labels. About 80 seconds; a failure costs only freshness.
echo "--- market state: panel from stored prices, then the labels [$(date +%H:%M:%S)] ---"
"$TRADINGAGENTS" lab prices --no-fetch < /dev/null \
    && "$TRADINGAGENTS" lab regime --since "$(date -v-10d +%Y-%m-%d)" < /dev/null \
    || echo "market state not refreshed; Desk shows the last one."

# Shadow picks: what the candidate variants pick from tonight's close, recorded
# beside the live ordering and scored once their week has traded. Never traded,
# no model calls; the forward test the lab's past cannot give (lab/shadow.py).
echo "--- shadow picks [$(date +%H:%M:%S)] ---"
"$TRADINGAGENTS" lab shadow --record < /dev/null || echo "shadow picks not recorded tonight."

if [ "$STANDARD_NAMES" -gt 0 ]; then
    echo
    # Only today's screen, and only when today has an open to trade it at: a
    # 5-day pick is tradeable only the morning after it is screened, and a
    # weekend or holiday night's picks never reach an open.
    if "$TRADINGAGENTS" trade session-today < /dev/null; then
        echo "--- step 4: adjudicate at most $STANDARD_NAMES standard names [$(date +%H:%M:%S)] ---"
        "$TRADINGAGENTS" nightly-queue --mandate none --max-names "$STANDARD_NAMES" \
            --backlog "$STANDARD_NAMES" --fresh < /dev/null || true
    else
        echo "--- step 4: skipped: no trading session today, so no standard decisions ---"
    fi
fi

# Paper book, once the Alpaca keys are in .env: yesterday's fills are final by
# now, so reconcile first, then plan the open. Nothing is sent from here --
# every plan waits for the operator's approval.
if grep -q '^ALPACA_API_KEY=.' "$REPO/.env" 2>/dev/null; then
    echo
    echo "--- step 5: paper book: reconcile, then the order plan [$(date +%H:%M:%S)] ---"
    "$TRADINGAGENTS" trade reconcile < /dev/null || true
    "$TRADINGAGENTS" trade plan < /dev/null || true
fi

echo
echo "--- data store ---"
"$TRADINGAGENTS" store-stats < /dev/null || true

echo
echo "=== finished $(date +%H:%M:%S), screen-run exit=$STATUS ==="
echo "Read the reports with: tradingagents report"

# Keep a month of logs; they are small and the history is how you notice a job
# that has been failing since Tuesday.
find "$LOG_DIR" -name '*.log' -mtime +30 -delete 2>/dev/null

exit $STATUS
