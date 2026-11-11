"""trader-broker/flat.py: paper day trades closed at flat-by (15:00 IST)."""

import datetime
import json
import os
import shutil
import tempfile
import unittest

from _load import load

flat = load("trader-broker", "flat")
ticket = load("trader-broker", "ticket")
DAY = datetime.date(2026, 11, 9)          # a Monday
IST = datetime.timedelta(hours=5, minutes=30)
CFG = {"paper": True, "budget": 100000, "flat_by": "15:00"}


def utc(hhmm, day=DAY):
    return datetime.datetime.combine(day, datetime.time(*map(int, hhmm.split(":")))) - IST


def ts(hhmm, day=DAY):
    return int(utc(hhmm, day).replace(tzinfo=datetime.timezone.utc).timestamp())


def bars(day=DAY, upto="15:25", start=100.0):
    """5-minute candles from 9:15; each closes 0.5 higher than the last."""
    out, t, c = [], datetime.datetime.combine(day, datetime.time(9, 15)), start
    end = datetime.datetime.combine(day, datetime.time(*map(int, upto.split(":"))))
    while t <= end:
        out.append({"t": ts(t.strftime("%H:%M"), day), "c": c})
        c += 0.5
        t += datetime.timedelta(minutes=5)
    return out


def close_at(hhmm, day=DAY):
    return next(b["c"] for b in bars(day) if b["t"] == ts(hhmm, day))


class Book(unittest.TestCase):
    """A paper buy recorded the way ticket.py records one."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = {"portfolio": os.path.join(self.dir, "portfolio.json"),
                      "state": os.path.join(self.dir, "broker.json")}
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.dir)

    def buy(self, sym="RELIANCE", qty=10, price=100.0, day=DAY, product=None):
        ticket.apply_fill(self.paths["portfolio"], CFG, sym, "buy", qty, price, day.isoformat())
        ticket.log_fill(self.paths["state"], utc("09:40", day), day.isoformat(), sym, "buy", qty, price)
        rec = {"at": "x", "date": day.isoformat(), "status": "paper", "symbol": sym, "side": "buy",
               "kind": "entry", "qty": str(qty), "max_loss": 50}
        if product:
            rec["product"] = product
        ticket._record(self.paths["state"], rec)

    def run_at(self, hhmm, day=DAY, cfg=CFG, the_bars=None, last=123.45):
        got = {"asked": []}

        def bars_for(sym, d):
            got["asked"].append((sym, d))
            return bars(d) if the_bars is None else the_bars
        exits = flat.check(cfg, utc(hhmm, day), bars_for, lambda s: last, paths=self.paths,
                           log=self.logs.append)
        return exits, got["asked"]

    def portfolio(self):
        with open(self.paths["portfolio"], encoding="utf-8") as fh:
            return json.load(fh)

    def orders(self):
        with open(self.paths["state"], encoding="utf-8") as fh:
            return json.load(fh)["orders"]


class FlatBy(Book):
    def test_nothing_happens_before_flat_by(self):
        self.buy()
        exits, asked = self.run_at("14:59")
        self.assertEqual((exits, asked), ([], []))
        self.assertEqual(self.portfolio()["positions"][0]["shares"], 10)

    def test_at_flat_by_it_sells_at_the_candle_that_ends_at_15_00(self):
        self.buy()
        exits, _ = self.run_at("15:00")
        want = close_at("14:55")                                     # the 14:55 candle closes at 15:00
        self.assertEqual(exits, [{"symbol": "RELIANCE", "qty": 10.0, "price": want, "date": "2026-11-09",
                                  "how": "15:00 candle"}])
        self.assertEqual(self.portfolio()["positions"], [])
        self.assertEqual(self.portfolio()["cash"], 100000 - 1000 + 10 * want)
        fill = [o for o in self.orders() if o["status"] == "filled" and o["side"] == "sell"][0]
        self.assertEqual((fill["reason"], fill["price"], fill["date"]), ("flat", want, "2026-11-09"))
        line = [o for o in self.orders() if o["status"] == "flat-exit"][0]
        self.assertIn("paper flat-by 15:00: SELL 10 RELIANCE", line["plain"])

    def test_later_in_the_day_still_uses_the_15_00_price(self):
        self.buy()
        exits, _ = self.run_at("15:22")                              # the dashboard opened late
        self.assertEqual(exits[0]["price"], close_at("14:55"))

    def test_without_the_15_00_candle_it_uses_the_last_price(self):
        self.buy()
        exits, _ = self.run_at("15:05", the_bars=bars(upto="10:40"))  # the data stops early
        self.assertEqual((exits[0]["price"], exits[0]["how"]), (123.45, "last price"))
        exits, _ = self.run_at("15:05")                                # and only once
        self.assertEqual(exits, [])

    def test_a_feed_that_is_only_late_is_waited_for(self):
        self.buy()
        late = bars(upto="14:45")                                     # Yahoo at 15:02: about 15 minutes behind
        exits, _ = self.run_at("15:02", the_bars=late)
        self.assertEqual(exits, [])                                   # not 14:45's price: wait for 15:00's
        self.assertEqual(self.portfolio()["positions"][0]["shares"], 10)
        exits, _ = self.run_at("15:16", the_bars=bars(upto="14:55"))  # the candle arrived
        self.assertEqual((exits[0]["price"], exits[0]["how"]), (close_at("14:55"), "15:00 candle"))

    def test_the_wait_for_a_late_feed_ends_after_30_minutes(self):
        self.buy()
        exits, _ = self.run_at("15:20", the_bars=bars(upto="14:50"))  # newest candle 25 minutes old: late
        self.assertEqual(exits, [])
        exits, _ = self.run_at("15:31", the_bars=bars(upto="14:50"))  # it never came: the last price
        self.assertEqual((exits[0]["price"], exits[0]["how"]), (123.45, "last price"))

    def test_a_position_left_open_from_an_earlier_day_closes_at_that_days_price(self):
        friday = DAY - datetime.timedelta(days=3)
        self.buy(day=friday, price=90)
        exits, asked = self.run_at("08:30")                           # Monday morning, before the open
        self.assertEqual(asked, [("RELIANCE", friday)])
        self.assertEqual((exits[0]["date"], exits[0]["price"]), ("2026-11-06", close_at("14:55", friday)))
        sell = [o for o in self.orders() if o["status"] == "filled" and o["side"] == "sell"][0]
        self.assertEqual((sell["date"], sell["at"][:16]), ("2026-11-06", "2026-11-06T15:00"))   # counts on Friday
        self.assertIn("left open from 2026-11-06", [o for o in self.orders() if o["status"] == "flat-exit"][0]["plain"])

    def test_cnc_and_live_are_never_touched(self):
        self.buy(sym="HDFCBANK", product="CNC")                       # delivery: kept
        self.buy(sym="INFY", product="INTRADAY")
        exits, _ = self.run_at("15:05")
        self.assertEqual([e["symbol"] for e in exits], ["INFY"])
        self.assertEqual([p["ticker"] for p in self.portfolio()["positions"]], ["HDFCBANK"])
        self.buy(sym="TCS")
        exits, asked = self.run_at("15:05", cfg=dict(CFG, paper=False))  # live: FYERS squares off itself
        self.assertEqual((exits, asked), ([], []))

    def test_no_price_at_all_waits_and_tries_again(self):
        self.buy()

        def broken(sym, d):
            raise RuntimeError("FYERS down")
        exits = flat.check(CFG, utc("15:05"), broken, lambda s: None, paths=self.paths, log=self.logs.append)
        self.assertEqual(exits, [])
        self.assertEqual(self.portfolio()["positions"][0]["shares"], 10)
        self.assertTrue(any("trying again" in m for m in self.logs))

    def test_a_flat_by_from_the_config(self):
        self.buy()
        self.assertEqual(self.run_at("14:50", cfg=dict(CFG, flat_by="14:45"))[0][0]["price"], close_at("14:40"))

    def test_the_cli_closes_from_a_terminal(self):
        self.buy()
        cwd = os.getcwd()
        os.makedirs(os.path.join(self.dir, "trader", "state"))
        shutil.move(self.paths["portfolio"], os.path.join(self.dir, "trader", "portfolio.json"))
        shutil.move(self.paths["state"], os.path.join(self.dir, "trader", "state", "broker.json"))
        with open(os.path.join(self.dir, "trader", "config.json"), "w", encoding="utf-8") as fh:
            json.dump(CFG, fh)
        old = flat._bars_and_price
        flat._bars_and_price = lambda sym, day: (bars(day), 150.0)
        try:
            os.chdir(self.dir)
            self.assertEqual(flat.main(["check", "--now", "2026-11-09 15:10"]), 0)
            with open(os.path.join("trader", "portfolio.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["positions"], [])
        finally:
            os.chdir(cwd)
            flat._bars_and_price = old


class Helpers(unittest.TestCase):
    def test_price_at_flat_needs_the_candle_that_ends_then(self):
        self.assertEqual(flat.price_at_flat(bars(), DAY, datetime.time(15, 0)), close_at("14:55"))
        self.assertIsNone(flat.price_at_flat(bars(upto="14:50"), DAY, datetime.time(15, 0)))
        self.assertIsNone(flat.price_at_flat(bars(DAY - datetime.timedelta(days=1)), DAY, datetime.time(15, 0)))
        self.assertIsNone(flat.price_at_flat([], DAY, datetime.time(15, 0)))

    def test_flat_time_defaults_to_15_00(self):
        self.assertEqual(flat.flat_time({}), datetime.time(15, 0))
        self.assertEqual(flat.flat_time({"flat_by": "nonsense"}), datetime.time(15, 0))
        self.assertEqual(flat.flat_time({"flat_by": "14:45"}), datetime.time(14, 45))

    def test_the_reason_is_the_backtests_name(self):
        self.assertEqual(flat.REASON, "flat")
        bt = load("trader-backtest", "backtest")
        with open(bt.__file__, encoding="utf-8") as fh:
            self.assertIn('outcome = exit_bar["c"] * (1 - slip), "flat"', fh.read())


if __name__ == "__main__":
    unittest.main()
