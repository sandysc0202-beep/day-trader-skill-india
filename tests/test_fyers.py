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
        slept = []
        fy.SLEEP = slept.append
        for _ in range(46):
            fy._pace()
        self.assertTrue(slept)                      # 4 a second at most
        self.assertLessEqual(len(fy._calls), 46)


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

    def test_the_tools_switch_when_you_are_logged_in(self):
        self.assertIsNotNone(entry.fyers_bridge())
        os.environ["TRADER_DATA_SOURCE"] = "yahoo"
        self.assertIsNone(entry.fyers_bridge())


if __name__ == "__main__":
    unittest.main()
