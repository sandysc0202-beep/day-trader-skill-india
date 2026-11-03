"""trader-broker/trail.py: the trailing stop, and --trail in the backtester."""

import datetime
import json
import os
import shutil
import tempfile
import unittest

from _load import load

trail = load("trader-broker", "trail")
bt = load("trader-backtest", "backtest")
DAY = datetime.date(2026, 11, 3)
IST = datetime.timedelta(hours=5, minutes=30)


def ts(hhmm):
    t = datetime.datetime.combine(DAY, datetime.time(*map(int, hhmm.split(":")))) - IST
    return int(t.replace(tzinfo=datetime.timezone.utc).timestamp())


def utc(hhmm):
    return datetime.datetime.combine(DAY, datetime.time(*map(int, hhmm.split(":")))) - IST


def bar(hhmm, o, h, l, c):
    return {"t": ts(hhmm), "o": o, "h": h, "l": l, "c": c}


# entry 100, first stop 95: R = 5, so breakeven when the price reaches 105
def rec(tick=None):
    return trail.new_record("X", 100, 95, 110, 10, ts("10:00"), tick)


class Rules(unittest.TestCase):
    def test_breakeven_at_1r_then_each_candle_low_and_never_down(self):
        r = rec(tick=0.05)                                                   # a ₹0.05-tick stock
        self.assertEqual(trail.advance(r, [bar("10:00", 100, 103, 99, 102)], 102, ts("10:05")), [])
        self.assertEqual(r["stop"], 95)
        moves = trail.advance(r, [bar("10:05", 102, 105.2, 101, 105)], 105, ts("10:10"))
        self.assertEqual((r["stop"], r["breakeven"]), (100, True))           # rule 1: breakeven
        self.assertIn("1R reached", moves[0]["why"])
        trail.advance(r, [bar("10:10", 105, 107, 103.37, 106)], 106, ts("10:15"))
        self.assertEqual(r["stop"], 103.35)                                  # rule 2: that candle's low, on the tick
        trail.advance(r, [bar("10:15", 106, 106.5, 101, 102)], 102, ts("10:20"))
        self.assertEqual(r["stop"], 103.35)                                  # rule 3: a lower low never lowers it
        self.assertEqual(r["target"], 110)                                   # rule 4: the target stays

    def test_a_forming_candle_and_candles_before_the_entry_do_not_count(self):
        r = rec()
        trail.advance(r, [bar("09:55", 90, 120, 89, 110)], 101, ts("10:20"))  # before the entry: ignored
        self.assertEqual(r["stop"], 95)
        trail.advance(r, [bar("10:00", 100, 106, 99, 105)], 104, ts("10:04"))  # still forming at 10:04
        self.assertFalse(r["breakeven"])
        trail.advance(r, [bar("10:00", 100, 106, 99, 105)], 104, ts("10:05"))  # closed now
        self.assertTrue(r["breakeven"])

    def test_the_live_price_can_reach_breakeven_between_candles(self):
        r = rec()
        trail.advance(r, [], 105.0, ts("10:02"))
        self.assertEqual((r["stop"], r["breakeven"]), (100, True))

    def test_the_breakeven_candle_low_does_not_trail(self):
        r = rec()
        trail.advance(r, [bar("10:00", 100, 106, 103, 105)], 105, ts("10:05"))
        self.assertEqual(r["stop"], 100)                                     # not 103: trailing starts next candle

    def test_hit(self):
        r = rec()
        self.assertTrue(trail.is_hit(r, 95))
        self.assertFalse(trail.is_hit(r, 95.05))
        self.assertRaises(trail.TrailError, trail.new_record, "X", 100, 101, 110, 1, 0)


class Workdir(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.makedirs(os.path.join("trader", "state"))
        self.cfg = {"budget": 100000, "paper": True, "trailing_stop": True}
        with open(os.path.join("trader", "portfolio.json"), "w") as fh:
            json.dump({"cash": 99000.0, "positions": [{"ticker": "X", "shares": 10.0,
                                                        "lots": [{"id": "X-1", "date": DAY.isoformat(), "shares": 10.0,
                                                                  "cost": 1000.0}]}]}, fh)
        self.orders = [
            {"at": "2026-11-03T10:00:00+05:30", "date": DAY.isoformat(), "status": "paper", "symbol": "X",
             "side": "buy", "kind": "entry", "stop": 95.0, "max_loss": 50.0, "qty": "10"},
            {"at": "2026-11-03T09:59:00+05:30", "date": DAY.isoformat(), "status": "filled", "symbol": "X",
             "side": "buy", "qty": "10", "price": 100.0}]
        self.write_state()
        self.bars = []
        self.price = None
        self.logs = []

    def write_state(self):
        with open(os.path.join("trader", "state", "broker.json"), "w") as fh:
            json.dump({"orders": self.orders}, fh)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def look(self, at, **kw):
        return trail.check(self.cfg, utc(at), lambda s: self.bars, lambda s: self.price, log=self.logs.append, **kw)

    def state(self):
        with open(os.path.join("trader", "state", "broker.json")) as fh:
            return json.load(fh)["orders"]

    def portfolio(self):
        with open(os.path.join("trader", "portfolio.json")) as fh:
            return json.load(fh)


class Paper(Workdir):
    def test_off_by_default_changes_nothing(self):
        self.cfg["trailing_stop"] = False
        self.price = 90
        self.assertEqual(self.look("10:30"), {})
        self.assertEqual(self.portfolio()["positions"][0]["shares"], 10.0)
        self.assertFalse(os.path.exists(trail.STATE))

    def test_the_stop_trails_and_the_paper_exit_is_recorded(self):
        self.bars = [bar("10:00", 100, 105.5, 99, 105), bar("10:05", 105, 108, 104.12, 107)]
        self.price = 107
        doc = self.look("10:10")
        self.assertEqual((doc["X"]["stop"], doc["X"]["breakeven"]), (104.12, True))
        self.price = 104.0                                             # through the trailing stop
        doc = self.look("10:11")
        self.assertEqual(doc["X"]["exited"]["how"], "trailing stop")
        self.assertEqual(self.portfolio()["positions"], [])
        st = self.state()
        self.assertEqual([r["status"] for r in st[-2:]], ["filled", "trail-exit"])
        self.assertEqual((st[-2]["side"], st[-2]["price"]), ("sell", 104.12))
        self.price = 90
        self.look("10:12")                                             # exited once, never twice
        self.assertEqual(len([r for r in self.state() if r["status"] == "trail-exit"]), 1)

    def test_before_breakeven_the_first_stop_is_the_exit(self):
        self.price = 94.9
        doc = self.look("10:20")
        self.assertEqual((doc["X"]["exited"]["how"], doc["X"]["exited"]["price"]), ("stop", 95.0))


class Live(Workdir):
    def setUp(self):
        Workdir.setUp(self)
        self.cfg["paper"] = False
        self.modified, self.book = [], []
        self.ops = {"modify": lambda oid, px: self.modified.append((oid, px)) or {"id": oid},
                    "orderbook": lambda: self.book}

    def test_no_stop_order_at_fyers_means_nothing_to_move(self):
        self.price = 106
        doc = self.look("10:10", live_ops=self.ops)
        self.assertIn("press STOP-LOSS", doc["X"]["note"])
        self.assertEqual(self.modified, [])

    def test_the_fyers_stop_is_moved_up_and_its_fill_recorded(self):
        self.orders.append({"at": "2026-11-03T10:01:00+05:30", "date": DAY.isoformat(), "status": "placed",
                            "symbol": "X", "side": "sell", "type": "stop", "broker_order_id": "SL1", "kind": "exit"})
        self.write_state()
        self.bars = [bar("10:00", 100, 105.5, 99, 105), bar("10:05", 105, 108, 104.12, 107)]
        self.price = 107
        self.look("10:10", live_ops=self.ops)
        self.assertEqual(self.modified, [("SL1", 104.12)])             # FYERS' SL-M now triggers at 104.12
        self.assertIn("stop-moved", [r["status"] for r in self.state()])
        self.book = [{"id": "SL1", "status_word": "filled", "filledQty": 10, "tradedPrice": 104.05}]
        doc = self.look("10:16", live_ops=self.ops)
        self.assertEqual(doc["X"]["exited"]["price"], 104.05)
        self.assertEqual(self.portfolio()["positions"], [])
        self.assertIsNone(trail.ticket.open_stop_id({"orders": self.state()}, "X", DAY.isoformat()))


class Backtest(unittest.TestCase):
    """--trail: the same rules, candle by candle."""

    def bars(self, rows):
        out = []
        for hm, o, h, l, c in rows:
            t = datetime.datetime.combine(DAY, datetime.time(*map(int, hm.split(":"))))
            out.append({"t": ts(hm), "m": t.hour * 60 + t.minute, "o": o, "h": h, "l": l, "c": c, "v": 1000})
        return out

    def test_trail_locks_in_the_gain(self):
        day = [("09:15", 104, 110, 100, 106), ("09:20", 106, 108, 103, 107), ("09:25", 107, 109, 104, 108),
               ("09:30", 108, 111, 107, 110.5),                                       # entry 110.50, stop 105, R 5.50
               ("09:35", 110.5, 116.5, 110, 116), ("09:40", 116, 118, 114.02, 117),    # 1R (116) -> 110.50; then 114.02
               ("09:45", 117, 117.5, 112, 113)]                                       # falls through 114.02
        cfg = dict(bt.DEFAULTS)
        rules = {"or_minutes": 15, "stop": "or-mid", "target_r": 2.0, "slippage": 0.0, "min_volume_x": None,
                 "prime": False}
        plain = bt.simulate_day("X", DAY, self.bars(day), cfg, rules)
        trailed = bt.simulate_day("X", DAY, self.bars(day), cfg, dict(rules, trail=True))
        self.assertEqual(plain["outcome"], "flat")                     # without it: held to the end of the data
        self.assertEqual((trailed["outcome"], trailed["exit"], trailed["final_stop"]), ("trail", 114.02, 114.02))
        self.assertGreater(trailed["r"], 0.6)
        self.assertEqual(trailed["stop"], plain["stop"])               # the planned stop and size do not change
        self.assertEqual(trailed["qty"], plain["qty"])


if __name__ == "__main__":
    unittest.main()
