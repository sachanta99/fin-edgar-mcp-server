"""Hand-built payloads shaped like real SEC and FRED responses.

They are synthetic (the numbers are illustrative, not real filings) so tests run offline and
each normalization rule has a case that exercises it. Re-record real responses with
``pytest -m live`` on a machine that can reach sec.gov.
"""

from __future__ import annotations

from typing import Any

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "2": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    "3": {"cik_str": 19617, "ticker": "JPM", "title": "JPMORGAN CHASE & CO"},
    "4": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
}


def fact(
    val: float,
    *,
    end: str,
    start: str | None = None,
    fy: int,
    fp: str,
    form: str,
    filed: str,
    accn: str,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed,
    }  # fmt: skip
    if start:
        out["start"] = start
    return out


def company_facts(cik: int, name: str, concepts: dict[str, dict[str, list[dict[str, Any]]]]):
    """concepts = {"us-gaap:Revenues": {"USD": [fact, ...]}}"""
    facts: dict[str, dict[str, Any]] = {}
    for key, units in concepts.items():
        taxonomy, _, concept = key.partition(":")
        facts.setdefault(taxonomy, {})[concept] = {"label": concept, "units": units}
    return {"cik": cik, "entityName": name, "facts": facts}


# Apple-like: revenue tag switches from SalesRevenueNet (FY2017) to
# RevenueFromContractWithCustomerExcludingAssessedTax (FY2018+); each 10-K repeats two prior
# years as comparatives; FY2019 revenue is restated in the FY2020 10-K; quarterly 10-Qs carry
# both 3-month and year-to-date values; no operating cash flow is tagged.
K17, K18, K19, K20 = (
    "0000320193-17-000070",
    "0000320193-18-000145",
    "0000320193-19-000119",
    "0000320193-20-000096",
)
Q20_2 = "0000320193-20-000052"

AAPL_FACTS = company_facts(
    320193,
    "Apple Inc.",
    {
        "us-gaap:SalesRevenueNet": {
            "USD": [
                fact(215_639e6, start="2015-09-27", end="2016-09-24", fy=2016, fp="FY",
                     form="10-K", filed="2016-10-26", accn="0001628280-16-020309"),
                fact(229_234e6, start="2016-09-25", end="2017-09-30", fy=2017, fp="FY",
                     form="10-K", filed="2017-11-03", accn=K17),
                fact(215_639e6, start="2015-09-27", end="2016-09-24", fy=2017, fp="FY",
                     form="10-K", filed="2017-11-03", accn=K17),
            ]
        },
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": {
            "USD": [
                fact(265_595e6, start="2017-10-01", end="2018-09-29", fy=2018, fp="FY",
                     form="10-K", filed="2018-11-05", accn=K18),
                fact(229_234e6, start="2016-09-25", end="2017-09-30", fy=2018, fp="FY",
                     form="10-K", filed="2018-11-05", accn=K18),
                fact(260_174e6, start="2018-09-30", end="2019-09-28", fy=2019, fp="FY",
                     form="10-K", filed="2019-10-31", accn=K19),
                fact(265_595e6, start="2017-10-01", end="2018-09-29", fy=2019, fp="FY",
                     form="10-K", filed="2019-10-31", accn=K19),
                fact(274_515e6, start="2019-09-29", end="2020-09-26", fy=2020, fp="FY",
                     form="10-K", filed="2020-10-30", accn=K20),
                # restated comparative: differs from the FY2019 10-K value
                fact(260_100e6, start="2018-09-30", end="2019-09-28", fy=2020, fp="FY",
                     form="10-K", filed="2020-10-30", accn=K20),
                # 10-Q, Q2 FY2020: 3-month value and 6-month year-to-date value
                fact(58_313e6, start="2019-12-29", end="2020-03-28", fy=2020, fp="Q2",
                     form="10-Q", filed="2020-05-01", accn=Q20_2),
                fact(150_129e6, start="2019-09-29", end="2020-03-28", fy=2020, fp="Q2",
                     form="10-Q", filed="2020-05-01", accn=Q20_2),
                fact(91_819e6, start="2019-09-29", end="2019-12-28", fy=2020, fp="Q1",
                     form="10-Q", filed="2020-01-29", accn="0000320193-20-000010"),
            ]
        },
        "us-gaap:NetIncomeLoss": {
            "USD": [
                fact(57_411e6, start="2019-09-29", end="2020-09-26", fy=2020, fp="FY",
                     form="10-K", filed="2020-10-30", accn=K20),
                fact(55_256e6, start="2018-09-30", end="2019-09-28", fy=2020, fp="FY",
                     form="10-K", filed="2020-10-30", accn=K20),
                fact(55_256e6, start="2018-09-30", end="2019-09-28", fy=2019, fp="FY",
                     form="10-K", filed="2019-10-31", accn=K19),
            ]
        },
        "us-gaap:EarningsPerShareDiluted": {
            "USD/shares": [
                fact(3.28, start="2019-09-29", end="2020-09-26", fy=2020, fp="FY",
                     form="10-K", filed="2020-10-30", accn=K20),
            ]
        },
        "us-gaap:Assets": {
            "USD": [
                fact(323_888e6, end="2020-09-26", fy=2020, fp="FY", form="10-K",
                     filed="2020-10-30", accn=K20),
                fact(338_516e6, end="2019-09-28", fy=2020, fp="FY", form="10-K",
                     filed="2020-10-30", accn=K20),
                fact(338_516e6, end="2019-09-28", fy=2019, fp="FY", form="10-K",
                     filed="2019-10-31", accn=K19),
                fact(320_400e6, end="2020-03-28", fy=2020, fp="Q2", form="10-Q",
                     filed="2020-05-01", accn=Q20_2),
            ]
        },
    },
)  # fmt: skip

# Microsoft-like: revenue tagged as us-gaap:Revenues.
MSFT_FACTS = company_facts(
    789019,
    "MICROSOFT CORP",
    {
        "us-gaap:Revenues": {
            "USD": [
                fact(143_015e6, start="2019-07-01", end="2020-06-30", fy=2020, fp="FY",
                     form="10-K", filed="2020-07-30", accn="0001564590-20-034944"),
            ]
        },
    },
)  # fmt: skip

# Tesla-like: revenue tagged including assessed tax, plus a foreign-currency-only concept.
TSLA_FACTS = company_facts(
    1318605,
    "Tesla, Inc.",
    {
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax": {
            "USD": [
                fact(31_536e6, start="2020-01-01", end="2020-12-31", fy=2020, fp="FY",
                     form="10-K", filed="2021-02-08", accn="0001564590-21-004599"),
            ]
        },
        "us-gaap:Assets": {
            "EUR": [
                fact(1_000e6, end="2020-12-31", fy=2020, fp="FY", form="10-K",
                     filed="2021-02-08", accn="0001564590-21-004599"),
            ]
        },
    },
)  # fmt: skip


AAPL_SUBMISSIONS = {
    "cik": "320193",
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "filings": {
        "recent": {
            "accessionNumber": [K20, "0000320193-20-000090", Q20_2, K19],
            "filingDate": ["2020-10-30", "2020-09-01", "2020-05-01", "2019-10-31"],
            "reportDate": ["2020-09-26", "", "2020-03-28", "2019-09-28"],
            "form": ["10-K", "8-K", "10-Q", "10-K"],
            "primaryDocument": [
                "aapl-20200926.htm", "d8k.htm", "a10-q20203282020.htm", "a10-k20199282019.htm",
            ],
            "primaryDocDescription": ["10-K", "8-K", "10-Q", "10-K"],
        },
        "files": [
            {"name": "CIK0000320193-submissions-001.json", "filingFrom": "1994-01-26",
             "filingTo": "2019-06-01"},
        ],
    },
}  # fmt: skip

AAPL_SUBMISSIONS_PAGE = {
    "accessionNumber": [K18, K17],
    "filingDate": ["2018-11-05", "2017-11-03"],
    "reportDate": ["2018-09-29", "2017-09-30"],
    "form": ["10-K", "10-K"],
    "primaryDocument": ["a10-k20189292018.htm", "a10-k20179302017.htm"],
    "primaryDocDescription": ["10-K", "10-K"],
}

FULL_TEXT = {
    "hits": {
        "total": {"value": 2, "relation": "eq"},
        "hits": [
            {
                "_id": f"{K20}:aapl-20200926.htm",
                "_source": {
                    "ciks": ["0000320193"], "display_names": ["Apple Inc.  (AAPL)  (CIK 0000320193)"],
                    "form": "10-K", "file_date": "2020-10-30", "period_ending": "2020-09-26",
                    "adsh": K20, "file_description": "10-K",
                },
            },
            {
                "_id": "0000789019-20-000001:msft-10k.htm",
                "_source": {
                    "ciks": ["0000789019"], "display_names": ["MICROSOFT CORP  (MSFT)"],
                    "form": "10-K", "file_date": "2020-07-30", "period_ending": "2020-06-30",
                    "adsh": "0000789019-20-000001",
                },
            },
        ],
    }
}  # fmt: skip

FRED_SERIES_UNRATE = {
    "seriess": [
        {
            "id": "UNRATE",
            "title": "Unemployment Rate",
            "units": "Percent",
            "frequency": "Monthly",
            "seasonal_adjustment": "Seasonally Adjusted",
            "last_updated": "2020-11-06 07:44:02-06",
        }
    ]
}

FRED_OBS_UNRATE = {
    "observations": [
        {"date": "2020-01-01", "value": "3.5"},
        {"date": "2020-02-01", "value": "3.5"},
        {"date": "2020-03-01", "value": "."},
        {"date": "2020-04-01", "value": "14.7"},
    ]
}
