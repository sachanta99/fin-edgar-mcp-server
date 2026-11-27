from datetime import date

from fin_edgar_mcp.financials import STANDARD_METRICS, normalize_all, normalize_metric

from . import factories as f

REVENUE = STANDARD_METRICS["revenue"]


def test_revenue_merges_across_tag_switch():
    series = normalize_metric(f.AAPL_FACTS, REVENUE, "annual")
    ends = [x.period_end for x in series]
    assert ends == [
        date(2020, 9, 26), date(2019, 9, 28), date(2018, 9, 29),
        date(2017, 9, 30), date(2016, 9, 24),
    ]  # fmt: skip
    by_end = {x.period_end: x for x in series}
    # FY2017 is reported under both tags; the higher-priority new tag wins.
    assert by_end[date(2017, 9, 30)].concept.endswith(
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    # FY2016 only exists under the old tag.
    assert by_end[date(2016, 9, 24)].concept == "us-gaap:SalesRevenueNet"


def test_restatement_uses_latest_filing_and_is_flagged():
    by_end = {x.period_end: x for x in normalize_metric(f.AAPL_FACTS, REVENUE, "annual")}
    fy19 = by_end[date(2019, 9, 28)]
    assert fy19.value == 260_100e6
    assert fy19.accession_number == f.K20
    assert fy19.restated is True
    # Labelled by the filing that first reported it as its current year.
    assert fy19.fiscal_year == 2019


def test_repeated_comparatives_are_not_restatements():
    by_end = {x.period_end: x for x in normalize_metric(f.AAPL_FACTS, REVENUE, "annual")}
    fy18 = by_end[date(2018, 9, 29)]
    assert fy18.restated is False
    assert fy18.fiscal_year == 2018
    assert fy18.accession_number == f.K19  # latest filing that reported it


def test_quarterly_skips_year_to_date_values():
    series = normalize_metric(f.AAPL_FACTS, REVENUE, "quarterly")
    assert [(x.fiscal_period, x.value) for x in series] == [("Q2", 58_313e6), ("Q1", 91_819e6)]


def test_balance_sheet_instants():
    assets = STANDARD_METRICS["total_assets"]
    annual = normalize_metric(f.AAPL_FACTS, assets, "annual")
    assert [(x.period_end, x.period_start) for x in annual] == [
        (date(2020, 9, 26), None),
        (date(2019, 9, 28), None),
    ]
    quarterly = normalize_metric(f.AAPL_FACTS, assets, "quarterly")
    assert [x.value for x in quarterly] == [320_400e6]


def test_eps_uses_per_share_unit():
    eps = normalize_metric(f.AAPL_FACTS, STANDARD_METRICS["eps_diluted"], "annual")
    assert eps[0].unit == "USD/shares"
    assert eps[0].value == 3.28


def test_three_revenue_tags_across_companies():
    concepts = {
        normalize_metric(facts, REVENUE, "annual")[0].concept
        for facts in (f.AAPL_FACTS, f.MSFT_FACTS, f.TSLA_FACTS)
    }
    assert concepts == {
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    }


def test_single_foreign_unit_is_used_and_labelled():
    assets = normalize_metric(f.TSLA_FACTS, STANDARD_METRICS["total_assets"], "annual")
    assert assets[0].unit == "EUR"


def test_missing_metric_returns_empty():
    ocf = STANDARD_METRICS["operating_cash_flow"]
    assert normalize_metric(f.AAPL_FACTS, ocf, "annual") == []


def test_normalize_all_covers_every_metric_and_period():
    out = normalize_all(f.AAPL_FACTS)
    assert len(out) == len(STANDARD_METRICS) * 2
    assert out[("revenue", "annual")]


def test_bad_rows_are_ignored():
    facts = f.company_facts(
        1,
        "X",
        {
            "us-gaap:Revenues": {
                "USD": [
                    {"end": "2020-12-31", "start": "2020-01-01", "val": None, "accn": "a",
                     "fy": 2020, "fp": "FY", "form": "10-K", "filed": "2021-01-01"},
                    {"end": "not-a-date", "start": "2020-01-01", "val": 1, "accn": "b",
                     "fy": 2020, "fp": "FY", "form": "10-K", "filed": "2021-01-01"},
                    {"end": "2020-12-31", "start": "2020-01-01", "val": 1, "accn": "c",
                     "fy": 2020, "fp": "FY", "form": "8-K", "filed": "2021-01-01"},
                ]
            }
        },
    )  # fmt: skip
    assert normalize_metric(facts, REVENUE, "annual") == []
