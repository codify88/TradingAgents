"""Agent variants: named, versioned edits to what the agents are told and given.

A variant changes only what it names. Everything else is the production
configuration, so a comparison against ``baseline`` isolates the change:

- ``edits``: per-agent prompt edits, applied at call time by ``hook.py`` --
  ``append`` or ``prepend`` a block, or ``replace`` an exact passage (``find``)
  that must occur in the prompt the agent receives;
- ``models``: the deep and quick models (e.g. a cheap model for a first test);
- ``settings``: debate rounds, holding period and its framing;
- ``vendors``: data-vendor overrides by category (``data_vendors``);
- ``extra_tools``: tools added to an analyst (by analyst key), e.g. the
  harvested ownership data no analyst calls today.

Every save is appended to ``history.jsonl`` with its version, so a run can name
exactly the text it used.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .catalog import AGENTS, NODES

KINDS = ("append", "prepend", "replace")
SETTINGS = ("max_debate_rounds", "max_risk_discuss_rounds", "holding_period_days", "holding_period_framing")
ANALYST_KEYS = tuple(a[3] for a in AGENTS if a[3])
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
BASELINE = "baseline"


@dataclass
class Edit:
    agent: str              # a node name from catalog.NODES, or "*" for every agent
    kind: str               # append | prepend | replace
    text: str
    find: str = ""          # for replace: the exact passage to replace


@dataclass
class Variant:
    name: str
    description: str = ""
    edits: list[Edit] = field(default_factory=list)
    models: dict[str, str] = field(default_factory=dict)      # {"deep": ..., "quick": ...}
    settings: dict = field(default_factory=dict)
    vendors: dict[str, str] = field(default_factory=dict)
    extra_tools: dict[str, list[str]] = field(default_factory=dict)
    version: int = 0
    saved: str = ""

    def config_overrides(self) -> dict:
        """The run config keys this variant sets."""
        out: dict = {}
        if self.models.get("deep"):
            out["deep_think_llm"] = self.models["deep"]
        if self.models.get("quick"):
            out["quick_think_llm"] = self.models["quick"]
        out.update({k: v for k, v in self.settings.items() if k in SETTINGS})
        return out


def validate(v: Variant, known_tools: set[str] | None = None, known_vendors: set[str] | None = None) -> list[str]:
    """Why this variant cannot be saved, or []."""
    errors = []
    if not NAME.match(v.name):
        errors.append("name: lowercase letters, digits and dashes, up to 40 characters")
    for i, e in enumerate(v.edits, 1):
        if e.agent != "*" and e.agent not in NODES:
            errors.append(f"edit {i}: unknown agent {e.agent!r}")
        if e.kind not in KINDS:
            errors.append(f"edit {i}: kind must be one of {', '.join(KINDS)}")
        if e.kind == "replace" and not e.find.strip():
            errors.append(f"edit {i}: a replace needs the exact passage to find")
        if not e.text.strip() and e.kind != "replace":
            errors.append(f"edit {i}: nothing to add")
    for k in v.models:
        if k not in ("deep", "quick"):
            errors.append(f"models: {k!r} is not deep or quick")
    for k, val in v.settings.items():
        if k not in SETTINGS:
            errors.append(f"settings: {k!r} is not one of {', '.join(SETTINGS)}")
        elif k == "holding_period_framing" and not isinstance(val, bool):
            errors.append("settings: holding_period_framing is true or false")
        elif k != "holding_period_framing" and (not isinstance(val, int) or not 0 < val <= 60):
            errors.append(f"settings: {k} must be a whole number from 1 to 60")
    for key, names in v.extra_tools.items():
        if key not in ANALYST_KEYS:
            errors.append(f"extra_tools: {key!r} is not an analyst ({', '.join(ANALYST_KEYS)})")
        for t in names:
            if known_tools is not None and t not in known_tools:
                errors.append(f"extra_tools: unknown tool {t!r}")
    if known_vendors is not None:
        for cat, vendor in v.vendors.items():
            if vendor not in known_vendors:
                errors.append(f"vendors: {cat} -> unknown vendor {vendor!r}")
    return errors


def _dir(config: dict) -> Path:
    d = Path(config["data_cache_dir"]) / "agentlab" / "variants"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _from_dict(d: dict) -> Variant:
    d = dict(d)
    d["edits"] = [Edit(**e) for e in d.get("edits", [])]
    return Variant(**{k: v for k, v in d.items() if k in Variant.__dataclass_fields__})


def load(config: dict, name: str) -> Variant:
    if name == BASELINE:
        return Variant(BASELINE, "The production agents, unchanged.")
    if not NAME.match(name):
        raise ValueError(f"no variant {name!r}")
    path = _dir(config) / f"{name}.json"
    if not path.exists():
        raise ValueError(f"no variant {name!r}")
    return _from_dict(json.loads(path.read_text()))


def all_variants(config: dict) -> list[Variant]:
    out = [load(config, BASELINE)]
    out += [_from_dict(json.loads(p.read_text())) for p in sorted(_dir(config).glob("*.json"))]
    return out


def save(config: dict, v: Variant, **validation) -> Variant:
    if v.name == BASELINE:
        raise ValueError("baseline is the production agents; save a variant under another name")
    errors = validate(v, **validation)
    if errors:
        raise ValueError("; ".join(errors))
    path = _dir(config) / f"{v.name}.json"
    previous = json.loads(path.read_text()).get("version", 0) if path.exists() else 0
    v.version = previous + 1
    v.saved = datetime.now(UTC).isoformat(timespec="seconds")
    body = asdict(v)
    path.write_text(json.dumps(body, indent=2))
    with (_dir(config) / "history.jsonl").open("a") as fh:
        fh.write(json.dumps(body) + "\n")
    return v


def history(config: dict, name: str) -> list[dict]:
    try:
        rows = [json.loads(x) for x in (_dir(config) / "history.jsonl").read_text().splitlines() if x.strip()]
    except OSError:
        return []
    return [r for r in rows if r["name"] == name]


def from_json(data: dict) -> Variant:
    return _from_dict(data)
