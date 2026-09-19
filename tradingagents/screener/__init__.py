"""Candidate selection: decide what the agent loop should spend its time on.

The agent loop is an adjudication tool, not a search tool. A full run costs
roughly thirty LLM calls and several minutes, and there are ~8,600 active US
common stocks, so something cheap has to choose what gets adjudicated.

Two rules shape this package, both of them about not rigging the debate:

1. **Exclusions do the narrowing, not the thesis.** A screener that ranks on
   the same signals the analysts then weigh hands each analyst a name
   pre-selected to look good on its own criteria, and every candidate
   "confirms". So the heavy lifting is done by disqualifiers -- the mandate's
   own hard screens, run in reverse -- which answer "is this name ineligible?"
   rather than "is this name attractive?".

2. **Every screen carries a random control.** A handful of names drawn at
   random from the same eligible pool go through the loop alongside the ranked
   picks. Without them there is no way to tell whether the ordering adds
   anything over picking eligible names at random, which is the only question
   that matters about a screener.
"""

from .manifest import ScreenManifest, load_manifests, save_manifest
from .screen import ScreenResult, run_screen
from .universe import Candidate, load_universe

__all__ = [
    "Candidate",
    "ScreenManifest",
    "ScreenResult",
    "load_manifests",
    "load_universe",
    "run_screen",
    "save_manifest",
]
