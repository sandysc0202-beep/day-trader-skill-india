"""trader-broker/fyers_api.py: FYERS API v3 with the network swapped for canned answers."""

import datetime
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.parse

from _load import load

fy = load("trader-broker", "fyers_api")
entry = load("trader-entry", "entry")
scan = load("trader-scanner", "scan")

# 2026-10-21, 5-minute bars from 9:15 IST, plus the day before (epochs are bar starts, UTC)
D1 = int(datetime.datetime(2026, 10, 20, 3, 45, tzinfo=datetime.timezone.utc).timestamp())
D2 = int(datetime.datetime(2026, 10, 21, 3, 45, tzinfo=datetime.timezone.utc).timestamp())
CANDLES = [[D1, 1400, 1405, 1398, 1402, 1000], [D1 + 300, 1402, 1404, 1401, 1403, 900],
           [D2, 1410, 1416, 1409, 1415, 5000], [D2 + 300, 1415, 1420, 1414, 1419, 4000],
           [D2 + 600, 1419, 1421, 1417, 1418.5, 3000]]
QUOTE = {"code": 200, "s": "ok", "d": [{"n": "NSE:RELIANCE-EQ", "s": "ok", "v": {
    "lp": 1418.5, "prev_close_price": 1403.0, "ch": 15.5, "chp": 1.1, "open_price": 1410, "high_price": 1421,
    "low_price": 1409, "volume": 12000, "tt": str(D2 + 660)}}]}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeFyers:
    """Answers history and quotes like FYERS, and remembers each request."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=20):
        url = req.full_url
        self.calls.append((req.get_method(), url, dict(req.header_items())))
        if "/data/history" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            a = datetime.date.fromisoformat(q["range_from"][0])
            b = datetime.date.fromisoformat(q["range_to"][0])
            day = lambda t: (datetime.datetime.fromtimestamp(t, datetime.timezone.utc) + fy.IST).date()
            body = {"s": "ok", "code": 200, "candles": [c for c in CANDLES if a <= day(c[0]) <= b]}
        elif "/data/quotes" in url:
            body = QUOTE
        elif "/orders/sync" in url:
            body = {"s": "ok", "code": 1101, "message": "Order submitted successfully", "id": "26102100001"}
        else:
            body = {"s": "error", "code": -16, "message": "Could not authenticate the user"}
        return Resp(json.dumps(body).encode())


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        self.env = {k: os.environ.get(k) for k in ("TRADER_DATA_SOURCE", "FYERS_SECRET", "FYERS_APP_ID")}
        for k in self.env:
            os.environ.pop(k, None)
        self.fake = FakeFyers()
        fy._urlopen = self.fake
        fy.SLEEP = lambda s: None
        del fy._calls[:]
        os.makedirs(os.path.join("trader", "state"))
        with open(fy.CONFIG, "w") as fh:
            json.dump({"app_id": "ABCD1234-100", "redirect_uri": "http://127.0.0.1:8765/fyers/callback"}, fh)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        fy._urlopen = fy.urllib.request.urlopen

    def token(self, day="2026-10-21"):
        with open(fy.TOKEN, "w") as fh:
            json.dump({"access_token": "tok123", "day": day}, fh)


class Basics(Base):
    def test_symbols(self):
        self.assertEqual(fy.fyers_symbol("reliance"), "NSE:RELIANCE-EQ")
        self.assertEqual(fy.fyers_symbol("M&M.NS"), "NSE:M&M-EQ")
        self.assertEqual(fy.fyers_symbol("NSE:INFY-EQ"), "NSE:INFY-EQ")
        self.assertEqual(fy.plain_symbol("NSE:BAJAJ-AUTO-EQ"), "BAJAJ-AUTO")

    def test_the_token_lives_until_6_30_am(self):
        self.token("2026-10-21")
        at = lambda s: datetime.datetime.fromisoformat(s) - fy.IST     # IST -> naive UTC
        self.assertTrue(fy.load_token(now_utc=at("2026-10-21 09:00")))
        self.assertTrue(fy.load_token(now_utc=at("2026-10-22 06:29")))
        self.assertIsNone(fy.load_token(now_utc=at("2026-10-22 06:30")))

    def test_data_source(self):
        self.assertEqual(fy.data_source(), "yahoo")
        self.token(fy.token_day(fy.ist_now()))
        self.assertEqual(fy.data_source(), "fyers")
        os.environ["TRADER_DATA_SOURCE"] = "yahoo"
        self.assertEqual(fy.data_source(), "yahoo")

    def test_login_link_and_auth_code(self):
        url = fy.login_url()
        self.assertTrue(url.startswith("https://api-t1.fyers.in/api/v3/generate-authcode?"))
        self.assertIn("client_id=ABCD1234-100", url)
        self.assertIn("response_type=code", url)
        self.assertEqual(fy.auth_code_from("http://127.0.0.1:8765/fyers/callback?s=ok&code=200&auth_code=eyJ.x.y"
                                           "&state=daytrader"), "eyJ.x.y")
        self.assertEqual(fy.auth_code_from(" eyJ.a.b "), "eyJ.a.b")
        with self.assertRaises(fy.FyersError):
            fy.auth_code_from("http://127.0.0.1:8765/fyers/callback?s=error")
        self.assertEqual(fy.app_id_hash("a", "b"), hashlib.sha256(b"a:b").hexdigest())

    def test_no_secret_no_token(self):
        with self.assertRaises(fy.FyersError) as e:
            fy.exchange_code("abc")
        self.assertIn("never in the chat", str(e.exception))

    def test_order_body(self):
        body = fy.order_payload({"symbol": "RELIANCE", "side": "buy", "qty": "8", "type": "market",
                                 "time_in_force": "day", "product": "INTRADAY", "take_profit_limit_price": "1450"})
        self.assertEqual(body, {"symbol": "NSE:RELIANCE-EQ", "qty": 8, "type": 2, "side": 1,
                                "productType": "INTRADAY", "limitPrice": 0, "stopPrice": 0, "validity": "DAY",
                                "disclosedQty": 0, "offlineOrder": False, "orderTag": "daytrader"})
        for bad in ({"type": "trailing_stop"}, {"product": "MIS"}, {"qty": "2.5"}, {"time_in_force": "gtc"}):
            o = dict({"symbol": "X", "side": "buy", "qty": "1", "type": "market"}, **bad)
            with self.assertRaises(fy.FyersError):
                fy.order_payload(o)

    def test_errors_carry_fyers_words(self):
        self.token(fy.token_day(fy.ist_now()))
        with self.assertRaises(fy.FyersError) as e:
            fy.positions()
        self.assertIn("token is invalid or expired", str(e.exception))
        self.assertEqual(self.fake.calls[0][2]["Authorization"], "ABCD1234-100:tok123")

    def test_pacing_stays_under_the_free_plan(self):
        now = 1000.0
        self.assertEqual(fy._wait([now - 0.5] * 3, now)[0], 0)                 # a 4th call this second: fine
        self.assertGreater(fy._wait([now - 0.5] * 4, now)[0], 0.4)             # a 5th: wait
        minute = [now - 59 + i for i in range(45)]
        self.assertGreater(fy._wait(minute, now)[0], 0.9)                       # the 46th in a minute: wait
        self.assertEqual(fy._wait([now - 61] * 45, now), (0, []))              # older than a minute: forgotten

    def test_the_count_is_shared_by_every_tool(self):
        """Two programs (the dashboard and the scanner it runs) share one count, in a file."""
        slept = []
        fy.SLEEP = slept.append
        with open(fy.PACE_FILE, "w") as fh:                  # another tool just made 45 calls
            json.dump([fy.time.time() - 1] * 45, fh)
        fy._pace()
        self.assertTrue(slept and slept[0] > 50)            # so this one waits out the minute
        with open(fy.PACE_FILE) as fh:
            doc = json.load(fh)
        self.assertEqual((len(doc["calls"]), doc["count"], doc["plan"]), (46, 1, "standard"))

    def prime(self):
        with open(fy.APP_CONFIG, "w") as fh:
            json.dump({"fyers_plan": "prime"}, fh)

    def test_the_plan_sets_the_pace(self):
        now = 1000.0
        self.assertEqual(fy.plan_name(), "standard")
        self.assertGreater(fy._wait([now - 30] * 45, now)[0], 0)              # standard: 45 a minute
        self.prime()
        self.assertEqual(fy.plan_name(), "prime")
        self.assertEqual(fy._wait([now - 30] * 449, now)[0], 0)               # Prime: up to 450 a minute
        self.assertGreater(fy._wait([now - 30] * 450, now)[0], 0)
        self.assertGreater(fy._wait([now - 0.5] * 9, now)[0], 0)              # and 9 a second
        os.environ["FYERS_PLAN"] = "standard"
        try:
            self.assertEqual(fy.plan_name(), "standard")                      # the environment wins
        finally:
            del os.environ["FYERS_PLAN"]
        with open(fy.APP_CONFIG, "w") as fh:
            json.dump({"fyers_plan": "gold"}, fh)
        self.assertEqual(fy.plan_name(), "standard")                          # unknown: the safe one

    def test_the_daily_limit_stops_fyers_for_the_day(self):
        day = fy.ist_now().date().isoformat()
        with open(fy.PACE_FILE, "w") as fh:
            json.dump({"day": day, "count": 4800, "calls": []}, fh)
        with self.assertRaises(fy.FyersError) as e:
            fy._pace()
        self.assertIn("Yahoo until tomorrow", str(e.exception))
        with open(fy.PACE_FILE, "w") as fh:                                   # yesterday's count is gone
            json.dump({"day": "2000-01-01", "count": 4800, "calls": []}, fh)
        fy._pace()


class Bridge(Base):
    """FYERS' bars in Yahoo's shape: the other tools parse them unchanged."""

    def setUp(self):
        Base.setUp(self)
        self.token(fy.token_day(fy.ist_now()))
        self.now = datetime.datetime(2026, 10, 21, 4, 30)        # 10:00 IST

    def test_chart_matches_yahoos_shape(self):
        doc = json.loads(fy.yahoo_compat("/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m", self.now))
        r = doc["chart"]["result"][0]
        self.assertEqual(r["timestamp"], [D2, D2 + 300, D2 + 600])           # range=1d: the last session only
        self.assertEqual(r["indicators"]["quote"][0]["close"], [1415, 1419, 1418.5])
        self.assertEqual((r["meta"]["symbol"], r["meta"]["regularMarketPrice"], r["meta"]["chartPreviousClose"]),
                         ("RELIANCE.NS", 1418.5, 1403.0))
        hist = [c for c in self.fake.calls if "/data/history" in c[1]][0][1]
        self.assertIn("symbol=NSE%3ARELIANCE-EQ", hist)
        self.assertIn("resolution=5", hist)
        two = json.loads(fy.yahoo_compat("/v8/finance/chart/RELIANCE.NS?range=5d&interval=5m", self.now))
        self.assertEqual(len(two["chart"]["result"][0]["timestamp"]), 5)

    def test_the_entry_tool_reads_it(self):
        text = fy.yahoo_compat(entry.chart_path("RELIANCE", "1d", "5m"), self.now)
        chart = entry.parse_chart(text, "RELIANCE")
        self.assertEqual(len(chart["bars"]), 3)
        self.assertEqual(chart["bars"][0]["c"], 1415)

    def test_spark_for_a_watchlist_and_not_the_market(self):
        doc = json.loads(fy.yahoo_compat(scan.spark_path(["RELIANCE", "INFY"], "1d", "5m"), self.now))
        self.assertEqual([r["symbol"] for r in doc["spark"]["result"]], ["RELIANCE.NS", "INFY.NS"])
        with self.assertRaises(fy.FyersError) as e:
            fy.yahoo_compat(scan.spark_path(["S%d" % i for i in range(41)], "1d", "5m"), self.now)
        self.assertIn("50 data calls a minute", str(e.exception))

    def test_long_ranges_are_split_at_100_days(self):
        fy.history("RELIANCE", "5", datetime.date(2026, 1, 1), datetime.date(2026, 6, 30))
        self.assertEqual(len([c for c in self.fake.calls if "/data/history" in c[1]]), 2)

    def test_prime_quotes_go_1000_at_a_time(self):
        with open(fy.APP_CONFIG, "w") as fh:
            json.dump({"fyers_plan": "prime"}, fh)
        fy.quotes(["S%d" % i for i in range(1500)])
        q = [c for c in self.fake.calls if "/data/quotes" in c[1]]
        self.assertEqual([m for m, _, _ in q], ["POST", "POST"])               # 1,000 + 500
        fy.quotes(["RELIANCE"])
        os.remove(fy.APP_CONFIG)
        del self.fake.calls[:]
        fy.quotes(["S%d" % i for i in range(120)])
        self.assertEqual(len([c for c in self.fake.calls if "/data/quotes" in c[1]]), 3)   # standard: 50 a GET

    def test_a_refused_bulk_request_falls_back_to_normal_quotes(self):
        """Some Prime accounts get HTTP 403 for the bulk POST: GET requests of 50, and not again today."""
        with open(fy.APP_CONFIG, "w") as fh:
            json.dump({"fyers_plan": "prime"}, fh)
        real = self.fake

        def no_post(req, timeout=20):
            if req.get_method() == "POST":
                real.calls.append(("POST", req.full_url, {}))
                raise fy.urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"<html>403</html>"))
            return real(req, timeout)
        fy._urlopen = no_post
        del fy._bulk_refused[:]
        try:
            q = fy.quotes(["RELIANCE"] + ["S%d" % i for i in range(59)])
            self.assertIn("RELIANCE", q)                                     # still FYERS' price
            methods = [m for m, u, _ in self.fake.calls if "/data/quotes" in u]
            self.assertEqual(methods, ["POST", "GET", "GET"])                # 60 symbols: two GETs of up to 50
            self.assertFalse(fy.bulk_quotes_ok())
            with open(fy.QUOTES_MODE) as fh:
                self.assertIn("403", json.load(fh)["why"])
            del fy._bulk_refused[:]
            self.assertFalse(fy.bulk_quotes_ok())                            # remembered for every tool today
            del self.fake.calls[:]
            fy.quotes(["RELIANCE"])
            self.assertEqual([m for m, u, _ in self.fake.calls if "/data/quotes" in u], ["GET"])
        finally:
            del fy._bulk_refused[:]

    def test_fyers_only_never_uses_yahoo(self):
        bridge = entry.fyers_bridge()
        with open(fy.APP_CONFIG, "w") as fh:
            json.dump({"fyers_only": True}, fh)
        self.assertTrue(bridge.fyers_only())
        os.remove(fy.TOKEN)                                                  # not logged in today
        self.assertEqual(bridge.data_source(), "fyers")                      # still FYERS: no quiet Yahoo
        yahoo = []
        real_open = entry._open_url
        entry._open_url = lambda url, timeout=20: yahoo.append(url) or "{}"
        try:
            with self.assertRaises(entry.EntryError) as e:
                entry.yahoo_get(entry.chart_path("RELIANCE", "1d", "5m"))
        finally:
            entry._open_url = real_open
        self.assertIn("FYERS only is on", str(e.exception))
        self.assertIn("not logged in to FYERS", str(e.exception))
        self.assertEqual(yahoo, [])

        class A:
            offline, json, quiet = None, True, True
        self.token(fy.token_day(fy.ist_now()))
        with self.assertRaises(scan.ScanError) as e:                         # standard plan: no Yahoo market scan
            scan.pick_source(["S%d" % i for i in range(2000)], A)
        self.assertIn("switch the plan to Prime", str(e.exception))

    def test_charts_run_several_at_a_time(self):
        import threading, time as _t
        live, peak, lock = [0], [0], threading.Lock()
        real = self.fake

        def slow(req, timeout=20):
            if "/data/history" in req.full_url:
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                _t.sleep(0.05)
                with lock:
                    live[0] -= 1
            return real(req, timeout)
        fy._urlopen = slow
        with open(fy.APP_CONFIG, "w") as fh:
            json.dump({"fyers_plan": "prime"}, fh)
        seen = []
        got, failed = fy.charts(["S%d" % i for i in range(24)], "5d", "5m", self.now,
                                progress=lambda n, t: seen.append((n, t)))
        self.assertEqual(len(got), 24)
        self.assertGreater(peak[0], 1)                       # in parallel...
        self.assertLessEqual(peak[0], 8)                     # ...at most 8 at once
        self.assertEqual(seen[-1], (24, 24))
        with open(fy.PACE_FILE) as fh:
            self.assertEqual(json.load(fh)["count"], 24)     # every one counted against the limits

    def test_a_scan_skips_what_cannot_have_broken_out(self):
        class Fy:
            FyersError = fy.FyersError
            asked = []

            def plan(self):
                return fy.PLANS["prime"]

            def quotes(self, syms):
                return {"UP": {"high": 105, "open": 100, "price": 104}, "FLAT": {"high": 100, "open": 100, "price": 99},
                        "CHEAP": {"high": 12, "open": 10, "price": 11}}

            def charts(self, syms, rng, interval, quote_map=None, workers=None, progress=None):
                self.asked.extend(syms)
                return {}, {}

        class A:
            json, quiet, min_price = True, True, 20.0
        f = Fy()
        scan.fyers_orb_bars(f, ["UP", "FLAT", "CHEAP", "NOQUOTE"], "1d", True, A)
        self.assertEqual(f.asked, ["UP", "NOQUOTE"])        # FLAT never rose, CHEAP is under ₹20
        f.asked = []
        scan.fyers_orb_bars(f, ["UP", "FLAT"], "5d", False, A)
        self.assertEqual(f.asked, ["UP", "FLAT"])           # a past day: no live quotes to filter by

    def test_a_scan_asks_for_quotes_once(self):
        fy.yahoo_compat(scan.spark_path(["RELIANCE", "INFY", "TCS"], "1d", "5m"), self.now)
        self.assertEqual(len([c for c in self.fake.calls if "/data/quotes" in c[1]]), 1)
        self.assertEqual(len([c for c in self.fake.calls if "/data/history" in c[1]]), 3)

    def test_prime_scans_the_whole_market_with_fyers(self):
        with open(fy.APP_CONFIG, "w") as fh:
            json.dump({"fyers_plan": "prime"}, fh)

        class A:
            offline, json, quiet = None, True, True
        try:
            self.assertEqual(scan.pick_source(["S%d" % i for i in range(2000)], A), "fyers")
        finally:
            os.environ.pop("TRADER_DATA_SOURCE", None)

    def test_fyers_down_means_yahoo_not_an_error(self):
        bridge = entry.fyers_bridge()                       # the copy of fyers_api the tools import
        self.assertIsNotNone(bridge)

        def down(req, timeout=20):
            raise bridge.urllib.error.URLError("connection reset by peer")
        real_fy, real_open = bridge._urlopen, entry._open_url
        yahoo = []
        bridge._urlopen = down
        entry._open_url = lambda url, timeout=20: yahoo.append(url) or '{"chart": {"result": []}}'
        try:
            text = entry.yahoo_get(entry.chart_path("RELIANCE", "1d", "5m"))
        finally:
            bridge._urlopen, entry._open_url = real_fy, real_open
        self.assertEqual(text, '{"chart": {"result": []}}')
        self.assertIn("query2.finance.yahoo.com", yahoo[0])
        self.assertIn("connection reset by peer", entry._fell_back[-1])

    def test_a_market_scan_goes_to_yahoo_not_fyers(self):
        """A whole-market scan in FYERS would use up the day's 5,000 data calls: it goes to Yahoo."""
        class A:
            offline, json, quiet = None, True, True
        self.assertEqual(scan.pick_source(["S%d" % i for i in range(40)], A), "fyers")
        self.assertEqual(scan.pick_source(["S%d" % i for i in range(2000)], A), "yahoo")
        self.assertEqual(os.environ["TRADER_DATA_SOURCE"], "yahoo")
        self.assertIsNone(scan.fyers_bridge())

    def test_the_tools_switch_when_you_are_logged_in(self):
        self.assertIsNotNone(entry.fyers_bridge())
        os.environ["TRADER_DATA_SOURCE"] = "yahoo"
        self.assertIsNone(entry.fyers_bridge())


if __name__ == "__main__":
    unittest.main()
