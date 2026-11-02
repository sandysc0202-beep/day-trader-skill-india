#!/usr/bin/env python3
"""FYERS API v3 for the day-trader pack. Standard library only.

Two jobs:

1. The broker. Login (FYERS' auth-code flow), funds, positions, the order
   book, quotes, and the two order calls the pack makes: place one approved
   order and cancel one order. ticket.py is the only caller of place_order,
   and only for an order you approved with an explicit yes.

2. The data. FYERS' history and quotes, handed to the other tools in the
   exact shape of Yahoo's chart answer, so the scanner, entry timer, exit
   planner, time machine, alerts and news read official NSE bars without a
   change to their parsing. See yahoo_compat() and data_source().

    python3 fyers_api.py setup --app-id ABCD1234-100 --redirect http://127.0.0.1:8765/fyers/callback
    python3 fyers_api.py login                 # prints the FYERS login link
    python3 fyers_api.py token "<the URL FYERS sent you back to>"   # or just the auth_code
    python3 fyers_api.py status                # is today's token there?
    python3 fyers_api.py quote RELIANCE INFY
    python3 fyers_api.py history RELIANCE --resolution 5 --days 5
    python3 fyers_api.py positions | orders | funds

Keys. trader/fyers.json holds your app id and redirect URL (not secret).
The app SECRET is read from the FYERS_SECRET environment variable, or from
trader/fyers.secret (a one-line file, keep it out of git, the pack's
.gitignore already ignores trader/). Never paste it into a chat. Today's
access token is saved in trader/state/fyers_token.json; FYERS expires every
token at 6:30 AM IST, so you log in once a trading day.

Endpoints are FYERS API v3 (https://fyers.in/api-docs/). The free plan
allows 5 data calls a second and 50 a minute, and FYERS blocks the API for
the rest of the day after the third minute over; this module paces itself
under that (4 a second, 45 a minute), counted across every tool that runs.

SEBI's rules (from 1 April 2026) accept API orders only from a static IP
you registered with FYERS. Data calls do not need it.

Education and tooling, not financial advice.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api-t1.fyers.in/api/v3"
DATA = "https://api-t1.fyers.in/data"
CONFIG = os.path.join("trader", "fyers.json")
SECRET_FILE = os.path.join("trader", "fyers.secret")
TOKEN = os.path.join("trader", "state", "fyers_token.json")
UA = "day-trader-skill-india/2 (+https://github.com/sandysc0202-beep/day-trader-skill-india)"

# FYERS order enums (Single Order API)
ORDER_TYPES = {"limit": 1, "market": 2, "stop": 3, "stop_limit": 4}
SIDES = {"buy": 1, "sell": -1}
PRODUCTS = ("INTRADAY", "CNC")          # equity products this pack sends (MTF and MARGIN are not used)
VALIDITY = {"day": "DAY", "ioc": "IOC"}
# Order book status codes
STATUS = {1: "cancelled", 2: "filled", 3: "unused", 4: "transit", 5: "rejected", 6: "pending", 7: "expired"}

# Yahoo interval -> FYERS resolution
RESOLUTION = {"1m": "1", "2m": "2", "3m": "3", "5m": "5", "10m": "10", "15m": "15", "30m": "30",
              "60m": "60", "1h": "60", "90m": "60", "1d": "D", "5d": "D", "1wk": "1W", "1mo": "1M"}
MAX_DAYS = {"intraday": 100, "daily": 366}   # per request, from the History API

IST = datetime.timedelta(hours=5, minutes=30)
TOKEN_RESET = datetime.time(6, 30)           # FYERS expires tokens at 6:30 AM IST

SLEEP = time.sleep
_calls = []                                  # monotonic times of data calls in this process


class FyersError(Exception):
    pass


# ---------------------------------------------------------------- symbols and time

def fyers_symbol(sym):
    """RELIANCE -> NSE:RELIANCE-EQ (also accepts RELIANCE.NS and NSE:RELIANCE-EQ)."""
    s = str(sym).strip().upper()
    if s.startswith("NSE:"):
        return s
    if s.endswith(".NS"):
        s = s[:-3]
    return "NSE:%s-EQ" % s


def plain_symbol(fsym):
    """NSE:RELIANCE-EQ -> RELIANCE."""
    s = str(fsym).strip().upper()
    if s.startswith("NSE:"):
        s = s[4:]
    return s[:-3] if s.endswith("-EQ") else s


def _utc(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).replace(tzinfo=None)


def ist_now(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc + IST


def token_day(ist):
    """The FYERS token day a moment belongs to: before 6:30 AM it is still yesterday's token."""
    return (ist.date() if ist.time() >= TOKEN_RESET else ist.date() - datetime.timedelta(days=1)).isoformat()


# ---------------------------------------------------------------- config and token

def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_config(path=CONFIG):
    cfg = _read(path) or {}
    if os.environ.get("FYERS_APP_ID"):
        cfg["app_id"] = os.environ["FYERS_APP_ID"]
    return cfg


def load_secret(path=SECRET_FILE):
    s = os.environ.get("FYERS_SECRET")
    if s:
        return s.strip()
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def load_token(path=TOKEN, now_utc=None):
    """Today's token dict, or None when there is none or it expired at 6:30 AM."""
    tok = _read(path)
    if not tok or not tok.get("access_token"):
        return None
    if tok.get("day") != token_day(ist_now(now_utc)):
        return None
    return tok


def data_source(now_utc=None):
    """'fyers' when FYERS is the data source, else 'yahoo'.

    TRADER_DATA_SOURCE=yahoo or =fyers decides; otherwise FYERS is used when today's
    token is saved (you logged in this morning), Yahoo when it is not."""
    want = (os.environ.get("TRADER_DATA_SOURCE") or "auto").lower()
    if want in ("yahoo", "fyers"):
        return want
    return "fyers" if load_token(now_utc=now_utc) else "yahoo"


def auth_header(cfg=None, tok=None):
    cfg = cfg or load_config()
    tok = tok or load_token()
    if not cfg.get("app_id"):
        raise FyersError("no FYERS app id: run fyers_api.py setup --app-id ... (or set FYERS_APP_ID)")
    if not tok:
        raise FyersError("no FYERS token for today (they expire at 6:30 AM IST): run fyers_api.py login")
    return "%s:%s" % (cfg["app_id"], tok["access_token"])


# ---------------------------------------------------------------- HTTP

_urlopen = urllib.request.urlopen     # tests swap this


PACE_FILE = os.path.join("trader", "state", "fyers_calls.json")
PER_MINUTE, PER_SECOND = 45, 4                # FYERS' free plan allows 50 and 5


def _wait(calls, now):
    """Seconds to wait before one more call, given the wall-clock times of recent calls."""
    calls = [t for t in calls if now - t < 60]
    wait = 0.0
    if len(calls) >= PER_MINUTE:
        wait = max(wait, 60 - (now - calls[-PER_MINUTE]) + 0.05)
    recent = [t for t in calls if now - t < 1]
    if len(recent) >= PER_SECOND:
        wait = max(wait, 1 - (now - recent[-PER_SECOND]) + 0.05)
    return wait, calls


def _pace():
    """Stay under the free plan: 4 calls a second, 45 a minute, counted across every tool.

    The dashboard runs the scanner, the entry timer and the time machine as separate
    programs, and FYERS blocks the API for the day after the third minute over the
    limit, so the count lives in trader/state/fyers_calls.json behind a file lock.
    Without a trader/state folder (or file locks), it is counted in this program only."""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    folder = os.path.dirname(PACE_FILE)
    if fcntl is None or not os.path.isdir(folder):
        wait, kept = _wait(_calls, time.time())
        if wait > 0:
            SLEEP(wait)
        _calls[:] = kept + [time.time()]
        return
    with open(PACE_FILE + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            calls = _read(PACE_FILE) or []
            if not isinstance(calls, list):
                calls = []
            wait, calls = _wait(calls, time.time())
            if wait > 0:
                SLEEP(wait)
            calls.append(time.time())
            _calls[:] = calls
            tmp = PACE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(calls, fh)
            os.replace(tmp, PACE_FILE)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def request(method, url, body=None, auth=None, data_call=False, timeout=20):
    """One FYERS call -> parsed JSON. Raises FyersError with FYERS' own message."""
    if data_call:
        _pace()
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if auth:
        headers["Authorization"] = auth
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with _urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        finally:
            e.close()
        if not text.strip().startswith("{"):
            raise FyersError("FYERS answered HTTP %d for %s" % (e.code, url.split("?")[0]))
    except (urllib.error.URLError, OSError) as e:
        raise FyersError("could not reach FYERS (%s)" % getattr(e, "reason", e))
    try:
        doc = json.loads(text)
    except ValueError:
        raise FyersError("FYERS sent something that is not JSON for %s" % url.split("?")[0])
    if isinstance(doc, dict) and doc.get("s") not in (None, "ok"):
        code = doc.get("code")
        msg = doc.get("message") or "error"
        if code in (-8, -15, -16, -17):
            msg += " (the token is invalid or expired: run fyers_api.py login)"
        if code == 429 or code == -429:
            msg += " (rate limit: wait a minute; three minutes over blocks the API for the day)"
        raise FyersError("FYERS: %s [code %s]" % (msg, code))
    return doc


# ---------------------------------------------------------------- login

def login_url(cfg=None, state="daytrader"):
    cfg = cfg or load_config()
    if not cfg.get("app_id") or not cfg.get("redirect_uri"):
        raise FyersError("run fyers_api.py setup --app-id APPID --redirect URL first")
    q = urllib.parse.urlencode({"client_id": cfg["app_id"], "redirect_uri": cfg["redirect_uri"],
                                "response_type": "code", "state": state})
    return "%s/generate-authcode?%s" % (API, q)


def auth_code_from(text):
    """The auth_code from the URL FYERS redirected to (or the bare code)."""
    t = (text or "").strip()
    if "auth_code=" in t or "://" in t or "?" in t:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(t).query)
        code = (q.get("auth_code") or [""])[0]
    else:
        code = t
    if not code or " " in code:
        raise FyersError("no auth_code in that: paste the whole URL FYERS sent you back to")
    return code


def app_id_hash(app_id, secret):
    return hashlib.sha256(("%s:%s" % (app_id, secret)).encode("utf-8")).hexdigest()


def exchange_code(code, cfg=None, secret=None, path=TOKEN, now_utc=None):
    """auth_code -> today's access token, saved to trader/state/fyers_token.json."""
    cfg = cfg or load_config()
    secret = secret or load_secret()
    if not cfg.get("app_id"):
        raise FyersError("no app id: run fyers_api.py setup first")
    if not secret:
        raise FyersError("no app secret: set FYERS_SECRET, or put it in %s (never in the chat)" % SECRET_FILE)
    doc = request("POST", API + "/validate-authcode",
                  {"grant_type": "authorization_code", "appIdHash": app_id_hash(cfg["app_id"], secret),
                   "code": code})
    if not doc.get("access_token"):
        raise FyersError("FYERS did not return an access token")
    ist = ist_now(now_utc)
    tok = {"access_token": doc["access_token"], "day": token_day(ist),
           "saved_at": ist.replace(microsecond=0).isoformat() + "+05:30"}
    _write(path, tok)
    return tok


# ---------------------------------------------------------------- account

def profile():
    return request("GET", API + "/profile", auth=auth_header()).get("data") or {}


def funds():
    return request("GET", API + "/funds", auth=auth_header()).get("fund_limit") or []


def positions():
    doc = request("GET", API + "/positions", auth=auth_header())
    return doc.get("netPositions") or []


def orderbook():
    doc = request("GET", API + "/orders", auth=auth_header())
    out = []
    for o in doc.get("orderBook") or []:
        o = dict(o)
        o["status_word"] = STATUS.get(o.get("status"), str(o.get("status")))
        out.append(o)
    return out


# ---------------------------------------------------------------- orders

def order_payload(order, tag="daytrader"):
    """The pack's order (trader/orders.json) -> the FYERS Single Order body.

    A bracket is sent as the entry only: the stop-loss follows as its own SL-M order
    (ticket.py protect) once the entry fills, and trader-alerts watches the target."""
    typ = str(order.get("type", "market")).lower()
    if typ not in ORDER_TYPES:
        raise FyersError("FYERS has no %r order type" % typ)
    side = str(order.get("side", "")).lower()
    if side not in SIDES:
        raise FyersError("side must be buy or sell")
    prod = str(order.get("product") or "INTRADAY").upper()
    if prod not in PRODUCTS:
        raise FyersError("product must be INTRADAY or CNC for NSE shares")
    tif = VALIDITY.get(str(order.get("time_in_force") or "day").lower())
    if not tif:
        raise FyersError("validity must be day or ioc")
    qty = float(order.get("qty") or 0)
    if qty <= 0 or qty != int(qty):
        raise FyersError("qty must be whole shares")
    body = {"symbol": fyers_symbol(order.get("symbol", "")), "qty": int(qty), "type": ORDER_TYPES[typ],
            "side": SIDES[side], "productType": prod, "limitPrice": 0, "stopPrice": 0,
            "validity": tif, "disclosedQty": 0, "offlineOrder": False,
            "orderTag": re.sub(r"[^A-Za-z0-9]", "", tag)[:30] or "daytrader"}
    if typ in ("limit", "stop_limit"):
        body["limitPrice"] = round(float(order["limit_price"]), 2)
    if typ in ("stop", "stop_limit"):
        body["stopPrice"] = round(float(order["stop_price"]), 2)
    return body


def place_order(body):
    """POST one order. Only ticket.py calls this, after your recorded yes."""
    doc = request("POST", API + "/orders/sync", body, auth=auth_header())
    return {"id": str(doc.get("id") or ""), "message": doc.get("message", "")}


def cancel_order(order_id):
    doc = request("DELETE", API + "/orders/sync", {"id": str(order_id)}, auth=auth_header())
    return {"id": str(doc.get("id") or order_id), "message": doc.get("message", "")}


# ---------------------------------------------------------------- data

def quotes(symbols):
    """{SYM: {price, prev_close, change, pct, open, high, low, volume, time}} in batches of 50."""
    out = {}
    syms = [s for s in symbols if s]
    for i in range(0, len(syms), 50):
        batch = syms[i:i + 50]
        q = urllib.parse.quote(",".join(fyers_symbol(s) for s in batch), safe=":,-")
        doc = request("GET", "%s/quotes?symbols=%s" % (DATA, q), auth=auth_header(), data_call=True)
        for item in doc.get("d") or []:
            v = item.get("v") or {}
            if item.get("s") not in (None, "ok") or v.get("lp") is None:
                continue
            out[plain_symbol(item.get("n") or v.get("symbol") or "")] = {
                "price": v.get("lp"), "prev_close": v.get("prev_close_price"), "change": v.get("ch"),
                "pct": v.get("chp"), "open": v.get("open_price"), "high": v.get("high_price"),
                "low": v.get("low_price"), "volume": v.get("volume"), "time": int(v.get("tt") or 0) or None}
    return out


def history(sym, resolution, start, end):
    """Candles [[epoch, o, h, l, c, v], ...] from start to end (dates, IST), in as many
    requests as FYERS' per-request limit needs (100 days intraday, 366 daily)."""
    step = MAX_DAYS["daily" if resolution in ("D", "1D", "1W", "1M") else "intraday"]
    candles, a = [], start
    while a <= end:
        b = min(end, a + datetime.timedelta(days=step - 1))
        q = urllib.parse.urlencode({"symbol": fyers_symbol(sym), "resolution": resolution, "date_format": 1,
                                    "range_from": a.isoformat(), "range_to": b.isoformat(), "cont_flag": 1})
        doc = request("GET", "%s/history?%s" % (DATA, q), auth=auth_header(), data_call=True)
        candles.extend(doc.get("candles") or [])
        a = b + datetime.timedelta(days=1)
    seen, out = set(), []
    for c in sorted(candles, key=lambda c: c[0]):
        if c[0] not in seen:
            seen.add(c[0])
            out.append(c)
    return out


# ---------------------------------------------------------------- the Yahoo-shaped bridge

def _range_days(rng):
    m = re.match(r"^(\d+)(d|wk|mo|y)$", rng or "")
    if not m:
        return None, None
    n, unit = int(m.group(1)), m.group(2)
    return n, unit


def window(rng, interval, today):
    """(start, end, keep_sessions) for a Yahoo range: '5d' keeps the last 5 sessions."""
    n, unit = _range_days(rng)
    if n is None:
        raise FyersError("range %r is not one this bridge knows" % rng)
    if unit == "d":
        return today - datetime.timedelta(days=int(n * 1.6) + 7), today, n
    days = {"wk": 7, "mo": 31, "y": 366}[unit] * n
    return today - datetime.timedelta(days=days), today, None


def chart_doc(sym, rng, interval, now_utc=None, period=None):
    """One symbol as Yahoo's v8 chart answer: {"chart": {"result": [{meta, timestamp, indicators}]}}."""
    res = RESOLUTION.get(interval)
    if not res:
        raise FyersError("interval %r is not one FYERS has" % interval)
    today = ist_now(now_utc).date()
    if period:
        start = (_utc(period[0]) + IST).date()
        end = (_utc(period[1]) + IST).date()
        keep = None
    else:
        start, end, keep = window(rng, interval, today)
    candles = history(sym, res, start, end)
    if keep:
        days = sorted({(_utc(c[0]) + IST).date() for c in candles})[-keep:]
        first = days[0] if days else today
        candles = [c for c in candles if (_utc(c[0]) + IST).date() >= first]
    q = quotes([sym]).get(plain_symbol(fyers_symbol(sym)), {})
    last = candles[-1] if candles else None
    meta = {"currency": "INR", "symbol": plain_symbol(fyers_symbol(sym)) + ".NS", "exchangeName": "NSI",
            "instrumentType": "EQUITY", "dataGranularity": interval, "range": rng or "",
            "exchangeTimezoneName": "Asia/Kolkata", "timezone": "IST", "gmtoffset": 19800,
            "regularMarketPrice": q.get("price", last[4] if last else None),
            "regularMarketTime": q.get("time") or (last[0] if last else None),
            "regularMarketDayHigh": q.get("high"), "regularMarketDayLow": q.get("low"),
            "regularMarketVolume": q.get("volume"),
            "chartPreviousClose": q.get("prev_close"), "previousClose": q.get("prev_close"),
            "source": "fyers"}
    return {"chart": {"result": [{
        "meta": meta, "timestamp": [c[0] for c in candles],
        "indicators": {"quote": [{"open": [c[1] for c in candles], "high": [c[2] for c in candles],
                                  "low": [c[3] for c in candles], "close": [c[4] for c in candles],
                                  "volume": [c[5] for c in candles]}]}}], "error": None}}


SPARK_LIMIT = 40   # one history call per symbol: more than this would hit the 50-a-minute limit


def spark_doc(symbols, rng, interval, now_utc=None):
    """Several symbols as Yahoo's v7 spark answer."""
    if len(symbols) > SPARK_LIMIT:
        raise FyersError("FYERS' free plan allows 50 data calls a minute: scan up to %d symbols with FYERS "
                         "(a watchlist), or the whole market with Yahoo (TRADER_DATA_SOURCE=yahoo)" % SPARK_LIMIT)
    out = []
    for s in symbols:
        doc = chart_doc(s, rng, interval, now_utc)
        r = doc["chart"]["result"][0]
        out.append({"symbol": r["meta"]["symbol"], "response": [r]})
    return {"spark": {"result": out, "error": None}}


def yahoo_compat(path_or_url, now_utc=None):
    """A Yahoo chart or spark URL (or path) -> the same answer, as JSON text, from FYERS."""
    u = urllib.parse.urlparse(path_or_url)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
    rng, interval = q.get("range", "1d"), q.get("interval", "5m")
    period = (int(q["period1"]), int(q["period2"])) if "period1" in q and "period2" in q else None
    if "/v8/finance/chart/" in u.path:
        sym = urllib.parse.unquote(u.path.rsplit("/", 1)[1])
        return json.dumps(chart_doc(sym, rng, interval, now_utc, period))
    if "/finance/spark" in u.path:
        syms = [urllib.parse.unquote(s) for s in q.get("symbols", "").split(",") if s]
        return json.dumps(spark_doc(syms, rng, interval, now_utc))
    raise FyersError("not a Yahoo chart or spark address: %s" % u.path)


# ---------------------------------------------------------------- CLI

def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(description="FYERS API v3 for the day-trader pack (login, data, account).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("setup", help="save your app id and redirect URL to trader/fyers.json")
    p.add_argument("--app-id", required=True, help="from myapi.fyers.in, like ABCD1234-100")
    p.add_argument("--redirect", required=True, help="the redirect URL you gave the app")
    sub.add_parser("login", help="print the FYERS login link")
    p = sub.add_parser("token", help="exchange the auth_code (or the whole redirect URL) for today's token")
    p.add_argument("code")
    sub.add_parser("status", help="is today's token saved?")
    p = sub.add_parser("quote", help="latest quotes")
    p.add_argument("symbols", nargs="+")
    p = sub.add_parser("history", help="candles")
    p.add_argument("symbol")
    p.add_argument("--resolution", default="5")
    p.add_argument("--days", type=int, default=5)
    sub.add_parser("positions")
    sub.add_parser("orders")
    sub.add_parser("funds")
    sub.add_parser("profile")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        if a.cmd == "setup":
            cfg = load_config()
            cfg.update({"app_id": a.app_id.strip(), "redirect_uri": a.redirect.strip()})
            _write(CONFIG, cfg)
            print("Saved %s. Put the app secret in FYERS_SECRET or %s (never in the chat), then: "
                  "fyers_api.py login" % (CONFIG, SECRET_FILE))
        elif a.cmd == "login":
            print("Open this link, log in to FYERS, then run: fyers_api.py token \"<the URL you land on>\"")
            print(login_url())
        elif a.cmd == "token":
            tok = exchange_code(auth_code_from(a.code))
            print("Saved today's FYERS token (valid until 6:30 AM IST tomorrow, day %s)." % tok["day"])
        elif a.cmd == "status":
            tok = load_token()
            print("FYERS token for today: %s. Data source: %s." % ("yes" if tok else "no (run login)",
                                                                   data_source()))
            return 0 if tok else 1
        elif a.cmd == "quote":
            _print(quotes(a.symbols))
        elif a.cmd == "history":
            end = ist_now().date()
            _print(history(a.symbol, a.resolution, end - datetime.timedelta(days=a.days), end))
        elif a.cmd == "positions":
            _print(positions())
        elif a.cmd == "orders":
            _print(orderbook())
        elif a.cmd == "funds":
            _print(funds())
        elif a.cmd == "profile":
            _print(profile())
        return 0
    except FyersError as e:
        print("fyers_api.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
