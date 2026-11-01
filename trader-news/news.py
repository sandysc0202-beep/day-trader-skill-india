#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines and NSE filings for the stocks you watch or hold and prints
only what is new since the last check, newest first, with how far each stock
has moved today and a flag on anything that touches your symbols.

    python3 news.py                                  # watchlist + plan + portfolio symbols
    python3 news.py --tickers RELIANCE INFY M&M
    python3 news.py --tickers RELIANCE --all --hours 72   # everything in 3 days, seen or not
    python3 news.py --no-filings                     # headlines only
    python3 news.py --no-moves                       # skip the price line per symbol

Your symbols, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json.

Sources:
  - Yahoo Finance headline RSS, one request per symbol (NSE symbols get .NS):
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>.NS&region=IN&lang=en-IN
  - NSE's own RSS feeds of company filings, one request per feed for all your
    symbols (announcements and financial results by default; --feeds adds
    board meetings and corporate actions):
    https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
    https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml
    The feeds name the company, not the symbol, so a filing is matched to your
    symbol by the company name in trader/universe.txt (written by
    trader-scanner's `scan.py universe`, NSE's own list) or in your watchlist.
    No universe file, no name: the tool says so and shows headlines only.
    The feeds hold only the latest filings: check often in the session.
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?range=1d&interval=5m

Times print in India time (IST). State: trader/state/news.json remembers what
you have seen (30 days), so an item shows once. Offline: --offline DIR reads
yahoo-<SYMBOL>.xml, chart-<SYMBOL>.json and nse-<feed>.xml from DIR. The tests
use it.

Headlines are leads, not facts: open the story or the filing before acting,
and treat nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
NSE_RSS = "https://nsearchives.nseindia.com/content/RSS/"
NSE_FEEDS = {"announcements": ("Online_announcements.xml", "announcement"),
             "results": ("Financial_Results.xml", "financial results"),
             "board": ("Board_Meetings.xml", "board meeting"),
             "actions": ("Corporate_action.xml", "corporate action")}
DEFAULT_FEEDS = ("announcements", "results")
NSE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/126.0 Safari/537.36")  # NSE's archive turns away requests that do not look like a browser
BROWSER_UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving

# an NSE filing whose text says one of these is flagged "!": results, the board, money and trouble
IMPORTANT = re.compile(r"financial results?|outcome of (?:the )?board meeting|resignation|insolvency|\bdefault|"
                       r"\bfraud|credit rating|delist|suspension|amalgamation|\bmerger|acquisition|buy-?back|"
                       r"\bdividend|\bbonus|stock split|sub-division|\bpledge", re.I)
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bforecast\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b|\bunderperform\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEBI order\b|\bshow[- ]cause\b|\bCCI\b|"
              r"\bNCLT\b|\bjudge\b|\bcourt\b|\btribunal\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b"),
    ("capital", r"\bdividend\b|\bbuy-?back\b|\brepurchase\b|\bstock split\b|\bbonus\b|\brights issue\b|"
                r"\bQIP\b|\bOFS\b|\boffering\b|\bblock deal\b|\bbulk deal\b|\bpledge\b|\bpromoter\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bresign|\bsteps down\b|\bappoint"),
    ("risk", r"\brecall\b|\bbankrupt|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)|\b(?:upper|lower) circuit\b"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|\bUSFDA\b|\bFDA\b|\bapproval\b|\bapproves\b|\bclinical\b|\blicen[cs]e\b"),
    ("macro", r"\brepo rate\b|\bMPC\b|\bCPI\b|\binflation\b|\bGDP\b|\bGST\b|\bUnion Budget\b|\btariffs?\b|"
              r"\bFIIs?\b|\bFPIs?\b|\bcrude\b|\bFed\b|\bfutures\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

IST_OFFSET = _dt.timedelta(hours=5, minutes=30)  # India Standard Time: UTC+5:30 all year, no daylight saving


def to_ist(utc):
    """UTC (naive or aware) -> naive India wall-clock time."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    return utc.replace(tzinfo=None) + IST_OFFSET


def ist_to_utc(local):
    """Naive India time -> naive UTC."""
    return local - IST_OFFSET


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{ticker: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


def names_from_universe(path="trader/universe.txt"):
    """{SYMBOL: company name} from trader-scanner's universe file (SYMBOL|Name a line)."""
    out = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "|" in ln:
                    sym, name = ln.split("|", 1)
                    out[sym.strip().upper()] = name.strip()
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.15):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, nse=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": NSE_UA if nse else BROWSER_UA, "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


def yahoo_symbol(ticker):
    """NSE symbol -> Yahoo's: RELIANCE -> RELIANCE.NS (M&M stays M&M, quoted in the URL)."""
    ticker = ticker.upper().strip()
    return ticker if ticker.endswith(".NS") else ticker + ".NS"


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS), ISO 8601, or NSE's "01-Nov-2026 02:05:35" (India time) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$", value)
    if m:
        try:
            local = _dt.datetime.strptime("%s-%s-%s %s:%s:%s" % (m.group(1), m.group(2).title(), m.group(3),
                                                                 m.group(4), m.group(5), m.group(6) or "00"),
                                          "%d-%b-%Y %H:%M:%S")
        except ValueError:
            return None
        return ist_to_utc(local).replace(tzinfo=_dt.timezone.utc)
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def norm_name(name):
    """A company name for matching: 'Mahindra & Mahindra Ltd.' and 'MAHINDRA AND MAHINDRA LIMITED' agree."""
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return " ".join(w for w in n.split() if w not in ("limited", "ltd", "the"))


def parse_nse_rss(data, feed):
    """One of NSE's RSS feeds -> [{company, text, link, time, feed}]. Each item's title is the company."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("NSE %s feed is not valid XML: %s" % (feed, exc))
    out = []
    for it in root.iter("item"):
        company = " ".join(_text(it, "title").split())
        if not company:
            continue
        out.append({"company": company, "text": " ".join(_text(it, "description").split()),
                    "link": _text(it, "link"), "time": parse_time(_text(it, "pubDate")), "feed": feed})
    return out


def nse_items(raw_items, names):
    """Raw NSE items -> news items for the symbols whose company name matches exactly (normalized)."""
    by_name = {}
    for sym, name in names.items():
        if name:
            by_name.setdefault(norm_name(name), []).append(sym)
    out = []
    for r in raw_items:
        for sym in by_name.get(norm_name(r["company"]), []):
            label = NSE_FEEDS.get(r["feed"], ("", r["feed"]))[1]
            text = r["text"]
            short = text if len(text) <= 140 else text[:137].rsplit(" ", 1)[0] + "..."
            title = "%s, NSE %s%s" % (r["company"], label, (": " + short) if short else "")
            stamp = r["time"].strftime("%Y%m%d%H%M%S") if r["time"] else ""
            out.append({"id": "nse:%s:%s:%s" % (r["feed"], r["link"] or r["company"], stamp),
                        "source": "NSE", "kind": "filing", "form": label, "ticker": sym, "title": title,
                        "summary": text, "link": r["link"], "time": r["time"],
                        "important": r["feed"] == "results" or bool(IMPORTANT.search(text))})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def fyers_bridge():
    """trader-broker/fyers_api.py when FYERS is today's data source (you logged in to FYERS
    this morning, or TRADER_DATA_SOURCE=fyers), else None and Yahoo it is. FYERS' bars come
    back in Yahoo's chart shape, so nothing else changes."""
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


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            fy = None if fetcher.offline else fyers_bridge()
            if fy:
                try:
                    out.append(parse_chart(fy.yahoo_compat(CHART % ("query2", urllib.parse.quote(
                        yahoo_symbol(t), safe="-."))), t))
                except fy.FyersError as exc:
                    errors.append("price for %s: %s" % (t, exc))
                continue
            try:
                data = fetcher.get(CHART % ("query2", urllib.parse.quote(yahoo_symbol(t), safe="-.")), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", urllib.parse.quote(yahoo_symbol(t), safe="-.")), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\b(?:NSE|BSE)\s*:\s*%s\b" % re.escape(t),
                r"\b%s\.NS\b" % re.escape(t)]
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "filing" and it.get("form") == "financial results" and "earnings" not in it["tags"]:
            it["tags"].insert(0, "earnings")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, feeds=DEFAULT_FEEDS, names=None, errors=None):
    """Headlines per symbol, then each NSE feed once, matched to your symbols by company name."""
    errors = [] if errors is None else errors
    items = []
    for t in sorted(watch):
        try:
            url = YAHOO % urllib.parse.quote(yahoo_symbol(t), safe="-.")
            items += parse_rss(fetcher.get(url, "yahoo-%s.xml" % t), t)
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("headlines for %s: %s" % (t, exc))
    if feeds:
        names = dict(names or {})
        missing = sorted(t for t in watch if not names.get(t))
        if missing:
            errors.append("NSE filings skipped for %s: no company name to match them by. Run trader-scanner's "
                          "`scan.py universe` (it writes trader/universe.txt)" % ", ".join(missing))
        known = {t: names[t] for t in watch if names.get(t)}
        if known:
            for feed in feeds:
                fname, _ = NSE_FEEDS[feed]
                try:
                    data = fetcher.get(NSE_RSS + fname, "nse-%s.xml" % feed, nse=True)
                    items += nse_items(parse_nse_rss(data, feed), known)
                except NewsError as exc:
                    errors.append("NSE %s feed: %s" % (feed, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-6s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]),
            when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--feeds", default=",".join(DEFAULT_FEEDS),
                    help="NSE feeds, comma separated: %s" % ", ".join(sorted(NSE_FEEDS)))
    ap.add_argument("--universe", default="trader/universe.txt", help="SYMBOL|Name list for company names")
    ap.add_argument("--no-filings", "--no-sec", dest="no_filings", action="store_true", help="headlines only")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.tickers:
        watch = {t.upper(): None for t in args.tickers}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no symbols. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    state = load_json(args.state) or {}
    errors = []
    feeds = [] if args.no_filings else [f.strip().lower() for f in args.feeds.split(",") if f.strip()]
    for f in feeds:
        if f not in NSE_FEEDS:
            print("news: --feeds knows %s, not %r" % (", ".join(sorted(NSE_FEEDS)), f), file=sys.stderr)
            return 2
    names = names_from_universe(args.universe)
    names.update({t: n for t, n in watch.items() if n})
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, state, feeds=feeds, names=names, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
