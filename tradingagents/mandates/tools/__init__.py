"""Deterministic analysis tools owned by the mandates.

These are *computations* over vendor data, not new vendors, so they live here
rather than in ``dataflows/``: registering them with upstream's vendor router
would put every future mandate tool on a file upstream edits often. They reuse
upstream's Alpha Vantage request helper unmodified.

Design rule: compute in Python, interpret in the LLM. Every ratio, growth rate,
percentile and discounted value an analyst cites comes out of this package.
"""
