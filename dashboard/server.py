#!/usr/bin/env python3
"""The day-trader dashboard: a local web app over the pack's own tools. Standard library only.

    python3 dashboard/server.py                      # live: your trader/ folder, FYERS or Yahoo data
    python3 dashboard/server.py --demo               # demo: replays a real session from the test data
    python3 dashboard/server.py --workdir ~/trading --port 8765

Open http://127.0.0.1:8765. It listens on 127.0.0.1 only: nothing on your
network can reach it, and it never needs to.

How it works. Every number on the screen comes from the pack's tools: the
scanner, entry timer, exit planner, time machine, budget, risk check, ticket
and alerts run as they do from the command line, in your working folder, and
the dashboard shows their JSON. Orders keep the pack's gates: trader-risk PASS
for that exact order, your typed "yes" (recorded word for word), then a second
confirm before ticket.py place sends it to FYERS. In paper mode (the default)
nothing goes to FYERS: the "fill" is the latest price.

Demo mode replays Monday 2026-10-19 from tests/fixtures (real 5-minute charts
of 234 US stocks, moved onto the NSE clock), with a clock you can play, pause
and scrub. The saved charts cover the opening hour: 9:15 to 10:45. Its state lives in its own folder (default ./trader-demo).

Education and tooling, not financial advice.
"""

import argparse
import collections
import datetime
import gzip
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(ROOT, "tests", "fixtures")
IST = datetime.timedelta(hours=5, minutes=30)
TOOLS = {
    "scan": os.path.join(ROOT, "trader-scanner", "scan.py"),
    "entry": os.path.join(ROOT, "trader-entry", "entry.py"),
    "exit": os.path.join(ROOT, "trader-exit", "exit.py"),
    "tm": os.path.join(ROOT, "trader-timemachine", "timemachine.py"),
    "size": os.path.join(ROOT, "trader-budget", "size.py"),
    "risk": os.path.join(ROOT, "trader-risk", "check.py"),
    "ticket": os.path.join(ROOT, "trader-broker", "ticket.py"),
    "alerts": os.path.join(ROOT, "trader-alerts", "alerts.py"),
    "news": os.path.join(ROOT, "trader-news", "news.py"),
    "facts": os.path.join(ROOT, "trader-analyst", "facts.py"),
}
DEMO_DAY = datetime.date(2026, 10, 19)
DEMO_END = datetime.time(10, 45)           # the saved charts run to 10:05 (TSLA to 10:43)
DEMO_SCAN_END = "10:05"
DEMO = {
    "wall": os.path.join(FIX, "trader-scanner", "wall5m-2026-10-19.json.gz"),
    "tm": {"TSLA": os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")},
    "news": os.path.join(FIX, "trader-news"),
    "news_now": "2026-10-19T14:15:00+00:00",
    "analyst": os.path.join(FIX, "trader-analyst"),
    "favorites": ["TSLA", "AAPL", "NVDA", "AMD", "META", "MSFT", "AMZN", "GOOGL", "NFLX", "JPM"],
}


LIVE_FAVORITES = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK"]


def load_module(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("dash_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


alerts_mod = load_module("trader-alerts", "alerts")
scan_mod = load_module("trader-scanner", "scan")
fyers = load_module("trader-broker", "fyers_api")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def read_json(path, default=None):
    try:
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
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- the clock

class Clock:
    """Real time, or in demo a replay clock you can play, pause and scrub (IST)."""

    def __init__(self, demo):
        self.demo = demo
        self.sim = datetime.datetime.combine(DEMO_DAY, datetime.time(9, 50))
        self.playing = False
        self.speed = 10.0
        self._last = time.monotonic()
        self.lock = threading.Lock()

    def ist(self):
        if not self.demo:
            return utcnow() + IST
        with self.lock:
            now = time.monotonic()
            if self.playing:
                self.sim += datetime.timedelta(seconds=(now - self._last) * self.speed)
                end = datetime.datetime.combine(DEMO_DAY, DEMO_END)
                if self.sim >= end:
                    self.sim, self.playing = end, False
            self._last = now
            return self.sim.replace(microsecond=0)

    def utc(self):
        return self.ist() - IST

    def set(self, hhmm=None, playing=None, speed=None):
        with self.lock:
            self.ist_unlocked_tick()
            if hhmm:
                h, m = [int(x) for x in hhmm.split(":")[:2]]
                t = datetime.datetime.combine(DEMO_DAY, datetime.time(h, m))
                lo = datetime.datetime.combine(DEMO_DAY, datetime.time(9, 0))
                hi = datetime.datetime.combine(DEMO_DAY, DEMO_END)
                self.sim = min(max(t, lo), hi)
            if playing is not None:
                self.playing = bool(playing)
            if speed:
                self.speed = max(1.0, min(600.0, float(speed)))
            self._last = time.monotonic()

    def ist_unlocked_tick(self):
        now = time.monotonic()
        if self.playing:
            self.sim += datetime.timedelta(seconds=(now - self._last) * self.speed)
        self._last = now

    def now_arg(self):
        return self.ist().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- the app

class App:
    def __init__(self, workdir, demo, port):
        self.demo = demo
        self.port = port
        self.workdir = os.path.abspath(workdir)
        os.makedirs(os.path.join(self.workdir, "trader", "state"), exist_ok=True)
        os.chdir(self.workdir)                 # the tools and fyers_api read trader/... from here
        self.clock = Clock(demo)
        self.cache = {}
        self.lock = threading.Lock()
        self._wall = None
        self._first_run()

    # ------------------------------------------------ first run

    def _first_run(self):
        """A config from the example (paper, ₹1 lakh, ₹500 max loss) and starter favourites, if missing."""
        cfg = os.path.join("trader", "config.json")
        if not os.path.exists(cfg):
            shutil.copy(os.path.join(ROOT, "trader-budget", "config.example.json"), cfg)
        fav = os.path.join("trader", "favorites.txt")
        if not os.path.exists(fav):
            with open(fav, "w") as fh:
                fh.write("\n".join(DEMO["favorites"] if self.demo else LIVE_FAVORITES) + "\n")

    def wall(self):
        if self._wall is None:
            with gzip.open(DEMO["wall"], "rt", encoding="utf-8") as fh:
                self._wall = json.load(fh)
        return self._wall

    def demo_chart_file(self, sym):
        """The day's chart for one symbol as its own file, cut at the replay clock."""
        doc = self.wall().get(sym)
        if not doc:
            raise ToolError("the demo has no chart for %s" % sym)
        path = os.path.join("trader", "demo-charts", "%s.json" % sym)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json(path, doc)
        return path

    # ------------------------------------------------ running the tools

    def run(self, tool, args, want_json=True, timeout=180, env=None):
        cmd = [sys.executable, TOOLS[tool]] + [str(a) for a in args]
        e = dict(os.environ)
        if self.demo:
            e["TRADER_DATA_SOURCE"] = "yahoo"      # demo data is offline anyway: never touch FYERS
        if env:
            e.update(env)
        try:
            p = subprocess.run(cmd, cwd=self.workdir, capture_output=True, text=True, timeout=timeout, env=e)
        except subprocess.TimeoutExpired:
            raise ToolError("%s took longer than %ds" % (os.path.basename(TOOLS[tool]), timeout))
        out, err = p.stdout.strip(), p.stderr.strip()
        data = None
        if want_json and out:
            try:
                data = json.loads(out)
            except ValueError:
                start = out.find("{") if "{" in out else out.find("[")
                if start >= 0:
                    try:
                        data = json.loads(out[start:])
                    except ValueError:
                        data = None
        return {"code": p.returncode, "data": data, "text": out, "error": err}

    def now_args(self):
        return ["--now", self.clock.now_arg()] if self.demo else []

    # ------------------------------------------------ status

    def config(self):
        return read_json(os.path.join("trader", "config.json"), {}) or {}

    def favorites(self):
        try:
            with open(os.path.join("trader", "favorites.txt"), encoding="utf-8") as fh:
                return [l.strip().upper() for l in fh if l.strip() and not l.startswith("#")]
        except OSError:
            return []

    def source(self):
        if self.demo:
            return "demo"
        return fyers.data_source()

    def status(self):
        now_utc = self.clock.utc()
        st = alerts_mod.market_status(now_utc)
        cfg = self.config()
        fcfg = fyers.load_config()
        tok = fyers.load_token()
        return {
            "mode": "demo" if self.demo else ("paper" if cfg.get("paper", True) else "live"),
            "paper": bool(cfg.get("paper", True)), "demo": self.demo,
            "now_ist": self.clock.ist().strftime("%Y-%m-%d %H:%M:%S"),
            "session": st, "source": self.source(),
            "fyers": {"configured": bool(fcfg.get("app_id")), "logged_in": bool(tok),
                      "app_id": fcfg.get("app_id", ""), "secret": bool(fyers.load_secret()),
                      "redirect": fcfg.get("redirect_uri") or "http://127.0.0.1:%d/fyers/callback" % self.port},
            "clock": {"playing": self.clock.playing, "speed": self.clock.speed,
                      "end": DEMO_END.strftime("%H:%M")} if self.demo else None,
            "config": {k: cfg.get(k) for k in ("budget", "max_loss_per_trade_inr", "max_daily_loss_inr",
                                               "max_trades_per_day", "no_entries_before", "no_entries_after",
                                               "flat_by", "min_reward_risk", "paper", "broker")},
            "favorites": self.favorites(), "workdir": self.workdir,
        }

    # ------------------------------------------------ market data

    def chart(self, sym, rng="1d", interval="5m"):
        sym = sym.upper().strip()
        if self.demo:
            doc = self.wall().get(sym)
            if not doc:
                raise ToolError("the demo has no chart for %s" % sym)
            res = doc["chart"]["result"][0]
            cut = int((self.clock.utc() - datetime.datetime(1970, 1, 1)).total_seconds())
        else:
            key = ("chart", sym, rng, interval)
            hit = self.cache.get(key)
            if hit and time.time() - hit[0] < (20 if interval == "5m" else 300):
                res = hit[1]
            else:
                text = scan_mod.yahoo_get(scan_mod.chart_path(sym, rng, interval))
                doc = json.loads(text)
                if not (doc.get("chart") or {}).get("result"):
                    raise ToolError("no chart for %s" % sym)
                res = doc["chart"]["result"][0]
                self.cache[key] = (time.time(), res)
            cut = None
        s = scan_mod.series_from_result(res)
        bars = []
        for i, t in enumerate(s["t"]):
            if cut is not None and t > cut - 1:
                break
            c = s["c"][i]
            if c is None:
                continue
            o = (s["o"] or [None])[i] if s["o"] else c
            h = (s["h"] or [None])[i] if s["h"] else c
            l = (s["l"] or [None])[i] if s["l"] else c
            v = (s["v"] or [0])[i] if s["v"] else 0
            if cut is not None and t + 300 > cut:
                frac = max(0.05, min(1.0, (cut - t) / 300.0))  # the forming bar, drawn as far as the replay clock
                c = o + (c - o) * frac
                h = max(o, c, o + (h - o) * frac)
                l = min(o, c, o + (l - o) * frac)
                v = int((v or 0) * frac)
            bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
        meta = s["meta"] or {}
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        price = bars[-1]["c"] if bars else meta.get("regularMarketPrice")
        return {"symbol": sym, "bars": bars, "prev_close": prev, "price": price,
                "pct": round(100.0 * (price / prev - 1), 2) if price and prev else None,
                "source": self.source(), "interval": interval}

    def quotes(self, symbols):
        syms = []
        for s in symbols:
            s = s.upper().strip()
            if s and s not in syms:
                syms.append(s)
        out = {}
        if not syms:
            return out
        if self.demo:
            for s in syms:
                try:
                    c = self.chart(s)
                except ToolError:
                    continue
                spark = [b["c"] for b in c["bars"]]
                out[s] = {"price": c["price"], "prev_close": c["prev_close"], "pct": c["pct"],
                          "spark": spark[-40:]}
            return out
        key = ("quotes", tuple(syms))
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < (3 if self.source() == "fyers" else 10):
            return hit[1]
        if self.source() == "fyers":
            try:
                q = fyers.quotes(syms)
                for s, v in q.items():
                    out[s] = {"price": v["price"], "prev_close": v["prev_close"], "pct": v["pct"], "spark": None}
            except fyers.FyersError as e:
                out["_error"] = str(e)
        else:
            data, failed = scan_mod.fetch_spark(syms, "1d", "5m", 0.2)
            for s, ser in data.items():
                closes = [c for c in ser["c"] if c is not None]
                meta = ser["meta"]
                prev = meta.get("chartPreviousClose") or meta.get("previousClose")
                price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
                out[s] = {"price": price, "prev_close": prev,
                          "pct": round(100.0 * (price / prev - 1), 2) if price and prev else None,
                          "spark": closes[-40:]}
        self.cache[key] = (time.time(), out)
        return out

    # ------------------------------------------------ the pipeline

    def scan(self, scope="market"):
        if self.demo:
            now = min(self.clock.now_arg(), "%s %s" % (DEMO_DAY.isoformat(), DEMO_SCAN_END))
            r = self.run("scan", ["orb", "--offline", DEMO["wall"], "--min-price", "0", "--min-volume", "0",
                                  "--json", "--now", now])
        else:
            args = ["orb", "--json"]
            env = None
            if scope == "favorites":
                fav = os.path.join("trader", "favorites.txt")
                if not os.path.exists(fav):
                    raise ToolError("add symbols to your favourites first")
                args += ["--watchlist", fav]
            else:
                if not os.path.exists(os.path.join("trader", "universe.txt")):
                    u = self.run("scan", ["universe"], want_json=False, timeout=300)
                    if u["code"] != 0:
                        raise ToolError("could not download NSE's equity list: " + (u["error"] or u["text"])[-300:])
                env = {"TRADER_DATA_SOURCE": "yahoo"}     # the whole market: Yahoo's batches, not FYERS' 50 a minute
            r = self.run("scan", args, timeout=900, env=env)
        if r["data"] is None:
            raise ToolError((r["error"] or r["text"] or "the scan failed")[-500:])
        return r["data"]

    def entry(self, sym):
        args = ["check", sym, "--json", "--write"] + self.now_args()
        if self.demo:
            args += ["--offline", self.demo_chart_file(sym)]
        r = self.run("entry", args)
        if r["data"] is None:
            raise ToolError((r["error"] or r["text"])[-500:])
        return r["data"]

    def plan(self, body):
        args = ["plan", "--json", "--write"] + self.now_args()
        if body.get("stop"):
            args += ["--stop", body["stop"]]
        if body.get("target"):
            args += ["--target", body["target"]]
        if body.get("entry"):
            args += ["--entry", str(body["entry"])]
        r = self.run("exit", args)
        if r["data"] is None or r["code"] != 0:
            raise ToolError((r["error"] or r["text"])[-500:])
        return r["data"]

    def timemachine(self):
        plan = read_json(os.path.join("trader", "plan.json"), {}) or {}
        sym = str(plan.get("symbol", "")).upper()
        if not sym:
            raise ToolError("make a plan first (Exit Planner)")
        args = ["run", "--json", "--write"] + self.now_args()
        if self.demo:
            if sym not in DEMO["tm"]:
                raise ToolError("the demo has 60 days of bars for TSLA only: the Time Machine runs on TSLA here")
            args += ["--offline", DEMO["tm"][sym]]
        if plan.get("flat_by"):
            args += ["--until", plan["flat_by"]]
        r = self.run("tm", args, timeout=300)
        if r["data"] is None:
            raise ToolError((r["error"] or r["text"])[-500:])
        return r["data"]

    def size(self, body):
        args = ["--write", "--json"] + self.now_args()
        if body.get("limit"):
            args += ["--limit"]
        r = self.run("size", args)
        if r["data"] is None:
            raise ToolError((r["error"] or r["text"])[-500:])
        return r["data"]

    def risk(self):
        r = self.run("risk", ["--json"] + self.now_args())
        if r["data"] is None:
            raise ToolError((r["error"] or r["text"])[-500:])
        return r["data"]

    def tickets(self):
        if not os.path.exists(os.path.join("trader", "orders.json")):
            return []
        r = self.run("ticket", ["show", "--json"] + self.now_args())
        d = r["data"]
        return d if isinstance(d, list) else ([d] if d else [])

    def approve(self, index, reply):
        r = self.run("ticket", ["approve", "--index", index, "--reply", reply] + self.now_args(), want_json=False)
        return {"ok": r["code"] == 0, "message": r["text"] or r["error"]}

    def decline(self, index):
        r = self.run("ticket", ["decline", "--index", index, "--reply", "no (dashboard)"] + self.now_args(),
                     want_json=False)
        return {"ok": r["code"] == 0, "message": r["text"] or r["error"]}

    def execute(self, index, confirm):
        tickets = self.tickets()
        t = next((x for x in tickets if int(x.get("index", 0)) == int(index)), None)
        if not t:
            raise ToolError("no order %s" % index)
        sym = t["tool_input"].get("symbol", "").split(":")[-1].replace("-EQ", "")
        qty = t["tool_input"].get("qty")
        if self.config().get("paper", True):
            q = self.quotes([sym]).get(sym) or {}
            price = q.get("price")
            if not price:
                raise ToolError("no latest price for %s: cannot record a paper fill" % sym)
            r = self.run("ticket", ["result", "--index", index, "--status", "paper", "--filled-qty", qty,
                                    "--filled-price", "%.2f" % price] + self.now_args(), want_json=False)
            return {"ok": r["code"] == 0, "message": r["text"] or r["error"], "paper": True, "price": price}
        if str(confirm or "").strip().upper() != sym:
            raise ToolError("type the symbol (%s) to confirm a live order" % sym)
        r = self.run("ticket", ["place", "--index", index], want_json=False, timeout=60)
        return {"ok": r["code"] == 0, "message": r["text"] or r["error"], "paper": False}

    def protect(self, sym):
        r = self.run("ticket", ["protect", "--symbol", sym] + self.now_args(), want_json=False)
        if r["code"] != 0:
            raise ToolError(r["error"] or r["text"])
        return {"ok": True, "message": r["text"], "risk": self.risk(), "tickets": self.tickets()}

    def flatten(self, sym):
        r = self.run("ticket", ["flatten", "--symbol", sym] + self.now_args(), want_json=False)
        if r["code"] != 0:
            raise ToolError(r["error"] or r["text"])
        return {"ok": True, "message": r["text"], "risk": self.risk(), "tickets": self.tickets()}

    def cancel(self, broker_id):
        r = self.run("ticket", ["cancel", "--broker-id", broker_id], want_json=False)
        return {"ok": r["code"] == 0, "message": r["text"] or r["error"]}

    def news(self, symbols):
        key = ("news", tuple(symbols))
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        if self.demo:
            args = ["--offline", DEMO["news"], "--now", DEMO["news_now"], "--hours", "2000",
                    "--universe", os.path.join(DEMO["news"], "universe.txt"), "--tickers", "TSLA", "INFY", "AAPL", "KO"]
        else:
            args = ["--hours", "24"]
            if symbols:
                args += ["--tickers"] + symbols
        r = self.run("news", args + ["--all", "--dry-run", "--json"], timeout=240)
        data = r["data"] or {"items": [], "moves": [], "notes": [r["error"][-300:]] if r["error"] else []}
        self.cache[key] = (time.time(), data)
        return data

    def analyst(self, sym):
        if self.demo:
            if sym not in ("DEMO", "DEMOBANK"):
                sym = "DEMO"
            r = self.run("facts", [sym, "--offline", DEMO["analyst"], "--universe",
                                   os.path.join(DEMO["analyst"], "universe.txt"), "--json"])
        else:
            r = self.run("facts", [sym, "--json"], timeout=120)
        if r["data"] is None:
            raise ToolError((r["error"] or r["text"])[-400:])
        return r["data"][0] if isinstance(r["data"], list) and r["data"] else r["data"]

    # ------------------------------------------------ the book

    def book(self):
        cfg = self.config()
        pf = read_json(os.path.join("trader", "portfolio.json"), None)
        state = read_json(os.path.join("trader", "state", "broker.json"), {"orders": []}) or {"orders": []}
        plan = read_json(os.path.join("trader", "plan.json"), {}) or {}
        today = self.clock.ist().date().isoformat()
        positions = []
        syms = []
        if pf:
            for p in pf.get("positions", []):
                shares = float(p.get("shares", 0))
                cost = sum(float(l.get("cost", 0)) for l in p.get("lots", []))
                positions.append({"symbol": p.get("ticker"), "shares": shares,
                                  "avg": round(cost / shares, 2) if shares else None, "cost": round(cost, 2)})
                syms.append(p.get("ticker"))
        q = self.quotes(syms) if syms else {}
        unreal = 0.0
        for p in positions:
            price = (q.get(p["symbol"]) or {}).get("price")
            p["price"] = price
            if price and p["avg"]:
                p["pnl"] = round((price - p["avg"]) * p["shares"], 2)
                p["pnl_pct"] = round(100 * (price / p["avg"] - 1), 2)
                unreal += p["pnl"]
            if str(plan.get("symbol", "")).upper() == p["symbol"]:
                p["stop"], p["target"], p["flat_by"] = plan.get("stop"), plan.get("target"), plan.get("flat_by")
        trades = self.journal(state)
        realized_today = round(sum(t["pnl"] for t in trades if t["date"] == today), 2)
        entries_today = [r for r in state.get("orders", []) if str(r.get("date")) == today
                         and r.get("status") in ("placed", "paper") and r.get("kind") == "entry"]
        held = {p["symbol"] for p in positions}
        at_risk = round(sum(float(r.get("max_loss") or 0) for r in entries_today if r.get("symbol") in held), 2)
        log = [r for r in state.get("orders", []) if str(r.get("date")) == today]
        wins = [t for t in trades if t["pnl"] > 0]
        stats = {"trades": len(trades), "wins": len(wins),
                 "win_rate": round(100.0 * len(wins) / len(trades), 1) if trades else None,
                 "net": round(sum(t["pnl"] for t in trades), 2),
                 "avg_r": round(sum(t["r"] for t in trades if t["r"] is not None) /
                                max(1, len([t for t in trades if t["r"] is not None])), 2) if trades else None}
        eq, run_ = [], 0.0
        for t in trades:
            run_ += t["pnl"]
            eq.append(round(run_, 2))
        broker_positions = None
        if not self.demo and not cfg.get("paper", True) and fyers.load_token():
            try:
                broker_positions = fyers.positions()
            except fyers.FyersError as e:
                broker_positions = {"error": str(e)}
        return {"cash": pf.get("cash") if pf else cfg.get("budget"), "positions": positions,
                "unrealized": round(unreal, 2), "realized_today": realized_today,
                "day_pnl": round(unreal + realized_today, 2),
                "limits": {"trades": len(entries_today), "max_trades": cfg.get("max_trades_per_day"),
                           "at_risk": at_risk, "max_daily_loss": cfg.get("max_daily_loss_inr"),
                           "loss_today": round(max(0.0, -realized_today), 2)},
                "log": log[-30:], "journal": trades[-50:], "stats": stats, "equity": eq,
                "broker_positions": broker_positions}

    def journal(self, state):
        """Round trips from the fill log, oldest lots first: buy, then the sells that close it."""
        lots = collections.defaultdict(list)
        risk_by = {}
        trades = []
        for r in state.get("orders", []):
            if r.get("status") in ("placed", "paper") and r.get("kind") == "entry" and r.get("max_loss"):
                risk_by[(r.get("symbol"), str(r.get("date")))] = float(r["max_loss"])
            if r.get("status") != "filled":
                continue
            sym, qty, price = r.get("symbol"), float(r.get("qty") or 0), float(r.get("price") or 0)
            day = str(r.get("date") or str(r.get("at", ""))[:10])
            if r.get("side") == "buy":
                lots[sym].append([qty, price, day, r.get("at")])
                continue
            left, pnl, cost = qty, 0.0, 0.0
            opened = None
            while left > 1e-9 and lots[sym]:
                lot = lots[sym][0]
                take = min(left, lot[0])
                pnl += take * (price - lot[1])
                cost += take * lot[1]
                opened = opened or lot
                lot[0] -= take
                left -= take
                if lot[0] <= 1e-9:
                    lots[sym].pop(0)
            if opened:
                risk = risk_by.get((sym, opened[2]))
                trades.append({"symbol": sym, "date": day, "qty": qty, "entry": round(cost / max(qty, 1e-9), 2),
                               "exit": price, "pnl": round(pnl, 2), "opened": opened[3], "closed": r.get("at"),
                               "r": round(pnl / risk, 2) if risk else None})
        return trades

    # ------------------------------------------------ FYERS setup

    def fyers_setup(self, body):
        app_id = str(body.get("app_id", "")).strip()
        if not app_id:
            raise ToolError("the App ID from myapi.fyers.in, like ABCD1234-100")
        cfg = fyers.load_config()
        cfg.update({"app_id": app_id, "redirect_uri": str(body.get("redirect") or
                                                           "http://127.0.0.1:%d/fyers/callback" % self.port)})
        write_json(fyers.CONFIG, cfg)
        secret = str(body.get("secret") or "").strip()
        if secret:
            with open(fyers.SECRET_FILE, "w", encoding="utf-8") as fh:
                fh.write(secret + "\n")
            try:
                os.chmod(fyers.SECRET_FILE, 0o600)
            except OSError:
                pass
        return {"ok": True, "secret_saved": bool(secret)}

    def favorites_set(self, symbols):
        syms = []
        for s in symbols:
            s = str(s).upper().strip()
            if s and s not in syms:
                syms.append(s)
        with open(os.path.join("trader", "favorites.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(syms) + ("\n" if syms else ""))
        return {"favorites": syms}

    def snapshot(self):
        """What the stream pushes: the clock, the session and quotes for what is on screen."""
        st = self.status()
        wl = read_json(os.path.join("trader", "watchlist.json"), {}) or {}
        plan = read_json(os.path.join("trader", "plan.json"), {}) or {}
        pf = read_json(os.path.join("trader", "portfolio.json"), {}) or {}
        syms = list(st["favorites"])
        syms += [h.get("symbol") for h in (wl.get("hits") or [])[:12]]
        if plan.get("symbol"):
            syms.append(plan["symbol"])
        syms += [p.get("ticker") for p in pf.get("positions", [])]
        try:
            q = self.quotes([s for s in syms if s])
        except Exception as e:                       # the stream keeps going
            q = {"_error": str(e)}
        return {"status": st, "quotes": q}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "DayTraderDashboard/1"

    def log_message(self, fmt, *args):
        if os.environ.get("DASHBOARD_LOG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body, default=str) if ctype == "application/json"
                                                        else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "text" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._local_only():
            return self._send(403, {"error": "local only"})
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        app = self.app
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html")
            if u.path == "/api/status":
                return self._send(200, app.status())
            if u.path == "/api/chart":
                return self._send(200, app.chart(q.get("symbol", ""), q.get("range", "1d"), q.get("interval", "5m")))
            if u.path == "/api/quotes":
                return self._send(200, app.quotes(q.get("symbols", "").split(",")))
            if u.path == "/api/watchlist":
                return self._send(200, read_json(os.path.join("trader", "watchlist.json"), {}) or {})
            if u.path == "/api/state":
                return self._send(200, {
                    "entry": read_json(os.path.join("trader", "entry.json"), None),
                    "plan": read_json(os.path.join("trader", "plan.json"), None),
                    "timemachine": read_json(os.path.join("trader", "timemachine.json"), None),
                    "risk": read_json(os.path.join("trader", "state", "risk.json"), None),
                    "tickets": app.tickets()})
            if u.path == "/api/book":
                return self._send(200, app.book())
            if u.path == "/api/news":
                syms = [s for s in q.get("symbols", "").split(",") if s]
                return self._send(200, app.news(syms))
            if u.path == "/api/analyst":
                return self._send(200, app.analyst(q.get("symbol", "").upper()))
            if u.path == "/api/stream":
                return self.stream()
            if u.path == "/fyers/login":
                self.send_response(302)
                self.send_header("Location", fyers.login_url())
                self.end_headers()
                return
            if u.path == "/fyers/callback":
                try:
                    fyers.exchange_code(fyers.auth_code_from(self.path))
                    to = "/?fyers=ok"
                except fyers.FyersError as e:
                    to = "/?fyers_error=" + urllib.parse.quote(str(e))
                self.send_response(302)
                self.send_header("Location", to)
                self.end_headers()
                return
            return self._send(404, {"error": "not found"})
        except (ToolError, fyers.FyersError, scan_mod.ScanError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        if not self._local_only():
            return self._send(403, {"error": "local only"})
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return self._send(403, {"error": "local pages only"})
        u = urllib.parse.urlparse(self.path)
        b = self._body()
        app = self.app
        try:
            if u.path == "/api/scan":
                return self._send(200, app.scan(b.get("scope", "market")))
            if u.path == "/api/entry":
                return self._send(200, app.entry(str(b.get("symbol", "")).upper()))
            if u.path == "/api/plan":
                return self._send(200, app.plan(b))
            if u.path == "/api/timemachine":
                return self._send(200, app.timemachine())
            if u.path == "/api/size":
                return self._send(200, app.size(b))
            if u.path == "/api/risk":
                return self._send(200, {"risk": app.risk(), "tickets": app.tickets()})
            if u.path == "/api/approve":
                return self._send(200, app.approve(str(b.get("index", 1)), str(b.get("reply", ""))))
            if u.path == "/api/decline":
                return self._send(200, app.decline(str(b.get("index", 1))))
            if u.path == "/api/execute":
                return self._send(200, app.execute(str(b.get("index", 1)), b.get("confirm")))
            if u.path == "/api/protect":
                return self._send(200, app.protect(str(b.get("symbol", "")).upper()))
            if u.path == "/api/flatten":
                return self._send(200, app.flatten(str(b.get("symbol", "")).upper()))
            if u.path == "/api/cancel":
                return self._send(200, app.cancel(str(b.get("broker_id", ""))))
            if u.path == "/api/favorites":
                return self._send(200, app.favorites_set(b.get("symbols", [])))
            if u.path == "/api/fyers/setup":
                return self._send(200, app.fyers_setup(b))
            if u.path == "/api/clock":
                if not app.demo:
                    return self._send(400, {"error": "the clock only moves in demo mode"})
                app.clock.set(b.get("time"), b.get("playing"), b.get("speed"))
                return self._send(200, app.status())
            if u.path == "/api/demo/reset":
                if not app.demo:
                    return self._send(400, {"error": "demo only"})
                for f in ("watchlist.json", "entry.json", "plan.json", "timemachine.json", "orders.json",
                          "portfolio.json", "prices.json"):
                    p = os.path.join("trader", f)
                    if os.path.exists(p):
                        os.remove(p)
                shutil.rmtree(os.path.join("trader", "state"), ignore_errors=True)
                os.makedirs(os.path.join("trader", "state"), exist_ok=True)
                app.clock.set("09:50", playing=False)
                return self._send(200, app.status())
            return self._send(404, {"error": "not found"})
        except (ToolError, fyers.FyersError, scan_mod.ScanError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        app = self.app
        every = 1.0 if app.demo else (3.0 if app.source() == "fyers" else 10.0)
        try:
            while True:
                snap = app.snapshot()
                self.wfile.write(("data: %s\n\n" % json.dumps(snap, default=str)).encode("utf-8"))
                self.wfile.flush()
                time.sleep(every)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main(argv=None):
    ap = argparse.ArgumentParser(description="The day-trader dashboard (local, standard library only).")
    ap.add_argument("--demo", action="store_true", help="replay 2026-10-19 from the test data")
    ap.add_argument("--workdir", help="your trading folder, with trader/ in it (default: here; demo: ./trader-demo)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    workdir = a.workdir or ("trader-demo" if a.demo else ".")
    app = App(workdir, a.demo, a.port)
    Handler.app = app
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    url = "http://127.0.0.1:%d/" % a.port
    print("Day-trader dashboard on %s (%s, working folder %s). Ctrl+C stops it." % (
        url, "DEMO replay of 2026-10-19" if a.demo else "live data", app.workdir))
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
