#!/usr/bin/env python3
"""Backtest the opening-range breakout on NSE (trader-backtest). Standard library only.

Replays every trading day in the window, for every symbol you give it, with
the pack's own rules, then reports what would have happened after FYERS'
charges: win rate, average R, the worst losing streak, drawdown, month by
month.

    python3 backtest.py run --symbols RELIANCE,HDFCBANK,INFY,TCS,SBIN        # the last 365 days
    python3 backtest.py run --watchlist trader/favorites.txt --days 180
    python3 backtest.py run --universe trader/universe.txt --limit 200      # Prime: the big list
    python3 backtest.py run --symbols TATASTEEL --stop or-low --target 1.5R --json
    python3 backtest.py show                                                # the last report

The rules, the same as the live pack:
  opening range   the first 15 minutes (9:15 to 9:30), from 5-minute bars
  entry           the first 5-minute bar that CLOSES above the range high,
                  closing at 9:30 or later and no later than no_entries_after
                  (14:45); bought at that close, plus slippage
  stop            the range midpoint (--stop or-mid, the default) or the range
                  low (or-low), rounded down onto NSE's tick
  target          2R by default (--target 1R, 1.5R, 3R...), rounded up onto the tick
  exit            the stop, the target, or the close of the last bar before
                  flat_by (15:00). A bar that touches both the stop and the
                  target counts as the stop (the bars cannot say which came
                  first, so the backtest assumes the worse). A gap past the
                  stop or the target fills at the bar's open.
  size            whole shares: max_loss_per_trade_inr / risk a share, capped
                  by max_order_inr, max_position_pct of the budget and the
                  cash after cash_buffer_pct, as trader-budget does
  a day           at most max_trades_per_day entries across all symbols, the
                  earliest triggers first, one per symbol, and only while the
                  max losses of the day's entries fit in max_daily_loss_inr,
                  as trader-risk does
  charges         FYERS equity intraday (fyers.in/charges-list, checked
                  2026-11-02): brokerage the lower of Rs 20 and 0.03% an order
                  (Rs 15 flat with "fyers_plan": "prime"), STT 0.025% of the
                  sell, NSE transaction charges 0.0030699%, SEBI Rs 10 a crore,
                  NSE IPFT Rs 0.01 a crore, stamp duty 0.003% of the buy, GST
                  18% on brokerage + exchange + SEBI + IPFT
  slippage        --slippage-pct (0.02%) against you on the entry, the stop and
                  the flat-by exit; a target is a resting limit, filled at its
                  price

Data: FYERS' 5-minute history (100 days a request; the free plan keeps a year,
Prime five years and more), cached in trader/backtest-cache/ so a second run
asks FYERS only for the days it does not have. Without today's FYERS login it
falls back to Yahoo, which keeps only 60 days of 5-minute bars, and says so.
--offline FILE|DIR reads saved Yahoo chart JSON instead (the tests do).

A backtest is history, not a forecast. It cannot see the order book, the
queue at your stop, a circuit, or how you would have felt after five losses
in a row. It is only as good as the list you give it: today's stocks, tested
on the past, leave out the ones that fell out of the index (survivorship).

Education and tooling, not financial advice.
"""

import argparse
import collections
import csv
import datetime
import gzip
import json
import math
import os
import re
import sys
import urllib.request

IST = datetime.timedelta(hours=5, minutes=30)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
BAR = 5
TICK_BANDS = ((250, 0.01), (1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00))
DEFAULTS = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 50000,
            "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
            "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:00",
            "tick_size": None, "fyers_plan": "standard"}
CHARGES = {"brokerage_pct": 0.0003, "brokerage_cap": 20.0, "brokerage_prime": 15.0, "stt_sell": 0.00025,
           "exchange": 0.000030699, "sebi": 10.0 / 1e7, "ipft": 0.01 / 1e7, "stamp_buy": 0.00003, "gst": 0.18}
CACHE = os.path.join("trader", "backtest-cache")
OUT = os.path.join("trader", "backtest.json")
UA = "Mozilla/5.0 (day-trader-skill-india backtest)"


class BacktestError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def utc_of(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).replace(tzinfo=None)


def ist_of(ts):
    return utc_of(ts) + IST


def today_ist():
    return (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + IST).date()


def hhmm(text, what):
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", str(text or "").strip())
    if not m:
        raise BacktestError("%s wants an India time like 14:45, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def minutes(t):
    return t.hour * 60 + t.minute


def nse_tick(price, override=None):
    if override:
        return float(override)
    for top, step in TICK_BANDS:
        if price <= top:
            return step
    return 5.00


def to_tick(x, tick, up):
    n = x / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 2)


def read_json(path, default=None):
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(path):
    cfg = dict(DEFAULTS)
    doc = read_json(path, {}) or {}
    for k in DEFAULTS:
        if doc.get(k) is not None:
            cfg[k] = doc[k]
    return cfg


def fyers_module():
    """trader-broker/fyers_api.py next door, when FYERS is today's data source; else None."""
    folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-broker")
    if not os.path.isfile(os.path.join(folder, "fyers_api.py")):
        return None
    if folder not in sys.path:
        sys.path.insert(0, folder)
    try:
        import fyers_api
    except ImportError:
        return None
    return fyers_api if fyers_api.data_source() == "fyers" else None


def fyers_only_on():
    folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-broker")
    if folder not in sys.path:
        sys.path.insert(0, folder)
    try:
        import fyers_api
    except ImportError:
        return False
    return fyers_api.fyers_only()


# ---------------------------------------------------------------- charges

def charges(buy_value, sell_value, prime=False):
    """FYERS equity intraday charges for one round trip (a buy and a sell), in rupees."""
    c = CHARGES
    if prime:
        brokerage = c["brokerage_prime"] * 2
    else:
        brokerage = min(c["brokerage_cap"], c["brokerage_pct"] * buy_value) + \
            min(c["brokerage_cap"], c["brokerage_pct"] * sell_value)
    turnover = buy_value + sell_value
    stt = c["stt_sell"] * sell_value
    exchange = c["exchange"] * turnover
    sebi = c["sebi"] * turnover
    ipft = c["ipft"] * turnover
    stamp = c["stamp_buy"] * buy_value
    gst = c["gst"] * (brokerage + exchange + sebi + ipft)
    total = brokerage + stt + exchange + sebi + ipft + stamp + gst
    return {"brokerage": round(brokerage, 2), "stt": round(stt, 2), "exchange": round(exchange, 2),
            "sebi": round(sebi + ipft, 2), "stamp": round(stamp, 2), "gst": round(gst, 2), "total": round(total, 2)}


# ---------------------------------------------------------------- data

def candles_from_yahoo(doc):
    """A Yahoo chart answer -> [[epoch, o, h, l, c, v], ...]."""
    res = ((doc or {}).get("chart") or {}).get("result") or []
    if not res:
        return []
    r = res[0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    out = []
    for i, t in enumerate(r.get("timestamp") or []):
        row = [col[i] if i < len(col) else None for col in cols]
        if None in row[:4]:
            continue
        out.append([int(t)] + [float(x) for x in row[:4]] + [float(row[4] or 0)])
    return out


def offline_candles(path, sym):
    """Saved Yahoo chart JSON: one file, a file named like the symbol in a folder, or a
    {SYM: chart} wall (the scanner's fixture)."""
    files = [path] if os.path.isfile(path) else [
        os.path.join(path, f) for f in sorted(os.listdir(path))
        if f.upper().startswith(sym.upper() + "-") or f.upper().split(".")[0] == sym.upper()]
    for f in files:
        doc = read_json(f)
        if isinstance(doc, dict) and "chart" not in doc and sym.upper() in doc:
            doc = doc[sym.upper()]
        c = candles_from_yahoo(doc)
        if c:
            return c
    return []


def fetch_yahoo(sym):
    """Yahoo keeps 60 days of 5-minute bars: the fallback without a FYERS login."""
    s = sym.upper()
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s.NS?range=60d&interval=5m" % \
        urllib.request.quote(s, safe="-")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return candles_from_yahoo(json.loads(r.read().decode("utf-8", "replace")))


def load_candles(sym, start, end, a, notes):
    """5-minute candles for sym from start to end (IST dates), with a cache for FYERS."""
    if a.offline:
        return offline_candles(a.offline, sym), "offline"
    fy = fyers_module()
    if fy:
        path = os.path.join(a.cache, "%s-5m.json" % re.sub(r"[^A-Z0-9&-]", "_", sym.upper()))
        cached = read_json(path, {}) or {}
        have = cached.get("candles") or []
        lo, hi = cached.get("from"), cached.get("to")
        need = []
        if not have or a.refresh:
            need, have = [(start, end)], []
        else:
            lo_d, hi_d = datetime.date.fromisoformat(lo), datetime.date.fromisoformat(hi)
            if start < lo_d:
                need.append((start, lo_d - datetime.timedelta(days=1)))
            # the last cached day may have been fetched mid-session: fetch it again
            if end >= hi_d:
                need.append((hi_d, end))
        try:
            for a_, b_ in need:
                have = [c for c in have if not (a_ <= ist_of(c[0]).date() <= b_)]
                have += fy.history(sym, "5", a_, b_)
        except fy.FyersError as e:
            if not have:
                raise BacktestError("FYERS: %s" % e)
            notes.append("%s: FYERS stopped (%s): using the %d cached bars" % (sym, e, len(have)))
        have = sorted({c[0]: c for c in have}.values(), key=lambda c: c[0])
        span = [ist_of(c[0]).date() for c in have]
        if have:
            write_json(path, {"symbol": sym.upper(), "from": min(min(span), start).isoformat(),
                              "to": max(max(span), end).isoformat(), "candles": have})
        return [c for c in have if start <= ist_of(c[0]).date() <= end], "fyers"
    if fyers_only_on():
        raise BacktestError("FYERS only is on and FYERS is not available: log in with FYERS first")
    try:
        c = fetch_yahoo(sym)
    except Exception as e:
        raise BacktestError("Yahoo: %s" % e)
    return [x for x in c if start <= ist_of(x[0]).date() <= end], "yahoo"


def sessions(candles):
    """{date: [bar, ...]} of whole 5-minute bars inside 9:15 to 15:30 IST, by start time."""
    days = collections.defaultdict(list)
    for t, o, h, l, c, v in candles:
        it = ist_of(t)
        m = minutes(it.time())
        if it.second or (m - minutes(OPEN)) % BAR or not (minutes(OPEN) <= m < minutes(CLOSE)):
            continue
        days[it.date()].append({"t": t, "m": m, "o": o, "h": h, "l": l, "c": c, "v": v})
    for d in days:
        days[d].sort(key=lambda b: b["m"])
    return days


# ---------------------------------------------------------------- one day, one symbol

def simulate_day(sym, day, bars, cfg, rules):
    """The trade the rules would have made that day, or None (with the reason in a dict)."""
    or_end = minutes(OPEN) + rules["or_minutes"]
    or_bars = [b for b in bars if b["m"] < or_end]
    if len(or_bars) < rules["or_minutes"] // BAR or or_bars[0]["m"] != minutes(OPEN):
        return {"skip": "incomplete opening range"}
    hi = max(b["h"] for b in or_bars)
    lo = min(b["l"] for b in or_bars)
    before = minutes(hhmm(cfg["no_entries_before"], "no_entries_before"))
    after = minutes(hhmm(cfg["no_entries_after"], "no_entries_after"))
    flat = minutes(hhmm(cfg["flat_by"], "flat_by"))
    avg_vol = sum(b["v"] for b in or_bars) / len(or_bars)
    trig = None
    for i, b in enumerate(bars):
        end = b["m"] + BAR
        if b["m"] < or_end or end < before:
            continue
        if end > after:
            break
        if b["c"] > hi:
            if rules["min_volume_x"] and avg_vol and b["v"] < rules["min_volume_x"] * avg_vol:
                continue
            trig = i
            break
    if trig is None:
        return {"skip": "no breakout"}
    tb = bars[trig]
    slip = rules["slippage"]
    entry = round(tb["c"] * (1 + slip), 2)
    tick = nse_tick(entry, cfg.get("tick_size"))
    stop = to_tick((hi + lo) / 2.0 if rules["stop"] == "or-mid" else lo, tick, up=False)
    risk = round(entry - stop, 2)
    if risk <= 0 or risk < 0.0005 * entry:
        return {"skip": "risk too tight"}
    target = to_tick(entry + rules["target_r"] * risk, tick, up=True)
    # whole shares, as trader-budget sizes them
    budget = float(cfg["budget"])
    cash = budget * (1 - float(cfg["cash_buffer_pct"]) / 100.0)
    caps = [int(float(cfg["max_loss_per_trade_inr"]) // risk), int(float(cfg["max_order_inr"]) // entry),
            int(budget * float(cfg["max_position_pct"]) / 100.0 // entry), int(cash // entry)]
    qty = min(caps)
    if qty <= 0:
        return {"skip": "too big for one share at your max loss"}
    exit_px, outcome, exit_bar = None, None, None
    for b in bars[trig + 1:]:
        if b["m"] + BAR > flat:
            break
        exit_bar = b
        if b["o"] <= stop:
            exit_px, outcome = b["o"] * (1 - slip), "stop"
        elif b["l"] <= stop:
            exit_px, outcome = stop * (1 - slip), "stop"
        elif b["o"] >= target:
            exit_px, outcome = b["o"], "target"
        elif b["h"] >= target:
            exit_px, outcome = target, "target"
        if outcome:
            break
    if not outcome:
        if exit_bar is None:
            return {"skip": "no bar left before flat-by"}
        exit_px, outcome = exit_bar["c"] * (1 - slip), "flat"
    exit_px = round(exit_px, 2)
    buy, sell = entry * qty, exit_px * qty
    ch = charges(buy, sell, rules["prime"])
    gross = round(sell - buy, 2)
    net = round(gross - ch["total"], 2)
    planned = risk * qty
    held = (exit_bar["m"] + BAR - (tb["m"] + BAR)) if exit_bar else 0
    return {"symbol": sym, "date": day.isoformat(), "time": "%02d:%02d" % divmod(tb["m"] + BAR, 60),
            "or_high": round(hi, 2), "or_low": round(lo, 2), "entry": entry, "stop": stop, "target": target,
            "qty": qty, "exit": exit_px, "outcome": outcome,
            "exit_time": "%02d:%02d" % divmod((exit_bar["m"] + BAR) if exit_bar else tb["m"] + BAR, 60),
            "minutes": held, "max_loss": round(planned, 2), "gross": gross, "charges": ch["total"], "net": net,
            "r": round(gross / planned, 2), "r_net": round(net / planned, 2), "_m": tb["m"]}


# ---------------------------------------------------------------- the whole run

def run(symbols, start, end, cfg, rules, a, progress=None):
    notes, sources, failed = [], collections.Counter(), {}
    by_day = collections.defaultdict(list)
    skipped = collections.Counter()
    days_seen = set()
    for n, sym in enumerate(symbols, 1):
        if progress and (n % 10 == 0 or n == len(symbols)):
            progress("  %d of %d symbols" % (n, len(symbols)))
        try:
            candles, src = load_candles(sym, start, end, a, notes)
        except BacktestError as e:
            failed[sym] = str(e)
            continue
        sources[src] += 1
        if not candles:
            failed[sym] = "no 5-minute bars in the window"
            continue
        for day, bars in sorted(sessions(candles).items()):
            days_seen.add(day)
            res = simulate_day(sym, day, bars, cfg, rules)
            if "skip" in res:
                skipped[res["skip"]] += 1
            else:
                by_day[day].append(res)
    trades, passed_over = pick_trades(by_day, cfg)
    return {"trades": trades, "sessions": len(days_seen), "skipped": dict(skipped),
            "passed_over": dict(passed_over), "failed": failed, "sources": dict(sources), "notes": notes}


def pick_trades(by_day, cfg):
    """The day limits, as trader-risk applies them: the earliest triggers first, one per symbol,
    at most max_trades_per_day, while the day's max losses fit max_daily_loss_inr."""
    trades, passed_over = [], collections.Counter()
    for day in sorted(by_day):
        taken, at_risk, syms = 0, 0.0, set()
        for t in sorted(by_day[day], key=lambda x: (x["_m"], x["symbol"])):
            if taken >= int(cfg["max_trades_per_day"]):
                passed_over["max_trades_per_day"] += 1
                continue
            if at_risk + t["max_loss"] > float(cfg["max_daily_loss_inr"]) + 1e-9:
                passed_over["max_daily_loss_inr"] += 1
                continue
            if t["symbol"] in syms:
                continue
            taken += 1
            at_risk += t["max_loss"]
            syms.add(t["symbol"])
            trades.append({k: v for k, v in t.items() if not k.startswith("_")})
    return trades, passed_over


def streaks(values):
    """(worst losing streak, best winning streak) over a list of results, in order."""
    worst = best = cur_l = cur_w = 0
    for v in values:
        if v < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w, cur_l = (cur_w + 1 if v > 0 else 0), 0
        worst, best = max(worst, cur_l), max(best, cur_w)
    return worst, best


def summarize(trades):
    n = len(trades)
    if not n:
        return {"trades": 0}
    nets = [t["net"] for t in trades]
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] < 0]
    eq, peak, dd, curve = 0.0, 0.0, 0.0, []
    for v in nets:
        eq += v
        curve.append(round(eq, 2))
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    worst_streak, best_streak = streaks(nets)
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    months = collections.OrderedDict()
    for t in trades:
        m = months.setdefault(t["date"][:7], {"trades": 0, "wins": 0, "net": 0.0})
        m["trades"] += 1
        m["wins"] += t["net"] > 0
        m["net"] = round(m["net"] + t["net"], 2)
    outcomes = collections.Counter(t["outcome"] for t in trades)
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / n, 1),
        "avg_r": round(sum(t["r"] for t in trades) / n, 3),
        "avg_r_net": round(sum(t["r_net"] for t in trades) / n, 3),
        "gross": round(sum(t["gross"] for t in trades), 2),
        "charges": round(sum(t["charges"] for t in trades), 2),
        "net": round(sum(nets), 2),
        "expectancy": round(sum(nets) / n, 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "worst_losing_streak": worst_streak, "best_winning_streak": best_streak,
        "max_drawdown": round(dd, 2),
        "best": max(trades, key=lambda t: t["net"]), "worst": min(trades, key=lambda t: t["net"]),
        "outcomes": dict(outcomes), "months": months,
        "avg_minutes": round(sum(t["minutes"] for t in trades) / n, 1),
        "equity": curve,
    }


def money(v, d=0):
    if v is None:
        return "-"
    s = "{:,.{}f}".format(abs(v), d)
    return ("-₹" if v < 0 else "₹") + s


def render(doc):
    s, p = doc["summary"], doc["params"]
    src = {"fyers": "FYERS", "yahoo": "Yahoo", "offline": "saved"}
    out = ["Opening-range breakout backtest, %s to %s, %d symbol%s, %d sessions. Data: %s."
           % (p["from"], p["to"], p["symbols"], "" if p["symbols"] == 1 else "s", doc["sessions"],
              ", ".join("%s for %d symbol%s" % (src.get(k, k), v, "" if v == 1 else "s")
                        for k, v in sorted(doc["sources"].items())) or "none"),
           "Rules: %d-minute range, entries %s to %s, stop %s, target %sR, flat by %s, max loss %s a trade, "
           "%d trades and %s a day, slippage %.3f%%, FYERS %s brokerage."
           % (p["or_minutes"], p["no_entries_before"], p["no_entries_after"], p["stop"], _r(p["target_r"]),
              p["flat_by"], money(p["max_loss_per_trade_inr"]), p["max_trades_per_day"],
              money(p["max_daily_loss_inr"]), 100 * p["slippage"], p["plan"]), ""]
    if not s.get("trades"):
        out.append("No trades: no breakout passed the rules in this window.")
    else:
        out += [
            "Trades            %d  (%d won, %d lost after charges)" % (s["trades"], s["wins"], s["losses"]),
            "Win rate          %.1f%%" % s["win_rate"],
            "Average R         %+.2fR before charges, %+.2fR after" % (s["avg_r"], s["avg_r_net"]),
            "Net P&L           %s  (gross %s, charges %s)" % (money(s["net"]), money(s["gross"]), money(s["charges"])),
            "Per trade         %s on average; average win %s, average loss %s" % (
                money(s["expectancy"], 2), money(s["avg_win"]), money(s["avg_loss"])),
            "Profit factor     %s" % (s["profit_factor"] if s["profit_factor"] is not None else "-"),
            "Worst losing run  %d trades in a row  (best winning run %d)" % (s["worst_losing_streak"],
                                                                               s["best_winning_streak"]),
            "Max drawdown      %s" % money(s["max_drawdown"]),
            "Exits             %s; held %s min on average" % (
                ", ".join("%s %d" % kv for kv in sorted(s["outcomes"].items())), s["avg_minutes"]),
            "Best / worst      %s %s %s / %s %s %s" % (s["best"]["date"], s["best"]["symbol"], money(s["best"]["net"]),
                                                      s["worst"]["date"], s["worst"]["symbol"], money(s["worst"]["net"])),
            "", "Month     trades  win%     net"]
        for m, v in s["months"].items():
            out.append("%s   %5d  %4.0f%%  %10s" % (m, v["trades"], 100.0 * v["wins"] / v["trades"], money(v["net"])))
    if doc["passed_over"]:
        out.append("")
        out.append("Breakouts left out by the day limits: %s." % ", ".join(
            "%d by %s" % (v, k) for k, v in sorted(doc["passed_over"].items())))
    if doc["failed"]:
        out.append("No data for %d symbol%s: %s" % (len(doc["failed"]), "" if len(doc["failed"]) == 1 else "s",
                                                   "; ".join("%s (%s)" % kv for kv in list(doc["failed"].items())[:5])))
    for n in doc["notes"][:5]:
        out.append("Note: " + n)
    out += ["", "History, not a forecast: fills at bar closes, no order book, no circuits, and today's list tested "
                "on the past. Paper trade it before you trust it. Not financial advice."]
    return "\n".join(out)


def _r(x):
    return ("%g" % x)


def read_symbols(a):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    else:
        path = a.watchlist or a.universe or os.path.join("trader", "favorites.txt")
        if not os.path.exists(path):
            raise BacktestError("which symbols? --symbols RELIANCE,INFY, --watchlist FILE or --universe FILE")
        if path.endswith(".json"):
            doc = read_json(path, {}) or {}
            syms = [h.get("symbol") for h in doc.get("hits", [])] if isinstance(doc, dict) else []
        else:
            with open(path, encoding="utf-8") as fh:
                syms = [l.split("|")[0].strip().upper() for l in fh if l.strip() and not l.startswith("#")]
    out = []
    for s in syms:
        if s and s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise BacktestError("no symbols to test")
    return out


def parse_target(text):
    m = re.match(r"^(\d+(?:\.\d+)?)R$", str(text).strip(), re.I)
    if not m or float(m.group(1)) <= 0:
        raise BacktestError("--target wants a multiple of the risk, like 2R or 1.5R")
    return float(m.group(1))


def cmd_run(a):
    cfg = load_config(a.config)
    for k in ("no_entries_after", "flat_by"):
        if getattr(a, k):
            cfg[k] = getattr(a, k)
    for k in ("budget", "max_loss_per_trade_inr", "max_trades_per_day"):
        v = getattr(a, k)
        if v is not None:
            cfg[k] = v
    end = datetime.date.fromisoformat(a.to) if a.to else today_ist()
    start = datetime.date.fromisoformat(a.from_) if a.from_ else end - datetime.timedelta(days=a.days)
    if start > end:
        raise BacktestError("--from is after --to")
    plan = (a.plan or cfg.get("fyers_plan") or "standard").lower()
    rules = {"or_minutes": a.or_minutes, "stop": a.stop, "target_r": parse_target(a.target),
             "slippage": a.slippage_pct / 100.0, "min_volume_x": a.min_volume_x, "prime": plan == "prime"}
    symbols = read_symbols(a)
    prog = None if a.json else (lambda m: print(m, file=sys.stderr))
    if prog:
        prog("Backtesting %d symbol%s, %s to %s..." % (len(symbols), "" if len(symbols) == 1 else "s", start, end))
    res = run(symbols, start, end, cfg, rules, a, prog)
    if res["sources"].get("yahoo"):
        res["notes"].insert(0, "no FYERS login today: Yahoo's 5-minute bars go back about 60 days only, so this "
                               "is a shorter test than asked. Log in to FYERS for a year.")
    params = {"from": start.isoformat(), "to": end.isoformat(), "symbols": len(symbols),
              "or_minutes": a.or_minutes, "stop": a.stop, "target_r": rules["target_r"],
              "slippage": rules["slippage"], "plan": plan, "min_volume_x": a.min_volume_x}
    params.update({k: cfg[k] for k in ("no_entries_before", "no_entries_after", "flat_by", "budget",
                                       "max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day",
                                       "max_order_inr")})
    doc = {"created_by": "trader-backtest", "created_at": datetime.datetime.now(datetime.timezone.utc)
           .strftime("%Y-%m-%dT%H:%M:%SZ"), "params": params, "symbols": symbols,
           "summary": summarize(res["trades"]), "trades": res["trades"], "sessions": res["sessions"],
           "skipped": res["skipped"], "passed_over": res["passed_over"], "failed": res["failed"],
           "sources": res["sources"], "notes": res["notes"],
           "note": "History, not a forecast. Not financial advice."}
    if not a.no_write:
        write_json(a.out, doc)
        with open(os.path.splitext(a.out)[0] + "-trades.csv", "w", newline="", encoding="utf-8") as fh:
            cols = ["date", "symbol", "time", "entry", "stop", "target", "qty", "exit", "exit_time", "outcome",
                    "minutes", "gross", "charges", "net", "r", "r_net"]
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for t in res["trades"]:
                w.writerow(t)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print(render(doc))
        if not a.no_write:
            print("\nWrote %s and the trade list %s." % (a.out, os.path.splitext(a.out)[0] + "-trades.csv"))
    return 0 if res["trades"] else 1


def cmd_show(a):
    doc = read_json(a.out)
    if not doc:
        raise BacktestError("no backtest yet at %s: backtest.py run first" % a.out)
    print(json.dumps(doc, indent=2) if a.json else render(doc))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest the opening-range breakout on NSE (trader-backtest).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run", help="replay the rules over past sessions")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--symbols", help="comma-separated NSE symbols")
    g.add_argument("--watchlist", help="a text file of symbols, or a scanner watchlist.json")
    g.add_argument("--universe", help="trader/universe.txt (SYMBOL|Name a line)")
    p.add_argument("--limit", type=int, help="only the first N symbols")
    p.add_argument("--days", type=int, default=365, help="calendar days back from --to (default 365)")
    p.add_argument("--from", dest="from_", help="YYYY-MM-DD (instead of --days)")
    p.add_argument("--to", help="YYYY-MM-DD (default today)")
    p.add_argument("--or-minutes", type=int, default=15, choices=[5, 10, 15, 30])
    p.add_argument("--stop", default="or-mid", choices=["or-mid", "or-low"])
    p.add_argument("--target", default="2R")
    p.add_argument("--slippage-pct", type=float, default=0.02, help="against you, per fill (default 0.02)")
    p.add_argument("--min-volume-x", type=float, help="breakout bar volume vs the range bars' average")
    p.add_argument("--no-entries-after", help="HH:MM (default: config, 14:45)")
    p.add_argument("--flat-by", help="HH:MM (default: config, 15:00)")
    p.add_argument("--budget", type=float)
    p.add_argument("--max-loss-per-trade-inr", type=float)
    p.add_argument("--max-trades-per-day", type=int)
    p.add_argument("--plan", choices=["standard", "prime"], help="FYERS brokerage (default: config fyers_plan)")
    p.add_argument("--config", default=os.path.join("trader", "config.json"))
    p.add_argument("--offline", help="saved Yahoo chart JSON: a file, a folder, or the scanner's wall file")
    p.add_argument("--cache", default=CACHE)
    p.add_argument("--refresh", action="store_true", help="fetch everything from FYERS again")
    p.add_argument("--out", default=OUT)
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the last backtest")
    p.add_argument("--out", default=OUT)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "show": cmd_show}[a.cmd](a)
    except BacktestError as e:
        print("backtest.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
