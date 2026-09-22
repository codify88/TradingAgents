"""EDGAR statements for companies Alpha Vantage has dropped.

Validated live against Coca-Cola, where both sources exist: margins, ROE, FCF
conversion and interest coverage matched exactly, ROIC within half a point. These
tests pin the pieces that made that true.
"""

from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.mandates.tools import edgar, financials as fin


def fact(val, end, filed, start=None, form="10-K"):
    f = {"val": val, "end": end, "filed": filed, "form": form}
    if start:
        f["start"] = start
    return f


def facts(gaap: dict, dei: dict | None = None) -> dict:
    wrap = lambda d: {tag: {"units": units} for tag, units in d.items()}  # noqa: E731
    return {"facts": {"us-gaap": wrap(gaap), "dei": wrap(dei or {})}}


# --- names ------------------------------------------------------------------------------


@pytest.mark.parametrize("vendor, sec", [
    ("Aarons Company Inc (The)", "AARON'S COMPANY, INC."),
    ("Arlington Asset Investment Corp - Class A", "ARLINGTON ASSET INVESTMENT CORP."),
    ("Atlas Air Worldwide Holdings Inc", "ATLAS AIR WORLDWIDE HOLDINGS INC"),
    ("Johnson & Johnson", "JOHNSON AND JOHNSON"),
    ("Acme Corporation", "ACME CORP"),
])
def test_vendor_and_sec_spellings_normalize_alike(vendor, sec):
    assert edgar.normalize_name(vendor) == edgar.normalize_name(sec)


class TestResolve:

    @pytest.fixture(autouse=True)
    def _index(self):
        exact = {"AARONS CO INC": {"0000000001", "0000000002"}, "ATLAS AIR INC": {"0000000009"}}
        stems = {"AARONS": {"0000000001", "0000000002"}, "ATLAS AIR": {"0000000009"}}
        with patch.object(edgar, "_name_index", return_value=(exact, stems)):
            yield

    def test_one_active_filer_resolves(self):
        with patch.object(edgar, "_was_filing", side_effect=lambda cik, as_of: cik == "0000000002"):
            assert edgar.resolve_cik("Aarons Company Inc (The)", "2022-03-01") == "0000000002"

    def test_several_active_filers_is_no_answer_not_a_guess(self):
        with patch.object(edgar, "_was_filing", return_value=True):
            assert edgar.resolve_cik("Aarons Company Inc (The)", "2022-03-01") is None

    def test_the_legal_form_tail_is_a_second_chance(self):
        with patch.object(edgar, "_was_filing", return_value=True):
            assert edgar.resolve_cik("Atlas Air Corporation", "2022-03-01") == "0000000009"

    def test_an_unknown_name_is_none(self):
        assert edgar.resolve_cik("Nobody Ever Filed Inc", "2022-03-01") is None


def test_was_filing_needs_periodic_reports_around_the_date():
    sub = {"filings": {"recent": {"form": ["8-K", "10-Q", "10-K"],
                                  "filingDate": ["2022-02-01", "2019-05-01", "2018-03-01"]}}}
    with patch.object(edgar, "_sec_json", return_value=sub):
        assert not edgar._was_filing("0000000001", "2022-03-01"), "an 8-K alone is not a live filer"
        assert edgar._was_filing("0000000001", "2019-06-01")


def test_a_reused_ticker_resolves_to_the_company_listed_on_the_date():
    rows = [{"symbol": "XYZ", "name": "Old Xyz Inc", "assetType": "Stock", "ipoDate": "2000-01-01", "delistingDate": "2010-06-01"},
            {"symbol": "XYZ", "name": "New Xyz Inc", "assetType": "Stock", "ipoDate": "2015-01-01", "delistingDate": "2023-06-01"}]
    with patch.object(edgar, "_delisted_listing", return_value=rows):
        assert edgar.delisted_name("XYZ", "2008-01-01") == "Old Xyz Inc"
        assert edgar.delisted_name("XYZ", "2020-01-01") == "New Xyz Inc"
        assert edgar.delisted_name("XYZ", "2024-01-01") == "New Xyz Inc"   # after delisting: latest prior listing


# --- facts -> statements ----------------------------------------------------------------------


def test_year_to_date_cash_flows_become_single_quarters():
    ytd = {("2021-01-01", "2021-04-02"): 10.0, ("2021-01-01", "2021-07-02"): 25.0,
           ("2021-01-01", "2021-10-01"): 45.0, ("2021-01-01", "2021-12-31"): 70.0}
    annual, quarters = edgar._flows(ytd)
    assert annual == {"2021-12-31": 70.0}
    assert quarters == {"2021-04-02": 10.0, "2021-07-02": 15.0, "2021-10-01": 20.0, "2021-12-31": 25.0}


def test_a_reported_quarter_wins_over_a_derived_one():
    values = {("2021-01-01", "2021-04-02"): 10.0, ("2021-01-01", "2021-07-02"): 25.0,
              ("2021-04-03", "2021-07-02"): 16.0}   # directly reported Q2 differs from 25 - 10
    _, quarters = edgar._flows(values)
    assert quarters["2021-07-02"] == 16.0


def test_a_fact_is_unknown_before_it_was_filed_and_a_restatement_only_after():
    units = [fact(100.0, "2021-12-31", "2022-02-20", start="2021-01-01"),
             fact(90.0, "2021-12-31", "2023-02-20", start="2021-01-01")]   # restated a year later
    assert edgar._known(units, "2022-02-19", duration=True) == {}
    assert edgar._known(units, "2022-06-01", duration=True) == {("2021-01-01", "2021-12-31"): 100.0}
    assert edgar._known(units, "2023-06-01", duration=True) == {("2021-01-01", "2021-12-31"): 90.0}


def test_tags_are_chosen_per_period_so_a_tag_switch_loses_nothing():
    """Coca-Cola moved long-term debt to a new tag in 2024."""
    gaap = {"LongTermDebtNoncurrent": {"units": {"USD": [fact(40.0, "2022-12-31", "2023-02-01"),
                                                         fact(41.0, "2023-12-31", "2024-02-01")]}},
            "LongTermDebtAndCapitalLeaseObligations": {"units": {"USD": [fact(99.0, "2023-12-31", "2024-02-01"),
                                                                         fact(42.0, "2024-12-31", "2025-02-01")]}}}
    merged = edgar._merged(gaap, edgar._INSTANT["longTermDebt"], "USD", "2026-01-01", duration=False)
    assert merged == {"2022-12-31": 40.0, "2023-12-31": 41.0, "2024-12-31": 42.0}


def test_shares_come_from_the_cover_after_the_period():
    dei = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        fact(4300.0, "2022-02-18", "2022-02-22"), fact(4310.0, "2022-10-20", "2022-10-25", form="10-Q")]}}}
    assert edgar._cover_shares({"dei": dei}, ["2021-12-31"], "2023-01-01") == {"2021-12-31": 4300.0}


def test_edgar_financials_speak_alpha_vantages_field_names():
    gaap = {
        "Revenues": {"USD": [fact(1000.0, "2021-12-31", "2022-02-20", start="2021-01-01")]},
        "OperatingIncomeLoss": {"USD": [fact(250.0, "2021-12-31", "2022-02-20", start="2021-01-01")]},
        "NetCashProvidedByUsedInOperatingActivities": {"USD": [fact(240.0, "2021-12-31", "2022-02-20", start="2021-01-01")]},
        "PaymentsForRepurchaseOfCommonStock": {"USD": [fact(30.0, "2021-12-31", "2022-02-20", start="2021-01-01")]},
        "LongTermDebtNoncurrent": {"USD": [fact(400.0, "2021-12-31", "2022-02-20")]},
        "LongTermDebtCurrent": {"USD": [fact(20.0, "2021-12-31", "2022-02-20")]},
        "CommercialPaper": {"USD": [fact(30.0, "2021-12-31", "2022-02-20")]},
        "StockholdersEquity": {"USD": [fact(800.0, "2021-12-31", "2022-02-20")]},
    }
    with patch.object(edgar, "_sec_json", return_value=facts(gaap)):
        f = edgar.edgar_financials("0000000001", "dead", "2022-06-01")
    row = f.annual.iloc[-1]
    assert f.ticker == "DEAD"
    assert row["totalRevenue"] == 1000.0 and row["operatingIncome"] == 250.0
    assert row["shortTermDebt"] == 50.0, "current maturities + commercial paper"
    assert row["proceedsFromRepurchaseOfEquity"] == -30.0, "Alpha Vantage's sign for buybacks"


def test_an_ifrs_filer_is_no_data():
    with patch.object(edgar, "_sec_json", return_value={"facts": {"ifrs-full": {}}}), \
         pytest.raises(fin.FinancialsUnavailable, match="IFRS"):
        edgar.edgar_financials("0000000001", "ABCM", "2022-03-01")


# --- the fallback path --------------------------------------------------------------------------


class TestFallback:

    def test_edgar_answers_when_alpha_vantage_has_nothing(self):
        stub = fin.Financials("DEAD", pd.Timestamp("2022-03-01"), "USD",
                              pd.DataFrame({"totalRevenue": [1.0]}, index=[pd.Timestamp("2021-12-31")]),
                              pd.DataFrame())
        with patch.object(fin, "_load_alpha_vantage", side_effect=fin.FinancialsUnavailable("empty")), \
             patch.object(edgar, "load_delisted_financials", return_value=stub):
            assert fin.load_financials("DEAD", "2022-03-01") is stub

    def test_alpha_vantages_reason_survives_when_edgar_has_nothing_either(self):
        with patch.object(fin, "_load_alpha_vantage", side_effect=fin.FinancialsUnavailable("AV empty")), \
             patch.object(edgar, "load_delisted_financials", side_effect=fin.FinancialsUnavailable("no filer")), \
             pytest.raises(fin.FinancialsUnavailable, match="AV empty"):
            fin.load_financials("DEAD", "2022-03-01")

    def test_an_edgar_outage_does_not_break_the_run(self):
        with patch.object(fin, "_load_alpha_vantage", side_effect=fin.FinancialsUnavailable("AV empty")), \
             patch.object(edgar, "load_delisted_financials", side_effect=RuntimeError("SEC down")), \
             pytest.raises(fin.FinancialsUnavailable, match="AV empty"):
            fin.load_financials("DEAD", "2022-03-01")

    def test_a_covered_company_never_touches_edgar(self):
        stub = object()
        with patch.object(fin, "_load_alpha_vantage", return_value=stub), \
             patch.object(edgar, "load_delisted_financials") as ed:
            assert fin.load_financials("KO", "2026-09-17") is stub
        ed.assert_not_called()
