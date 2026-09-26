"""What each agent is, reads and can call, from the code itself.

Tools come from the tool definitions and the analysts' tool nodes, so the
catalog cannot drift from what runs; the graph's roles and model tiers mirror
``graph/setup.py``. Vendors are the configured routing (``data_vendors`` and
``tool_vendors``), which is what a run actually calls.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import asdict, dataclass, field

# The graph, in order: (node name, role key, tier, analyst key or None, what it reads).
AGENTS = [
    ("Market Analyst", "market", "quick", "market",
     "prices and technical indicators for the ticker, and a verified market snapshot"),
    ("Sentiment Analyst", "social", "quick", "social",
     "news, StockTwits and Reddit posts for the ticker over the last week"),
    ("News Analyst", "news", "quick", "news",
     "company and global news for the past week, insider transactions, macro indicators, prediction markets"),
    ("Fundamentals Analyst", "fundamentals", "quick", "fundamentals",
     "company overview and financial statements"),
    ("Bull Researcher", "bull", "quick", None,
     "the four analyst reports, the debate so far, lessons from past decisions"),
    ("Bear Researcher", "bear", "quick", None,
     "the four analyst reports, the debate so far, lessons from past decisions"),
    ("Research Manager", "research_manager", "deep", None,
     "the analyst reports and the bull/bear debate; writes the investment plan"),
    ("Trader", "trader", "quick", None,
     "the investment plan and lessons from past decisions; proposes the transaction"),
    ("Aggressive Analyst", "aggressive", "quick", None,
     "the trader's plan, the analyst reports and the risk debate"),
    ("Conservative Analyst", "conservative", "quick", None,
     "the trader's plan, the analyst reports and the risk debate"),
    ("Neutral Analyst", "neutral", "quick", None,
     "the trader's plan, the analyst reports and the risk debate"),
    ("Portfolio Manager", "portfolio_manager", "deep", None,
     "everything above; gives the final rating (Buy / Overweight / Hold / Underweight / Sell)"),
]
NODES = [a[0] for a in AGENTS]


@dataclass
class ToolInfo:
    name: str
    description: str
    args: dict[str, str]
    category: str | None
    vendor: str | None
    used_by: list[str] = field(default_factory=list)


@dataclass
class AgentInfo:
    node: str
    role: str
    tier: str
    model: str
    reads: str
    tools: list[str]


def all_tools() -> dict:
    """Every agent tool defined in ``agents/utils``, by name."""
    from langchain_core.tools import BaseTool

    import tradingagents.agents.utils as pkg

    out = {}
    for m in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"{pkg.__name__}.{m.name}")
        for obj in vars(mod).values():
            if isinstance(obj, BaseTool):
                out.setdefault(obj.name, obj)
    return out


def analyst_tools() -> dict[str, list[str]]:
    """Tool names each analyst's tool node can execute."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return {key: sorted(node.tools_by_name) for key, node in TradingAgentsGraph._create_tool_nodes(None).items()}


def _category(tool: str) -> str | None:
    from tradingagents.dataflows.interface import TOOLS_CATEGORIES

    return next((c for c, info in TOOLS_CATEGORIES.items() if tool in info["tools"]), None)


def _vendor(tool: str, config: dict) -> str | None:
    if tool in (config.get("tool_vendors") or {}):
        return config["tool_vendors"][tool]
    cat = _category(tool)
    return (config.get("data_vendors") or {}).get(cat) if cat else None


def catalog(config: dict) -> dict:
    tools = all_tools()
    bound = analyst_tools()
    users: dict[str, list[str]] = {}
    for node, _role, _tier, key, _reads in AGENTS:
        for t in bound.get(key or "", []):
            users.setdefault(t, []).append(node)
    tool_infos = []
    for name, t in sorted(tools.items()):
        desc = inspect.cleandoc(t.description or "").split("\n\n")[0]
        args = {k: (v.get("description") or v.get("type") or "") for k, v in (t.args or {}).items()}
        tool_infos.append(ToolInfo(name, desc, args, _category(name), _vendor(name, config), users.get(name, [])))
    agents = [AgentInfo(node, role, tier, config.get(f"{tier}_think_llm", ""), reads, bound.get(key or "", []))
              for node, role, tier, key, reads in AGENTS]
    return {
        "agents": [asdict(a) for a in agents],
        "tools": [asdict(t) for t in tool_infos],
        "unused_tools": [t.name for t in tool_infos if not t.used_by],
        "vendors": dict(config.get("data_vendors") or {}),
        "tool_vendors": dict(config.get("tool_vendors") or {}),
        "models": {"deep": config.get("deep_think_llm"), "quick": config.get("quick_think_llm"),
                   "provider": config.get("llm_provider")},
        "settings": {k: config.get(k) for k in ("max_debate_rounds", "max_risk_discuss_rounds",
                                                "holding_period_days", "holding_period_framing")},
    }
