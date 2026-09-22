"""LEAPS build step 3: the overlay mandate, the LEAPS Analyst, and the
Portfolio Manager's instrument field."""

from __future__ import annotations

import pytest

from tradingagents.agents.schemas import (
    Instrument,
    PortfolioDecision,
    PortfolioRating,
    render_pm_decision,
)
from tradingagents.agents.utils.agent_utils import mandate_section
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.mandates import (
    EQUITY_MOMENTUM,
    EQUITY_MOMENTUM_LEAPS,
    get_mandate,
    render_mandate_context,
)
from tradingagents.mandates.analysts.leaps import LEAPS_ANALYST
from tradingagents.mandates.tools import leaps as lp
from tradingagents.mandates.tools.leaps_tools import LEAPS_TOOLS


def _decision(**kw):
    return PortfolioDecision(rating=PortfolioRating("Overweight"), executive_summary="s",
                             investment_thesis="t", **kw)


class TestInstrumentField:
    def test_unset_renders_exactly_as_before(self):
        """Every non-LEAPS decision must be byte-identical to the pre-field shape."""
        text = render_pm_decision(_decision())
        assert "Instrument" not in text
        assert text.endswith("**Time Horizon**: not provided")

    def test_set_renders_a_line_the_rating_parser_ignores(self):
        text = render_pm_decision(_decision(instrument=Instrument.CALL))
        assert text.endswith("**Instrument**: Call")
        assert parse_rating(text) == "Overweight"

    @pytest.mark.parametrize("text, expected", [
        ("**Rating**: Buy\n\n**Instrument**: Call", "Call"),
        ("**Instrument**: **Stock**", "Stock"),
        ("**Rating**: Buy", None),
    ])
    def test_the_instrument_reads_back_from_a_logged_decision(self, text, expected):
        assert lp.decision_instrument(text) == expected


class TestOverlayMandate:
    def test_registered(self):
        assert get_mandate("equity_momentum_leaps") is EQUITY_MOMENTUM_LEAPS

    @pytest.mark.parametrize("field", [
        "horizon_days", "review_horizons_days", "benchmark", "thesis_frame", "rating_guidance",
        "disqualifiers", "indicator_shortlist", "risk_frame", "analyst_guidance",
    ])
    def test_direction_is_momentums_exactly(self, field):
        """An overlay: everything that decides direction is inherited, not copied,
        so a fix to momentum reaches the LEAPS runs too."""
        assert getattr(EQUITY_MOMENTUM_LEAPS, field) == getattr(EQUITY_MOMENTUM, field)

    def test_adds_only_the_leaps_analyst(self):
        assert EQUITY_MOMENTUM_LEAPS.analysts == EQUITY_MOMENTUM.analysts + (LEAPS_ANALYST,)

    def test_keeps_momentums_role_guidance_and_adds_the_instrument_rule(self):
        g = EQUITY_MOMENTUM_LEAPS.agent_guidance
        for role, text in EQUITY_MOMENTUM.agent_guidance.items():
            assert g[role] == text
        assert "Instrument" in g["portfolio_manager"]
        assert "portfolio_manager" not in EQUITY_MOMENTUM.agent_guidance

    def test_only_the_leaps_portfolio_manager_is_asked_for_an_instrument(self):
        def pm(mandate):
            state = {"mandate": mandate.name, "mandate_context": render_mandate_context(mandate)}
            return mandate_section(state, "portfolio_manager")

        assert "Set the Instrument field" in pm(EQUITY_MOMENTUM_LEAPS)
        assert "Set the Instrument field" not in pm(EQUITY_MOMENTUM)

    def test_the_rule_never_lets_a_bearish_or_flat_call_be_a_call(self):
        rule = EQUITY_MOMENTUM_LEAPS.agent_guidance["portfolio_manager"]
        assert "Buy or Overweight" in rule and "all CLEAR" in rule
        assert "Hold, Underweight and Sell" in rule


class TestLeapsAnalyst:
    def test_uses_the_leaps_tool(self):
        assert LEAPS_ANALYST.tools == LEAPS_TOOLS

    def test_judges_the_instrument_not_the_direction_or_the_contract(self):
        msg = LEAPS_ANALYST.system_message
        assert "do not judge direction" in msg
        assert "do not choose a strike or expiry" in msg

    def test_reports_screens_as_given_and_recommends_a_call_only_when_all_clear(self):
        msg = LEAPS_ANALYST.system_message
        assert "exactly as the tool returned it" in msg
        assert "Call only when every screen is CLEAR" in msg
