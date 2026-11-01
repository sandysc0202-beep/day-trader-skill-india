import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
UNIVERSE = os.path.join(DIR, "universe.txt")
NOW = "2026-10-14T00:00:00+00:00"   # 05:30 IST
NAMES = {"RELIANCE": "Reliance Industries Limited", "INFY": "Infosys Limited", "M&M": "Mahindra & Mahindra Limited",
         "RIIL": "Reliance Industrial Infrastructure Limited"}


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_yahoo_rss(self):
        items = news.parse_rss(raw("yahoo-AAPL.xml"), "AAPL")
        self.assertEqual(len(items), 6)
        first = items[0]
        self.assertEqual(first["title"], "Apple may have a way to dominate the smart home space")
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 13, 19, 46, 52,
                                                          tzinfo=datetime.timezone.utc))
        self.assertTrue(first["link"].startswith("https://finance.yahoo.com/"))
        self.assertTrue(first["id"].startswith("rss:"))

    def test_nse_announcements(self):
        raw_items = news.parse_nse_rss(raw("nse-announcements.xml"), "announcements")
        self.assertEqual([r["company"] for r in raw_items],
                         ["Reliance Industries Limited", "Infosys Limited", "MAHINDRA & MAHINDRA LTD.",
                          "Reliance Industrial Infrastructure Limited"])
        # NSE's pubDate is India time with no zone: 18:05:11 IST is 12:35:11 UTC
        self.assertEqual(raw_items[0]["time"].isoformat(), "2026-10-13T12:35:11+00:00")
        items = news.nse_items(raw_items, {"RELIANCE": NAMES["RELIANCE"], "M&M": NAMES["M&M"]})
        self.assertEqual([i["ticker"] for i in items], ["RELIANCE", "M&M"])   # exact names only: not RIIL
        rel = items[0]
        self.assertTrue(rel["important"])                                     # outcome of board meeting
        self.assertTrue(rel["title"].startswith("Reliance Industries Limited, NSE announcement: "))
        self.assertTrue(rel["id"].startswith("nse:announcements:"))
        self.assertTrue(items[1]["important"])                                # credit rating

    def test_names_match_whatever_the_case_and_suffix(self):
        self.assertEqual(news.norm_name("MAHINDRA & MAHINDRA LTD."), news.norm_name("Mahindra and Mahindra Limited"))
        self.assertNotEqual(news.norm_name("Reliance Industries Limited"),
                            news.norm_name("Reliance Industrial Infrastructure Limited"))
        self.assertEqual(news.names_from_universe(UNIVERSE), NAMES)
        self.assertEqual(news.names_from_universe("/nonexistent.txt"), {})

    def test_times(self):
        self.assertEqual(news.parse_time("Tue, 13 Oct 2026 20:02:03 +0000").hour, 20)
        self.assertEqual(news.parse_time("2026-09-01T16:15:35+05:30").hour, 10)
        self.assertEqual(news.parse_time("01-Nov-2026 02:05:35").isoformat(), "2026-10-31T20:35:35+00:00")
        self.assertIsNone(news.parse_time("soon"))
        self.assertIsNone(news.parse_time("31-Foo-2026 10:00:00"))

    def test_yahoo_symbols(self):
        self.assertEqual(news.yahoo_symbol("reliance"), "RELIANCE.NS")
        self.assertEqual(news.yahoo_symbol("M&M.NS"), "M&M.NS")


class Tagging(unittest.TestCase):
    def test_touches_and_tags(self):
        items = news.parse_rss(raw("yahoo-KO.xml"), "KO")
        watch = {"KO": "Coca-Cola", "CVX": "Chevron", "AAPL": "Apple"}
        news.tag(items, watch)
        by = {i["title"][:20]: i for i in items}
        chevron = by["Coca-Cola or Chevron"]
        self.assertEqual(chevron["touches"], ["CVX", "KO"])
        self.assertIn("capital", chevron["tags"])  # dividend
        self.assertEqual(by["Is Coca-Cola (KO) St"]["touches"], ["KO"])

    def test_results_filings_are_tagged_earnings(self):
        raw_items = news.parse_nse_rss(raw("nse-results.xml"), "results")
        items = news.tag(news.nse_items(raw_items, {"INFY": NAMES["INFY"]}), {"INFY": NAMES["INFY"]})
        self.assertEqual(items[0]["tags"][0], "earnings")
        self.assertTrue(items[0]["important"])
        ann = news.nse_items(news.parse_nse_rss(raw("nse-announcements.xml"), "announcements"),
                             {"INFY": NAMES["INFY"]})
        self.assertFalse(ann[0]["important"])     # an analyst meet: shown, not flagged

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("yahoo-AAPL.xml"), "AAPL")
        b = news.parse_rss(raw("yahoo-AAPL.xml"), "GOOGL")
        items = news.dedupe(news.tag(a + b, {"AAPL": None, "GOOGL": None}))
        self.assertEqual(len(items), 6)
        self.assertEqual(items[0]["touches"], ["AAPL", "GOOGL"])


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW, "--universe", UNIVERSE,
                     "--portfolio", os.path.join(self.tmp.name, "none.json"),
                     "--watchlist", os.path.join(self.tmp.name, "none.json"),
                     "--plan", os.path.join(self.tmp.name, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "AAPL", "KO", "RELIANCE", "--hours", "24", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(data["items"]), 11)  # 6 Apple + 4 Coca-Cola headlines + Reliance's board meeting
        self.assertEqual(sum(1 for i in data["items"] if i["source"] == "NSE"), 1)
        self.assertIn("NSE filings skipped for AAPL, KO", " ".join(data["notes"]))   # no NSE names for them
        code, out, _ = run(["--tickers", "AAPL", "KO", "RELIANCE", "--hours", "24"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_filings_show_in_a_wider_window(self):
        code, out, err = run(["--tickers", "INFY", "--hours", "2400"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("! 2026-07-16 16:15 IST  INFY", out)     # the results filing, NSE's own IST time
        self.assertIn("NSE financial results", out)
        self.assertIn("tags: earnings", out)
        self.assertIn("  2026-10-13 17:30 IST  INFY", out)     # the analyst meet, not flagged
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)

    def test_no_filings_and_a_bad_feed_name(self):
        code, out, _ = run(["--tickers", "RELIANCE", "--hours", "24", "--no-filings", "--json"] + self.base)
        self.assertEqual(json.loads(out)["items"], [])
        code, _, err = run(["--tickers", "RELIANCE", "--feeds", "sec"] + self.base)
        self.assertEqual(code, 2)
        self.assertIn("--feeds knows", err)

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "KO", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "M&M"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["KO", "M&M"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        args[args.index("--plan") + 1] = plan
        code, out, err = run(["--json", "--dry-run", "--hours", "24"] + args)
        self.assertEqual(code, 0, err)
        tickers = {t for i in json.loads(out)["items"] for t in i["touches"]}
        self.assertEqual(tickers, {"KO", "M&M"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_tickers_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err)

    def test_the_reel_tsla_morning(self):
        # Yahoo Finance's real TSLA headlines of Monday Oct 19 2026 (absolute UTC times), read in IST:
        # checked at 14:15 UTC = 19:45 IST
        code, out, err = run(["--tickers", "TSLA", "--hours", "6", "--no-filings", "--no-moves",
                              "--now", "2026-10-19T19:45:00+05:30"] + self.base[:4])
        self.assertEqual(code, 0, err)
        self.assertIn("2026-10-19 17:30 IST  TSLA", out)       # 12:00 UTC, the oldest in the feed
        self.assertNotIn("20:02 IST", out)                      # 14:32 UTC: after the pinned clock
        self.assertIn("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", out)
        merger = [l for l in out.splitlines() if "Merger Mania" in l]
        tags = out.splitlines()[out.splitlines().index(merger[0]) + 1]
        self.assertIn("earnings", tags)
        self.assertIn("deal", tags)
        self.assertIn("Checked 2026-10-19 19:45 IST", out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("  2026-10-19")]), 16)  # 18 in the feed, 2 later
        # the price line, from the chart retimed to 10:37 IST
        code, out, err = run(["--tickers", "TSLA", "--hours", "6", "--no-filings", "--dry-run",
                              "--now", "2026-10-19T10:40:00+05:30"] + self.base[:4])
        self.assertRegex(out, r"TSLA\s+366\.4[78] \+3\.31% vs yesterday's close 354\.72 at 10:37 IST  MOVING")

    def test_moves(self):
        m = news.parse_chart(raw("chart-TSLA.json"), "TSLA")
        self.assertEqual(m["prev_close"], 354.72)
        self.assertEqual(m["pct"], 3.31)
        self.assertEqual(news.to_ist(m["time"]).strftime("%H:%M"), "10:37")
        quiet = dict(m, pct=1.2)
        self.assertNotIn("MOVING", news.render_moves([quiet])[0])
        self.assertIn("MOVING", news.render_moves([quiet], move_pct=1.0)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        # no chart file offline: no move line, no error note
        code, out, _ = run(["--tickers", "KO", "--hours", "24", "--dry-run", "--no-filings"] + self.base)
        self.assertNotIn("vs yesterday's close", out)
        self.assertNotIn("note:", out)

    def test_india_time_has_no_daylight_saving(self):
        utc = datetime.datetime(2026, 1, 15, 4, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(news.to_ist(utc).strftime("%H:%M"), "10:00")
        utc = datetime.datetime(2026, 10, 19, 4, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(news.to_ist(utc).strftime("%H:%M"), "10:00")

    def test_day_trading_tags(self):
        items = [{"id": "x", "source": "Yahoo Finance", "kind": "headline", "ticker": "TSLA",
                  "title": "Trading halted in XYZ after SEBI order; futures rise before the RBI repo rate call",
                  "summary": "", "link": "", "time": None}]
        tags = news.tag(items, {"TSLA": None})[0]["tags"]
        for t in ("halt", "regulatory", "macro", "legal"):
            self.assertIn(t, tags)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-07-31T14:15:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


if __name__ == "__main__":
    unittest.main()
