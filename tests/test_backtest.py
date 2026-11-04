"""trader-backtest/backtest.py: the opening-range breakout replayed over past sessions."""

import argparse
import csv
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

bt = load("trader-backtest", "backtest")
TSLA = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")   # 60 days of real 5-minute bars
CFG = dict(bt.DEFAULTS)
RULES = {"or_minutes": 15, "stop": "or-mid", "target_r": 2.0, "slippage": 0.0, "min_volume_x": None, "prime": False}
DAY = datetime.date(2026, 10, 21)


def bars(rows):
    """[(HH:MM, o, h, l, c), ...] -> the simulator's bars on DAY."""
    out = []
    for hm, o, h, l, c in rows:
        t = datetime.datetime.combine(DAY, datetime.time(*map(int, hm.split(":"))))
        ts = int((t - bt.IST).replace(tzinfo=datetime.timezone.utc).timestamp())
        out.append({"t": ts, "m": t.hour * 60 + t.minute, "o": o, "h": h, "l": l, "c": c, "v": 1000})
    return out


# a range of 100 to 110 (midpoint 105), then a close above 110 on the 9:30 bar (closes 9:35)
OPENING = [("09:15", 104, 110, 100, 106), ("09:20", 106, 108, 103, 107), ("09:25", 107, 109, 104, 108),
           ("09:30", 108, 111, 107, 110.5)]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = bt.main(argv)
    return code, out.getvalue(), err.getvalue()


class Rules(unittest.TestCase):
    def sim(self, after, **cfg):
        return bt.simulate_day("X", DAY, bars(OPENING + after), dict(CFG, **cfg), RULES)

    def test_the_target(self):
        t = self.sim([("09:35", 110.5, 112, 110, 111), ("09:40", 111, 122, 111, 121)])
        # entry 110.50, stop 105.00 (the midpoint), risk 5.50, target 110.50 + 11 = 121.50
        self.assertEqual((t["entry"], t["stop"], t["target"], t["time"]), (110.5, 105.0, 121.5, "09:35"))
        self.assertEqual(t["qty"], 90)                         # ₹500 / 5.50 = 90 shares
        self.assertEqual(t["outcome"], "target")
        self.assertEqual((t["exit"], t["gross"], t["r"]), (121.5, 990.0, 2.0))
        self.assertLess(t["net"], t["gross"])                  # charges come off

    def test_the_stop_and_a_bar_that_touches_both_counts_as_the_stop(self):
        t = self.sim([("09:35", 110.5, 112, 109, 111), ("09:40", 111, 125, 104, 120)])
        self.assertEqual((t["outcome"], t["exit"], t["r"]), ("stop", 105.0, -1.0))

    def test_a_gap_fills_at_the_open(self):
        t = self.sim([("09:35", 110.5, 111, 110, 110.6), ("09:40", 101, 102, 100, 101)])
        self.assertEqual((t["outcome"], t["exit"]), ("stop", 101))
        self.assertLess(t["r"], -1)                            # worse than the plan: a gap

    def test_flat_by_closes_it(self):
        drift = [("%02d:%02d" % divmod(575 + 5 * i, 60), 110.6, 111, 110.2, 110.8) for i in range(80)]
        t = self.sim([b for b in drift if b[0] < "15:30"])
        self.assertEqual((t["outcome"], t["exit_time"], t["exit"]), ("flat", "15:00", 110.8))

    def test_no_breakout_or_too_late(self):
        quiet = bt.simulate_day("X", DAY, bars(OPENING[:3] + [("09:30", 108, 110, 107, 109), ("09:35", 109, 110, 108,
                                                                                              109)]), CFG, RULES)
        self.assertEqual(quiet, {"skip": "no breakout"})
        late = bt.simulate_day("X", DAY, bars(OPENING[:3] + [("14:45", 108, 112, 107, 111.5)]), CFG, RULES)
        self.assertEqual(late, {"skip": "no breakout"})        # closes 14:50, after the 14:45 cutoff
        self.assertEqual(bt.simulate_day("X", DAY, bars(OPENING[1:]), CFG, RULES),
                         {"skip": "incomplete opening range"})

    def test_slippage_is_against_you(self):
        r = dict(RULES, slippage=0.001)
        t = bt.simulate_day("X", DAY, bars(OPENING + [("09:35", 110.5, 112, 104, 111)]), CFG, r)
        self.assertEqual(t["entry"], 110.61)
        self.assertLess(t["exit"], t["stop"])

    def test_charges_match_fyers_price_list(self):
        c = bt.charges(30000, 30300)
        # brokerage 0.03% (₹9 + ₹9.09) under the ₹20 cap; STT 0.025% of the sell; stamp 0.003% of the buy
        self.assertEqual((c["brokerage"], c["stt"], c["stamp"]), (18.09, 7.58, 0.9))
        self.assertEqual(c["exchange"], round(60300 * 0.000030699, 2))
        self.assertAlmostEqual(c["total"], 18.09 + 7.575 + 1.851 + 0.06 + 0.9 + 0.18 * (18.09 + 1.851 + 0.0603),
                               delta=0.02)
        self.assertEqual(bt.charges(200000, 200000)["brokerage"], 40.0)          # capped at ₹20 an order
        self.assertEqual(bt.charges(30000, 30300, prime=True)["brokerage"], 30.0)  # Prime: ₹15 an order

    def test_streaks(self):
        self.assertEqual(bt.streaks([5, -1, -2, -3, 4, 4, -1]), (3, 2))
        self.assertEqual(bt.streaks([]), (0, 0))


class TheDayLimits(unittest.TestCase):
    def test_three_a_day_earliest_first_and_the_daily_loss(self):
        day = [{"symbol": sym, "date": DAY.isoformat(), "max_loss": 500.0, "_m": m}
               for sym, m in (("A", 600), ("B", 580), ("C", 620), ("D", 590))]
        trades, over = bt.pick_trades({DAY: day}, CFG)
        self.assertEqual([t["symbol"] for t in trades], ["B", "D", "A"])     # 9:40, 9:50, 10:00
        self.assertEqual(dict(over), {"max_trades_per_day": 1})
        self.assertNotIn("_m", trades[0])
        trades, over = bt.pick_trades({DAY: day}, dict(CFG, max_daily_loss_inr=1200))
        self.assertEqual(len(trades), 2)                                     # the 3rd ₹500 would pass ₹1,200
        self.assertEqual(dict(over), {"max_daily_loss_inr": 2})


class OnRealBars(unittest.TestCase):
    """60 days of real TSLA 5-minute bars (on the NSE clock): the Time Machine's history check, again."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_the_sixty_days(self):
        code, text, _ = run(["run", "--symbols", "TSLA", "--offline", TSLA, "--to", "2026-10-19", "--days", "90",
                             "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        s = doc["summary"]
        self.assertEqual(doc["sessions"], 58)
        self.assertEqual(s["trades"], 33)
        self.assertEqual(s["outcomes"], {"flat": 13, "stop": 17, "target": 3})
        self.assertAlmostEqual(s["avg_r"], -0.19, places=2)    # trader-timemachine's history: -0.193R
        self.assertLess(s["avg_r_net"], s["avg_r"])
        self.assertEqual(s["worst_losing_streak"], 7)
        self.assertEqual(s["net"], round(s["gross"] - s["charges"], 2))
        self.assertEqual(sum(m["trades"] for m in s["months"].values()), 33)
        with open(os.path.join("trader", "backtest-trades.csv")) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 33)
        code, text, _ = run(["show"])
        self.assertIn("Worst losing run  7 trades in a row", text)
        self.assertIn("Average R", text)

    def test_every_trade_is_scored_and_checked(self):
        code, text, _ = run(["run", "--symbols", "TSLA", "--offline", TSLA, "--to", "2026-10-19", "--json", "--no-write"])
        doc = json.loads(text)
        self.assertEqual(doc["summary"]["trades"], 33)                 # the same trades as without a score
        self.assertTrue(all(0 <= t["score"] <= 100 for t in doc["trades"]))
        sc = doc["summary"]["score_check"]
        self.assertEqual(sum(b["trades"] for b in sc["bands"]), 33)
        self.assertTrue(-1 <= sc["correlation"] <= 1)
        self.assertIn("higher-scored half", sc["verdict"])
        code, text, _ = run(["run", "--symbols", "TSLA", "--offline", TSLA, "--to", "2026-10-19", "--no-write"])
        self.assertIn("Score check", text)

    def test_the_score_check_verdicts(self):
        good = [{"score": 40 + i, "r_net": -1 + i / 20.0, "net": i - 20} for i in range(40)]
        self.assertTrue(bt.score_check(good)["verdict"].startswith("yes"))
        bad = [{"score": 40 + i, "r_net": 1 - i / 20.0, "net": 20 - i} for i in range(40)]
        self.assertTrue(bt.score_check(bad)["verdict"].startswith("no"))
        self.assertIn("too few trades", bt.score_check(good[:10])["verdict"])
        self.assertIsNone(bt.score_check([{"score": None, "r_net": 1, "net": 1}]))

    def test_settings_change_the_result(self):
        _, a, _ = run(["run", "--symbols", "TSLA", "--offline", TSLA, "--to", "2026-10-19", "--json", "--no-write"])
        _, b, _ = run(["run", "--symbols", "TSLA", "--offline", TSLA, "--to", "2026-10-19", "--json", "--no-write",
                       "--target", "1R"])
        ta, tb = json.loads(a)["summary"]["outcomes"], json.loads(b)["summary"]["outcomes"]
        self.assertGreater(tb.get("target", 0), ta.get("target", 0))   # a closer target is hit more often

    def test_bad_input(self):
        self.assertIn("2R or 1.5R", run(["run", "--symbols", "TSLA", "--target", "two"])[2])
        self.assertIn("which symbols", run(["run"])[2])
        self.assertIn("no backtest yet", run(["show"])[2])


class FromFyers(unittest.TestCase):
    """FYERS swapped for a fake: the bars are cached and only new days are asked for again."""

    def setUp(self):
        from test_fyers import FakeFyers, CANDLES
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.makedirs(os.path.join("trader", "state"))
        self.fy = bt.fyers_module.__globals__["sys"].modules.get("fyers_api")
        if self.fy is None:
            import importlib
            bt.fyers_module()                       # puts trader-broker on the path
            self.fy = importlib.import_module("fyers_api")
        with open(self.fy.CONFIG, "w") as fh:
            json.dump({"app_id": "ABCD1234-100"}, fh)
        with open(self.fy.TOKEN, "w") as fh:
            json.dump({"access_token": "tok", "day": self.fy.token_day(self.fy.ist_now())}, fh)
        self.fake = FakeFyers()
        self.fy._urlopen = self.fake
        self.candles = CANDLES

    def tearDown(self):
        self.fy._urlopen = self.fy.urllib.request.urlopen
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_cache(self):
        a = argparse.Namespace(offline=None, cache=os.path.join("trader", "backtest-cache"), refresh=False)
        notes = []
        start, end = datetime.date(2026, 1, 1), datetime.date(2026, 10, 21)
        c, src = bt.load_candles("RELIANCE", start, end, a, notes)
        self.assertEqual((src, len(c)), ("fyers", 5))
        first = len([x for x in self.fake.calls if "/data/history" in x[1]])
        self.assertEqual(first, 3)                                  # 294 days: three 100-day requests
        c, src = bt.load_candles("RELIANCE", start, end, a, notes)
        again = [x for x in self.fake.calls if "/data/history" in x[1]][first:]
        self.assertEqual(len(again), 1)                             # only the last cached day again
        self.assertIn("range_from=2026-10-21", again[0][1])
        self.assertEqual(len(c), 5)


if __name__ == "__main__":
    unittest.main()
