#!/usr/bin/env python3
"""The trailing stop for an open day trade (trader-broker). Standard library only.

Off unless "trailing_stop": true is in trader/config.json. The rules, long
trades only:

  1. When the price reaches entry + 1R (R = entry minus the first stop), the
     stop moves up to the entry price: breakeven. A winner cannot turn into a
     loss after that, short of a gap.
  2. After that, at each completed 5-minute candle, the stop rises to that
     candle's low if the low is above the stop (rounded down onto NSE's tick).
     The candle in which 1R was reached does not count; trailing starts with
     the next one.
  3. The stop only ever moves up, never down.
  4. The target stays as it is.

What happens when the stop moves or is hit:

  paper  nothing goes to FYERS. When the price is at or under the stop, the
         exit is recorded as a paper sell at the stop price (ticket.py fill).
  live   the stop-loss order at FYERS (the SL-M from ticket.py protect) is
         moved up to the new stop (FYERS' modify order), so FYERS sells when
         it is hit, even if this computer is asleep. Without that order at
         FYERS there is nothing to move: press STOP-LOSS (protect) first. The
         app never sends a second sell for the same shares (both could fill).
         When FYERS reports the stop filled, the fill is recorded.

    python3 trail.py check            # advance every open position once (the dashboard does this itself)
    python3 trail.py show             # each position's stop now and how it got there

State: trader/state/trail.json, one record a symbol, with every move.
Education and tooling, not financial advice.
"""

import argparse
import datetime
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket       # noqa: E402  (next to this file)
import fyers_api    # noqa: E402

STATE = os.path.join("trader", "state", "trail.json")
BE_R = 1.0          # breakeven at entry + 1R
BAR = 300           # seconds in a 5-minute candle
TICK_BANDS = ((250, 0.01), (1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00))


class TrailError(Exception):
    pass


def enabled(cfg):
    return bool((cfg or {}).get("trailing_stop"))


def nse_tick(price, override=None):
    if override:
        return float(override)
    for top, step in TICK_BANDS:
        if price <= top:
            return step
    return 5.00


def floor_tick(x, tick):
    return round(math.floor(x / tick + 1e-9) * tick, 2)


def hm(ts):
    return (datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).replace(tzinfo=None)
            + datetime.timedelta(hours=5, minutes=30)).strftime("%H:%M")


# ---------------------------------------------------------------- the rules (pure)

def new_record(sym, entry, stop, target, qty, since_ts, tick=None):
    entry, stop = float(entry), float(stop)
    if stop >= entry:
        raise TrailError("%s: the stop %.2f is not under the entry %.2f" % (sym, stop, entry))
    return {"symbol": sym.upper(), "entry": round(entry, 2), "initial_stop": round(stop, 2), "stop": round(stop, 2),
            "target": float(target) if target else None, "qty": float(qty), "r": round(entry - stop, 4),
            "breakeven": False, "since": int(since_ts), "last_bar": int(since_ts) - BAR, "tick": tick,
            "moves": [], "exited": None}


def advance(rec, bars, price, now_ts):
    """Apply the rules to the candles completed since the last look, then the live price.
    bars: [{"t", "o", "h", "l", "c"}] 5-minute candles (start times). Changes rec; returns
    the moves made, [{"from", "to", "why", "at"}]. The stop never goes down."""
    moves = []
    if rec.get("exited"):
        return moves
    one_r = rec["entry"] + BE_R * rec["r"]
    tick = nse_tick(rec["entry"], rec.get("tick"))

    def move(to, why, at):
        to = round(to, 2)
        if to > rec["stop"] + 1e-9:
            moves.append({"from": rec["stop"], "to": to, "why": why, "at": at})
            rec["stop"] = to

    for b in sorted(bars, key=lambda x: x["t"]):
        if b["t"] <= rec["last_bar"] or b["t"] < rec["since"] or b["t"] + BAR > now_ts:
            continue                                  # seen already, before the entry, or still forming
        if not rec["breakeven"]:
            if b["h"] >= one_r - 1e-9:
                rec["breakeven"] = True
                move(rec["entry"], "1R reached (%.2f): stop to breakeven" % one_r, hm(b["t"] + BAR))
        else:
            move(floor_tick(b["l"], tick), "the %s candle's low" % hm(b["t"]), hm(b["t"] + BAR))
        rec["last_bar"] = b["t"]
    if not rec["breakeven"] and price is not None and price >= one_r - 1e-9:
        rec["breakeven"] = True
        move(rec["entry"], "1R reached (%.2f): stop to breakeven" % one_r, hm(now_ts))
    return moves


def is_hit(rec, price):
    return price is not None and not rec.get("exited") and price <= rec["stop"] + 1e-9


# ---------------------------------------------------------------- state

def load(path=STATE):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save(doc, path=STATE):
    ticket.write_json(path, doc)


def entry_facts(sym, portfolio, broker_state, plan, today):
    """(entry, stop, target, qty) for an open position: the average cost and shares from the
    portfolio, the stop from today's approved entry (or the plan for this symbol)."""
    pos = next((p for p in (portfolio or {}).get("positions", [])
                if str(p.get("ticker", "")).upper() == sym), None)
    if not pos or float(pos.get("shares", 0)) <= 0:
        return None
    shares = float(pos["shares"])
    cost = sum(float(l.get("cost", 0)) for l in pos.get("lots", []))
    entry = cost / shares if shares else None
    stop = target = None
    for r in (broker_state or {}).get("orders", []):
        if str(r.get("symbol", "")).upper() == sym and r.get("kind") == "entry" and r.get("stop") and \
                ticket.rec_day(r) == today:
            stop = float(r["stop"])
    if plan and str(plan.get("symbol", "")).upper() == sym:
        stop = stop or plan.get("stop")
        target = plan.get("target")
    if not entry or not stop:
        return None
    return entry, float(stop), target, shares


# ---------------------------------------------------------------- one pass over the open positions

def check(cfg, now_utc, bars_for, price_for, paths=None, live_ops=None, log=print):
    """One look at every open position. bars_for(sym) -> 5-minute candles, price_for(sym) -> last price.
    paths: {"portfolio", "state", "plan", "trail"}. live_ops: {"modify", "orderbook"} (FYERS by default).
    Returns {SYM: record}. Does nothing unless the config turns the trailing stop on."""
    if not enabled(cfg):
        return {}
    paths = dict({"portfolio": os.path.join("trader", "portfolio.json"),
                  "state": os.path.join("trader", "state", "broker.json"),
                  "plan": os.path.join("trader", "plan.json"), "trail": STATE}, **(paths or {}))
    live = not cfg.get("paper", True)
    ops = dict({"modify": fyers_api.modify_stop, "orderbook": fyers_api.orderbook}, **(live_ops or {}))
    now_ts = int((now_utc - datetime.datetime(1970, 1, 1)).total_seconds())
    today = ticket.to_ist(now_utc).date().isoformat()
    pf = ticket.read_json(paths["portfolio"], "portfolio", required=False) or {}
    bstate = ticket.load_state(paths["state"])
    plan = ticket.read_json(paths["plan"], "plan", required=False) or {}
    doc = load(paths["trail"])
    held = set()
    for p in pf.get("positions", []):
        sym = str(p.get("ticker", "")).upper()
        facts = entry_facts(sym, pf, bstate, plan, today)
        if not facts:
            continue
        held.add(sym)
        entry, stop, target, qty = facts
        rec = doc.get(sym)
        if not rec or rec.get("day") != today or rec.get("exited") or abs(rec.get("qty", 0) - qty) > 1e-9:
            try:
                rec = new_record(sym, entry, stop, target, qty, now_ts - (now_ts % BAR), cfg.get("tick_size"))
            except TrailError as e:
                log("trail: %s" % e)
                continue
            rec["day"] = today
            fill_at = _first_fill_ts(bstate, sym, today)
            if fill_at:
                rec["since"] = fill_at - (fill_at % BAR) + BAR      # candles that started after the fill
                rec["last_bar"] = rec["since"] - BAR
            doc[sym] = rec
        try:
            bars = bars_for(sym) or []
            price = price_for(sym)
        except Exception as e:                                       # no data this time: try again next look
            log("trail: %s: no prices (%s)" % (sym, e))
            continue
        moves = advance(rec, bars, price, now_ts)
        for m in moves:
            rec["moves"].append(m)
            log("trail: %s stop %.2f -> %.2f (%s)" % (sym, m["from"], m["to"], m["why"]))
        if live:
            _live_follow(rec, moves, bstate, today, ops, paths, cfg, now_utc, log)
        elif is_hit(rec, price):
            _paper_exit(rec, paths, cfg, now_utc, today, log)
    for sym, rec in doc.items():                                     # closed elsewhere (flatten, a fill)
        if sym not in held and not rec.get("exited") and rec.get("day") == today:
            rec["exited"] = {"how": "closed", "at": ticket.ist_iso(now_utc)}
    save(doc, paths["trail"])
    return doc


def _first_fill_ts(bstate, sym, today):
    for r in bstate.get("orders", []):
        if r.get("status") == "filled" and str(r.get("symbol", "")).upper() == sym and r.get("side") == "buy" \
                and ticket.rec_day(r) == today and r.get("at"):
            try:
                t = datetime.datetime.fromisoformat(str(r["at"]))
                return int(t.timestamp())
            except ValueError:
                return None
    return None


def _paper_exit(rec, paths, cfg, now_utc, today, log):
    """Paper: the stop was hit, so record the sell at the stop, as an SL-M would have filled."""
    px = rec["stop"]
    ticket.apply_fill(paths["portfolio"], cfg, rec["symbol"], "sell", rec["qty"], px, today)
    ticket.log_fill(paths["state"], now_utc, today, rec["symbol"], "sell", rec["qty"], px)
    ticket._record(paths["state"], {
        "at": ticket.ist_iso(now_utc), "date": today, "status": "trail-exit", "symbol": rec["symbol"],
        "side": "sell", "qty": ticket._str_num(rec["qty"]), "price": px, "paper": True,
        "plain": "paper trailing stop: SELL %s %s at %s (%s)" % (
            ticket._str_num(rec["qty"]), rec["symbol"], ticket._p(px),
            "breakeven or better" if rec["breakeven"] else "the first stop")})
    rec["exited"] = {"how": "trailing stop" if rec["stop"] > rec["initial_stop"] else "stop", "price": px,
                     "at": ticket.ist_iso(now_utc)}
    log("trail: %s paper exit at %.2f (%s)" % (rec["symbol"], px, rec["exited"]["how"]))


def _live_follow(rec, moves, bstate, today, ops, paths, cfg, now_utc, log):
    """Live: move the SL-M at FYERS up with the stop, and record it when FYERS fills it."""
    stop_id = ticket.open_stop_id(bstate, rec["symbol"], today)
    if not stop_id:
        if moves or not rec.get("warned"):
            rec["warned"] = True
            rec["note"] = ("no stop-loss order at FYERS to move: press STOP-LOSS (ticket.py protect) so the "
                           "trailing stop has an order to raise")
            log("trail: %s: %s" % (rec["symbol"], rec["note"]))
        return
    rec["note"] = None
    rec["broker_stop_id"] = stop_id
    if moves:
        try:
            ops["modify"](stop_id, rec["stop"])
            rec["broker_stop"] = rec["stop"]
            ticket._record(paths["state"], {
                "at": ticket.ist_iso(now_utc), "date": today, "status": "stop-moved", "symbol": rec["symbol"],
                "broker_order_id": stop_id, "price": rec["stop"],
                "plain": "trailing stop: FYERS stop-loss %s moved to %s" % (stop_id, ticket._p(rec["stop"]))})
        except fyers_api.FyersError as e:
            rec["note"] = "FYERS did not move the stop to %.2f: %s" % (rec["stop"], e)
            log("trail: %s: %s" % (rec["symbol"], rec["note"]))
    try:
        book = ops["orderbook"]()
    except fyers_api.FyersError:
        return
    row = next((o for o in book if str(o.get("id")) == str(stop_id)), None)
    if row and row.get("status_word") == "filled" and row.get("tradedPrice"):
        qty, px = row.get("filledQty") or rec["qty"], float(row["tradedPrice"])
        ticket.apply_fill(paths["portfolio"], cfg, rec["symbol"], "sell", qty, px, today)
        ticket.log_fill(paths["state"], now_utc, today, rec["symbol"], "sell", qty, px)
        ticket._record(paths["state"], {
            "at": ticket.ist_iso(now_utc), "date": today, "status": "stop-filled", "symbol": rec["symbol"],
            "broker_order_id": stop_id, "price": px,
            "plain": "FYERS stop-loss %s filled: SELL %s %s at %s" % (stop_id, ticket._str_num(qty), rec["symbol"],
                                                                     ticket._p(px))})
        rec["exited"] = {"how": "trailing stop" if rec["stop"] > rec["initial_stop"] else "stop", "price": px,
                         "at": ticket.ist_iso(now_utc)}


# ---------------------------------------------------------------- CLI

def _bars_and_price(sym):
    """Today's 5-minute candles and the last price: FYERS after the morning login, else Yahoo."""
    if fyers_api.data_source() == "fyers":
        try:
            doc = fyers_api.chart_doc(sym, "1d", "5m")
        except fyers_api.FyersError as e:
            if fyers_api.fyers_only():
                raise
            sys.stderr.write("FYERS: %s; using Yahoo for this instead.\n" % e)
            doc = None
    else:
        doc = None
    if doc is None:
        import urllib.request
        url = "https://query2.finance.yahoo.com/v8/finance/chart/%s.NS?range=1d&interval=5m" % sym
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                    timeout=20) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    res = doc["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = [{"t": t, "o": o, "h": h, "l": l, "c": c} for t, o, h, l, c in
            zip(res.get("timestamp") or [], q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, l, c)]
    price = res["meta"].get("regularMarketPrice") or (bars[-1]["c"] if bars else None)
    return bars, price


def main(argv=None):
    ap = argparse.ArgumentParser(description="The trailing stop for open day trades (trader-broker).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("check", help="advance every open position's trailing stop once")
    p.add_argument("--config", default=os.path.join("trader", "config.json"))
    p.add_argument("--now", help="pretend it is this India time (IST)")
    p = sub.add_parser("show", help="each position's trailing stop")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        doc = load()
        if not doc:
            print("No trailing stops yet.")
            return 1
        for sym, r in doc.items():
            print("%s  entry %.2f  stop %.2f (first %.2f)%s%s" % (
                sym, r["entry"], r["stop"], r["initial_stop"], "  breakeven" if r["breakeven"] else "",
                ("  exited: %s at %s" % (r["exited"].get("how"), r["exited"].get("price"))) if r.get("exited") else ""))
            for m in r.get("moves", []):
                print("    %s  %.2f -> %.2f  %s" % (m["at"], m["from"], m["to"], m["why"]))
        return 0
    if a.cmd != "check":
        ap.print_help()
        return 1
    cfg = ticket.read_json(a.config, "config", required=False) or {}
    if not enabled(cfg):
        print("The trailing stop is off (\"trailing_stop\": false in %s)." % a.config)
        return 1
    cache = {}

    def get(sym):
        if sym not in cache:
            cache[sym] = _bars_and_price(sym)
        return cache[sym]
    check(cfg, ticket.parse_now(a.now), lambda s: get(s)[0], lambda s: get(s)[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
