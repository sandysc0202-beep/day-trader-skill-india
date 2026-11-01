"""trader-analyst tests. The fixtures are invented: DEMO and DEMOBANK are made-up companies, written in
the exact JSON shape of Yahoo's fundamentals-timeseries answer and NSE's results RSS."""

import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

facts = load("trader-analyst", "facts")
DIR = os.path.join(FIX, "trader-analyst")
UNIVERSE = os.path.join(DIR, "universe.txt")


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(argv)
    return code, out.getvalue(), err.getvalue()


class Parse(unittest.TestCase):
    def test_quarterly_points_only(self):
        ts = facts.parse_timeseries(raw("timeseries-DEMO.json"))
        self.assertEqual(sorted(ts["revenue"]), ["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31",
                                                 "2026-06-30", "2026-09-30"])
        self.assertEqual(ts["revenue"]["2026-03-31"], 9.6e10)    # the 12M point for the year is ignored
        self.assertEqual(ts["gross_profit"], {})                   # a series Yahoo sent with no points

    def test_bad_answers(self):
        with self.assertRaises(facts.FactsError):
            facts.parse_timeseries(raw("timeseries-NOPE.json"))
        with self.assertRaises(facts.FactsError):
            facts.parse_timeseries(b"<html>")

    def test_fiscal_labels_follow_indias_april_year(self):
        self.assertEqual([facts.fiscal_label(d) for d in ("2026-06-30", "2026-09-30", "2026-12-31", "2027-03-31")],
                         ["Q1 FY27", "Q2 FY27", "Q3 FY27", "Q4 FY27"])

    def test_url(self):
        url = facts.ts_url("m&m", datetime.datetime(2026, 10, 30, tzinfo=datetime.timezone.utc))
        self.assertTrue(url.startswith("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/"
                                       "timeseries/M%26M.NS?symbol=M%26M.NS&type=quarterlyTotalRevenue,"))
        self.assertIn("quarterlyDilutedEPS", url)


class Analyse(unittest.TestCase):
    def setUp(self):
        self.r = facts.analyse(facts.parse_timeseries(raw("timeseries-DEMO.json")), name="Demo Industries Limited")

    def test_the_latest_quarter(self):
        cur = self.r["rows"][0]
        self.assertEqual((cur["end"], cur["fiscal"]), ("2026-09-30", "Q2 FY27"))
        self.assertEqual(cur["gross_profit"], 9.66e10 - 5.6e10)     # revenue minus cost: no gross series
        self.assertEqual((cur["gross_margin"], cur["operating_margin"], cur["net_margin"]), (42.0, 16.0, 11.6))
        self.assertEqual((cur["revenue_yoy"], cur["net_income_yoy"], cur["eps_yoy"]), (15.0, 17.9, 17.8))
        self.assertIsNone(self.r["rows"][2]["revenue_yoy"])        # no year-earlier quarter in the data

    def test_ttm(self):
        self.assertEqual(self.r["ttm"], {"revenue": 3.756e11, "net_income": 4.52e10, "eps_diluted": 60.27})

    def test_changes_in_crore(self):
        lines = facts.changes(self.r)
        self.assertEqual(lines[0], "Q2 FY27 (quarter ending 2026-09-30): revenue ₹9,660.0 cr (+15.0% vs a year "
                                   "earlier), net profit ₹1,120.0 cr (+17.9%), diluted EPS ₹14.93 (+17.8%).")
        self.assertIn("Gross margin 42.0% (+0.7 points vs the quarter before).", lines)

    def test_a_bank_has_no_gross_margin_and_a_one_off_is_flagged(self):
        r = facts.analyse(facts.parse_timeseries(raw("timeseries-DEMOBANK.json")))
        self.assertIsNone(r["rows"][0]["gross_margin"])
        self.assertIn("Net profit is above operating profit", " ".join(facts.changes(r)))


class Filings(unittest.TestCase):
    def test_the_newest_filing_for_the_name(self):
        f = facts.latest_result(raw("nse-results.xml"), "Demo Industries Limited")
        self.assertEqual(f["filed"], "2026-10-17 15:47 IST")
        self.assertTrue(f["url"].endswith("DEMO_Q2FY27.xml"))
        self.assertIsNone(facts.latest_result(raw("nse-results.xml"), "Demo Bank Limited"))
        self.assertEqual(facts.norm_name("DEMO INDUSTRIES LTD."), facts.norm_name("Demo Industries Limited"))


class Cli(unittest.TestCase):
    def test_report_metrics_and_notes(self):
        with tempfile.TemporaryDirectory() as d:
            m = os.path.join(d, "metrics.json")
            code, out, err = run(["DEMO", "DEMOBANK", "--offline", DIR, "--universe", UNIVERSE, "--metrics", m])
            self.assertEqual(code, 0, err)
            self.assertIn("Demo Industries Limited (DEMO). ₹ crore except EPS.", out)
            self.assertIn("2026-09-30  Q2 FY27       9,660   +15.0%   42.0%   16.0%   11.6%      1,120    14.93  +17.8%",
                          out)
            self.assertIn("Latest results filing on NSE: 2026-10-17 15:47 IST", out)
            self.assertIn("note: no DEMOBANK filing in NSE's results feed", out)
            with open(m) as fh:
                rows = {r["ticker"]: r for r in json.load(fh)["companies"]}
            self.assertEqual((rows["DEMO"]["fiscal"], rows["DEMO"]["currency"]), ("Q2 FY27", "INR"))

    def test_failures_and_json(self):
        code, _, err = run(["NOPE", "--offline", DIR, "--universe", UNIVERSE])
        self.assertEqual(code, 2)
        self.assertIn("NOPE", err)
        code, out, _ = run(["DEMO.NS", "--offline", DIR, "--json", "--no-filings"])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc[0]["ticker"], "DEMO")
        self.assertIsNone(doc[0]["filing"])
        code, out, _ = run(["DEMO", "--offline", DIR, "--universe", "/nonexistent.txt"])
        self.assertIn("no company name for DEMO", out)

    def test_the_metrics_example_is_what_it_writes(self):
        with open(os.path.join(ROOT, "trader-analyst", "metrics.example.json"), encoding="utf-8") as fh:
            ex = json.load(fh)["companies"][0]
        r = facts.analyse(facts.parse_timeseries(raw("timeseries-DEMO.json")), name="Demo Industries Limited")
        self.assertEqual(ex, facts.metrics_row("DEMO", r))


if __name__ == "__main__":
    unittest.main()
