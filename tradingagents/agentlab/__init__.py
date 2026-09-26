"""The agent lab: see what each agent reads and can call, edit its prompt as a
named variant, and test variants on fixed historical cases, like the screen lab.

- ``catalog``: every agent in graph order -- model tier, what it reads, the tools
  it can call and the vendor behind each -- and every tool defined but unused.
- ``variants``: named, versioned edits -- per-agent prompt edits, models,
  settings, vendors, extra tools.
- ``hook``: applies a variant at call time and captures each agent's prompt,
  inside agent-lab runs only; production runs never load it.
- ``suites``: fixed sets of historical cases (screens: picks and controls).
- ``runs``: a variant on a suite, in its own directory, with metrics.

Design: docs/design/agent-lab.md.
"""
