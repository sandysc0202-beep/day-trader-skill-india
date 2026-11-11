#!/usr/bin/env python3
"""
The flat-by close for paper day trades (trader-broker).

A day trade is closed by flat_by (15:00 IST by default). Live, FYERS squares
off INTRADAY positions itself from 3:05 PM. Paper has no broker to do that, so
this does it: every open paper position that is past its flat-by time, on its
own day or left open from an earlier day, is sold at that day's 15:00 price.

  the 15:00 price  the close of the 5-minute candle that ends at flat_by on the
                   day the position was opened (the 14:55 candle for 15:00);
                   when there is no such candle (the data ends earlier, or the
                   day is too old), the last price. A feed that is only late
                   (Yahoo runs about 15 minutes behind; FYERS adds a candle a
                   few seconds after it closes) is waited for, up to 30 minutes
                   past flat-by, so the price is 15:00's and not 14:45's
  the record       the sell in trader/portfolio.json, a fill in the log with
                   reason "flat" (the backtest's name for this exit), and a
                   "flat-exit" line, dated the day the position was opened so
                   it counts on that day, not today
  never            a live account (FYERS squares off; nothing is sent from
                   here), or a CNC (delivery) position

The dashboard runs this every few seconds and when it starts. From a terminal:

  python3 trader-broker/flat.py check          close what is past flat-by now
  python3 trader-broker/flat.py check --now "2026-10-19 15:05"

Standard library only.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket       # noqa: E402  (next to this file)
import fyers_api    # noqa: E402

BAR = 300           # seconds in a 5-minute candle
FRESH = 30 * 60     # candles this recent mean the data is still arriving (a delayed feed)
PATIENCE = 30 * 60  # how long past flat-by to wait for a delayed feed's 15:00 candle
EPOCH = datetime.datetime(1970, 1, 1)
REASON = "flat"     # what trader-backtest calls an exit at flat-by


def flat_time(cfg):
    """flat_by from the config as a time (15:00 by default)."""
    text = str(cfg.get("flat_by") or "15:00")
    try:
        h, m = [int(x) for x in text.split(":")[:2]]
        return datetime.time(h, m)
    except (ValueError, TypeError):
        return datetime.time(15, 0)


def utc_ts(day, hhmm):
    """An India wall-clock time on a day -> a UTC timestamp."""
    ist = datetime.datetime.combine(day, hhmm)
    return int((ist - ticket.IST_OFFSET - EPOCH).total_seconds())


def price_at_flat(bars, day, hhmm):
    """The close of the candle that ends at flat_by on that day, or None. bars: [{t (UTC start), c}]."""
    end = utc_ts(day, hhmm)
    start = utc_ts(day, datetime.time(9, 15))
    best = None
    for b in bars or []:
        t = int(b.get("t") or 0)
        if start <= t and t + BAR <= end and b.get("c") is not None:
            if best is None or t > best["t"]:
                best = b
    # only the candle that actually ends at flat_by counts as "the 15:00 price"
    if best is not None and int(best["t"]) + BAR == end:
        return float(best["c"])
    return None


def still_arriving(bars, now_utc, day, hhmm):
    """True when the flat-by candle is missing only because the feed is behind: the newest
    candle of that day is under 30 minutes old and it is under 30 minutes past flat-by."""
    now = int((now_utc - EPOCH).total_seconds())
    due = utc_ts(day, hhmm)
    if now - due > PATIENCE:
        return False
    start = utc_ts(day, datetime.time(9, 15))
    times = [int(b.get("t") or 0) for b in bars or [] if start <= int(b.get("t") or 0) < due]
    return bool(times) and now - (max(times) + BAR) < FRESH


def product_of(state, sym):
    """INTRADAY or CNC for the latest buy of this symbol (older records have none: INTRADAY)."""
    prod = "INTRADAY"
    for r in state.get("orders", []):
        if str(r.get("symbol", "")).upper() == sym and r.get("side") == "buy" and \
                r.get("status") in ("placed", "paper") and r.get("product"):
            prod = str(r["product"]).upper()
    return prod


def opened_day(pos, today):
    """The day the oldest open lot was bought."""
    days = []
    for lot in pos.get("lots", []):
        try:
            days.append(datetime.date.fromisoformat(str(lot.get("date"))[:10]))
        except ValueError:
            pass
    return min(days) if days else today


def check(cfg, now_utc, bars_for, price_for, paths=None, log=print):
    """One look at every open paper position. bars_for(sym, day) -> that day's 5-minute candles
    (or None), price_for(sym) -> the last price. Returns the exits it recorded."""
    if not cfg.get("paper", True):
        return []
    paths = dict({"portfolio": os.path.join("trader", "portfolio.json"),
                  "state": os.path.join("trader", "state", "broker.json")}, **(paths or {}))
    pf = ticket.read_json(paths["portfolio"], "portfolio", required=False) or {}
    if not pf.get("positions"):
        return []
    state = ticket.load_state(paths["state"])
    hhmm = flat_time(cfg)
    now_ist = ticket.to_ist(now_utc)
    today = now_ist.date()
    exits = []
    for pos in list(pf.get("positions", [])):
        sym = str(pos.get("ticker", "")).upper()
        qty = float(pos.get("shares") or 0)
        if not sym or qty <= 1e-9 or product_of(state, sym) == "CNC":
            continue
        day = opened_day(pos, today)
        due = datetime.datetime.combine(day, hhmm)
        if now_ist < due:
            continue                                # still inside its day: nothing to do yet
        price, how, bars = None, "15:00 candle", None
        try:
            bars = bars_for(sym, day)
            price = price_at_flat(bars, day, hhmm)
        except Exception as e:                      # no bars: fall back to the last price
            log("flat: %s: no %s candle (%s)" % (sym, hhmm.strftime("%H:%M"), e))
        if price is None and still_arriving(bars, now_utc, day, hhmm):
            continue                                # the feed is behind: its 15:00 candle is on the way
        if price is None:
            try:
                price = price_for(sym)
            except Exception as e:
                log("flat: %s: no price yet (%s); trying again shortly" % (sym, e))
                continue
            how = "last price"
        if not price:
            log("flat: %s: no price yet; trying again shortly" % sym)
            continue
        price = round(float(price), 2)
        at = datetime.datetime.combine(day, hhmm) - ticket.IST_OFFSET if day < today else now_utc
        d = day.isoformat()
        ticket.apply_fill(paths["portfolio"], cfg, sym, "sell", qty, price, d)
        ticket.log_fill(paths["state"], at, d, sym, "sell", qty, price, reason=REASON)
        ticket._record(paths["state"], {
            "at": ticket.ist_iso(at), "date": d, "status": "flat-exit", "symbol": sym, "side": "sell",
            "qty": ticket._str_num(qty), "price": price, "paper": True, "reason": REASON,
            "plain": "paper flat-by %s: SELL %s %s at %s (%s%s)" % (
                hhmm.strftime("%H:%M"), ticket._str_num(qty), sym, ticket._p(price), how,
                ", left open from %s" % d if day < today else "")})
        exits.append({"symbol": sym, "qty": qty, "price": price, "date": d, "how": how})
        log("flat: %s paper exit at %.2f (%s, flat-by %s on %s)" % (sym, price, how, hhmm.strftime("%H:%M"), d))
    return exits


def _bars_and_price(sym, day):
    """That day's 5-minute candles and the last price: FYERS after the morning login, else Yahoo."""
    rng = "5d" if (datetime.date.today() - day).days <= 4 else "1mo"
    doc = None
    if fyers_api.data_source() == "fyers":
        try:
            doc = fyers_api.chart_doc(sym, rng, "5m")
        except fyers_api.FyersError as e:
            if fyers_api.fyers_only():
                raise
            sys.stderr.write("FYERS: %s; using Yahoo for this instead.\n" % e)
    if doc is None:
        import urllib.request
        url = "https://query2.finance.yahoo.com/v8/finance/chart/%s.NS?range=%s&interval=5m" % (sym, rng)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                    timeout=20) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    res = doc["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = [{"t": t, "c": c} for t, c in zip(res.get("timestamp") or [], q["close"]) if c is not None]
    price = res["meta"].get("regularMarketPrice") or (bars[-1]["c"] if bars else None)
    return bars, price


def main(argv=None):
    ap = argparse.ArgumentParser(description="Close paper day trades at flat-by (trader-broker).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("check", help="sell every open paper position that is past flat-by")
    p.add_argument("--config", default=os.path.join("trader", "config.json"))
    p.add_argument("--now", help="pretend it is this India time (IST)")
    a = ap.parse_args(argv)
    if a.cmd != "check":
        ap.print_help()
        return 1
    cfg = ticket.read_json(a.config, "config", required=False) or {}
    if not cfg.get("paper", True):
        print("A live account: FYERS squares off INTRADAY positions itself; nothing to do here.")
        return 0
    cache = {}

    def get(sym, day):
        if (sym, day) not in cache:
            cache[(sym, day)] = _bars_and_price(sym, day)
        return cache[(sym, day)]
    exits = check(cfg, ticket.parse_now(a.now), lambda s, d: get(s, d)[0],
                  lambda s: get(s, datetime.date.today())[1])
    if not exits:
        print("No open paper position is past flat-by.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
