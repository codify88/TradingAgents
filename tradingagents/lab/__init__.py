"""The screen lab: rerun a screener on many past dates, cheaply, and say what works.

No model calls. A strategy's screen is replayed on every schedule date from a
price panel built out of stored daily histories, each variant is scored on the
benchmark ladder (picks vs the eligible pool vs the style index vs the S&P 500),
tuned on early dates and tested on later ones, and every variant tried is
counted so the bar a winner must clear rises with the search.

Design: docs/design/go-live.md (phase 1).
"""
