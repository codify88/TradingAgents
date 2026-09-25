#!/bin/bash
# The harvest, as the second step of the nightly chain (scripts/platform-run.sh).
#
# Makes no model calls. Pulls transcripts, insider and congressional trades, and
# institutional and ETF holdings ahead of need, within a request budget and a
# deadline: 25,000 requests a night until the first pass completes (~8 nights,
# decided 2026-09-25), after which the work that is due falls to ~2,000 a night
# on its own. It stops by 07:30 whatever is left, so the Mac is free before the
# 07:55 wake and the 08:00 report.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/.tradingagents/logs/harvest"
TRADINGAGENTS="$HOME/.local/bin/tradingagents"
MAX_REQUESTS="${HARVEST_MAX_REQUESTS:-25000}"
STOP_AT="${HARVEST_STOP_AT:-07:30}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%Y%m%d_%H%M%S).log"
export NO_COLOR=1 COLUMNS=200
exec > >(tee -a "$LOG") 2>&1

echo "=== harvest $(date +%Y-%m-%d\ %H:%M:%S) max_requests=$MAX_REQUESTS stop_at=$STOP_AT ==="
cd "$REPO" || exit 1
"$TRADINGAGENTS" harvest --max-requests "$MAX_REQUESTS" --stop-at "$STOP_AT" < /dev/null
STATUS=$?
echo
"$TRADINGAGENTS" harvest-status < /dev/null || true
echo "=== finished $(date +%H:%M:%S), harvest exit=$STATUS ==="
find "$LOG_DIR" -name '*.log' -mtime +30 -delete 2>/dev/null
exit $STATUS
