#!/bin/bash
# The 08:00 report (trade-agents docs/design/hermes.md): last night's run, what
# it decided, and reviews due this week. Deterministic -- no model writes it.
exec /Users/jeremysmith/.local/bin/tradingagents morning-report
