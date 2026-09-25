"""Paper execution: order plans from a strategy's decisions, approved by the
operator, placed with the broker, reconciled every evening.

Nothing is sent without an approval, a plan expires at the open it was made
for, and a halt or an unacknowledged mismatch blocks every new order. Paper
only until the live endpoint is enabled on purpose (go-live phase 5).

Design: docs/design/go-live.md (phase 3).
"""
