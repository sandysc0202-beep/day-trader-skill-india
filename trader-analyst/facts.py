#!/usr/bin/env python3
"""Earnings reader for trader-analyst. Standard library only, no keys.

Pulls an NSE company's reported quarterly numbers and lays out what changed,
quarter by quarter: revenue, gross, operating and net margins, net profit,
diluted EPS, and growth against the same quarter a year earlier. It also finds
the company's latest results filing in NSE's own feed.

    python3 facts.py RELIANCE
    python3 facts.py RELIANCE INFY --quarters 5
    python3 facts.py INFY --json
    python3 facts.py RELIANCE INFY TCS --metrics trader/metrics.json

Sources:
  - Yahoo Finance's fundamentals time series (free, unofficial, can lag or
    change without notice; NSE symbols get .NS):
    https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/<SYMBOL>.NS
        ?type=quarterlyTotalRevenue,quarterlyCostOfRevenue,quarterlyGrossProfit,
              quarterlyOperatingIncome,quarterlyNetIncome,quarterlyDilutedEPS&period1=..&period2=..
    Yahoo keeps about five quarters, enough for one year-on-year comparison.
  - NSE's financial results feed, for the newest filing and its link:
    https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml
    matched by the company name in trader/universe.txt (trader-scanner writes it).
    The feed holds only the latest filings, so an older one may not be there.

Indian companies file results every quarter, the fourth quarter (January to
March) too, under SEBI's listing rules, so there is no derived quarter here.
Quarters are labelled by India's financial year (April to March): the quarter
ending 30 September 2026 is Q2 FY27. Amounts print in rupees crore
(1 crore = 10 million). Banks and insurers have no gross profit: blank.

The numbers are Yahoo's reading of the filings. For anything that matters,
open the filing on NSE and check the line yourself.

--metrics FILE merges one row per symbol into a JSON file (for notes or a
sheet). Offline: --offline DIR reads timeseries-<SYMBOL>.json and
nse-results.xml from DIR.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime as _dt
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

TS_URL = ("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s"
          "?symbol=%s&type=%s&period1=%d&period2=%d")
RESULTS_URL = "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml"
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
NSE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/126.0 Safari/537.36")
CRORE = 1e7
SERIES = {  # our key -> Yahoo's quarterly series name
    "revenue": "quarterlyTotalRevenue",
    "cost_of_revenue": "quarterlyCostOfRevenue",
    "gross_profit": "quarterlyGrossProfit",
    "operating_income": "quarterlyOperatingIncome",
    "net_income": "quarterlyNetIncome",
    "eps_diluted": "quarterlyDilutedEPS",
}
SOURCE = "Yahoo Finance fundamentals (unofficial)"


class FactsError(Exception):
    pass


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.3):
        self.offline, self.pause, self._last = offline, pause, 0.0

    def get(self, url, name, nse=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise FactsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": NSE_UA if nse else UA,
                                                   "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise FactsError("HTTP %s for %s: Yahoo is asking for a browser cookie on this endpoint. "
                                 "Read the numbers from the filing on nseindia.com instead"
                                 % (exc.code, url.split("?")[0]))
            raise FactsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except urllib.error.URLError as exc:
            raise FactsError("could not reach %s: %s" % (url.split("/")[2], exc.reason))


def yahoo_symbol(sym):
    sym = sym.upper().strip()
    return sym if sym.endswith(".NS") else sym + ".NS"


def ts_url(sym, now=None):
    """Three years of quarterly series for one NSE symbol."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    end = int(now.timestamp()) + 86400
    start = end - 3 * 366 * 86400
    ys = urllib.parse.quote(yahoo_symbol(sym), safe="-.")
    return TS_URL % (ys, ys, ",".join(SERIES.values()), start, end)


# ---------------------------------------------------------------- parsing

def parse_timeseries(data):
    """Yahoo's timeseries JSON -> {our key: {quarter end 'YYYY-MM-DD': value}}."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        result = doc["timeseries"]["result"]
    except (ValueError, KeyError, TypeError):
        raise FactsError("Yahoo's fundamentals answer is not the shape this tool knows")
    if not result:
        err = (doc.get("timeseries") or {}).get("error") or {}
        raise FactsError("Yahoo has no fundamentals: %s" % (err.get("description") or "empty answer"))
    back = {v: k for k, v in SERIES.items()}
    out = {k: {} for k in SERIES}
    for block in result:
        for name, points in block.items():
            if name not in back or not isinstance(points, list):
                continue
            for p in points:
                if not p or p.get("periodType", "3M") != "3M":
                    continue
                raw = (p.get("reportedValue") or {}).get("raw")
                if raw is not None and p.get("asOfDate"):
                    out[back[name]][p["asOfDate"]] = float(raw)
    if not out["revenue"] and not out["net_income"]:
        raise FactsError("no quarterly revenue or net profit in Yahoo's answer")
    return out


def fiscal_label(end):
    """'2026-09-30' -> 'Q2 FY27': India's financial year runs April to March."""
    d = _dt.date.fromisoformat(end)
    q = (d.month - 4) % 12 // 3 + 1
    fy = d.year + 1 if d.month >= 4 else d.year
    return "Q%d FY%02d" % (q, fy % 100)


def _yoy(series_, end, slack=10):
    """The value of the same quarter a year earlier (within a few days), or None."""
    target = _dt.date.fromisoformat(end) - _dt.timedelta(days=365)
    best = None
    for e, v in series_.items():
        gap = abs((_dt.date.fromisoformat(e) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, v)
    return best[1] if best else None


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def analyse(ts, quarters=6, name=None):
    """Every number in the report, as a dict."""
    rev, ni = ts["revenue"], ts["net_income"]
    ends = sorted(set(rev) | set(ni), reverse=True)[:quarters]
    rows = []
    for end in ends:
        r = rev.get(end)
        gp = ts["gross_profit"].get(end)
        if gp is None and r is not None and end in ts["cost_of_revenue"]:
            gp = r - ts["cost_of_revenue"][end]
        oi = ts["operating_income"].get(end)
        n = ni.get(end)
        eps = ts["eps_diluted"].get(end)
        rows.append({
            "end": end, "fiscal": fiscal_label(end),
            "revenue": r, "gross_profit": gp, "operating_income": oi, "net_income": n,
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gp, r), "operating_margin": margin(oi, r), "net_margin": margin(n, r),
            "revenue_yoy": pct(r, _yoy(rev, end)), "net_income_yoy": pct(n, _yoy(ni, end)),
            "eps_yoy": pct(eps, _yoy(ts["eps_diluted"], end)),
        })
    ttm = {}
    for key in ("revenue", "net_income", "eps_diluted"):
        last4 = sorted(ts[key])[-4:]
        if len(last4) == 4 and (_dt.date.fromisoformat(last4[-1]) - _dt.date.fromisoformat(last4[0])).days < 300:
            ttm[key] = sum(ts[key][e] for e in last4)
    if "eps_diluted" in ttm:
        ttm["eps_diluted"] = round(ttm["eps_diluted"], 2)
    return {"entity": name, "rows": rows, "ttm": ttm}


def cr(v):
    """Rupees -> '₹1,234.5 cr'."""
    return "n/a" if v is None else "₹{:,.1f} cr".format(v / CRORE)


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    out = []
    line = "%s (quarter ending %s): revenue %s" % (cur["fiscal"], cur["end"], cr(cur["revenue"]))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    line += ", net profit %s" % cr(cur["net_income"])
    if cur["net_income_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_income_yoy"]
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS ₹%.2f" % cur["eps_diluted"]
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur["net_income"] is not None and cur["operating_income"] is not None \
            and cur["net_income"] > cur["operating_income"]:
        out.append("Net profit is above operating profit: it includes income outside the business "
                   "(other income, investment gains, tax items). Check the filing before reading it as growth.")
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append("Revenue %+.1f%% vs the quarter before (many businesses are seasonal: "
                   "compare with a year earlier first)." % pct(cur["revenue"], prev["revenue"]))
    return out


# ---------------------------------------------------------------- the NSE results feed

def norm_name(name):
    """'Mahindra & Mahindra Ltd.' and 'MAHINDRA AND MAHINDRA LIMITED' agree."""
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return " ".join(w for w in n.split() if w not in ("limited", "ltd", "the"))


def names_from_universe(path="trader/universe.txt"):
    """{SYMBOL: company name} from trader-scanner's universe file (SYMBOL|Name a line)."""
    out = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                if "|" in ln and not ln.startswith("#"):
                    sym, nm = ln.strip().split("|", 1)
                    out[sym.strip().upper()] = nm.strip()
    return out


def latest_result(feed_xml, company):
    """The newest item for this company in NSE's results feed: {filed, text, url} or None."""
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError as exc:
        raise FactsError("NSE's results feed is not valid XML: %s" % exc)
    want = norm_name(company)
    best, best_t = None, None
    for it in root.iter("item"):
        if norm_name(" ".join((it.findtext("title") or "").split())) != want:
            continue
        stamp = (it.findtext("pubDate") or "").strip()
        try:
            when = _dt.datetime.strptime(stamp, "%d-%b-%Y %H:%M:%S")
        except ValueError:
            when = _dt.datetime.min
        if best is None or when > best_t:
            best_t = when
            best = {"filed": when.strftime("%Y-%m-%d %H:%M IST") if when != _dt.datetime.min else stamp,
                    "text": " ".join((it.findtext("description") or "").split()),
                    "url": (it.findtext("link") or "").strip()}
    return best


# ---------------------------------------------------------------- output

def _p(v):
    return "-" if v is None else ("%+.1f%%" % v if v else "0.0%")


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def _c(v):
    return "-" if v is None else "{:,.0f}".format(v / CRORE)


def render(sym, result, filing=None):
    who = "%s (%s)" % (result["entity"], sym) if result["entity"] else sym
    out = ["%s. ₹ crore except EPS. Source: %s." % (who, SOURCE), ""]
    out.append("%-11s %-8s %10s %8s %7s %7s %7s %10s %8s %7s" % (
        "quarter end", "fiscal", "revenue", "rev yoy", "gross", "oper", "net",
        "net prof", "EPS dil", "EPS yoy"))
    for r in result["rows"]:
        out.append("%-11s %-8s %10s %8s %7s %7s %7s %10s %8s %7s" % (
            r["end"], r["fiscal"], _c(r["revenue"]), _p(r["revenue_yoy"]),
            _pm(r["gross_margin"]), _pm(r["operating_margin"]), _pm(r["net_margin"]),
            _c(r["net_income"]), "-" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"],
            _p(r["eps_yoy"])))
    t = result["ttm"]
    if t:
        out.append("")
        out.append("Last 4 quarters: revenue %s, net profit %s, diluted EPS %s." % (
            cr(t.get("revenue")) if t.get("revenue") is not None else "-",
            cr(t.get("net_income")) if t.get("net_income") is not None else "-",
            "₹%.2f" % t["eps_diluted"] if t.get("eps_diluted") is not None else "-"))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    if filing:
        out.append("")
        out.append("Latest results filing on NSE: %s, %s  %s" % (filing["filed"], filing["text"], filing["url"]))
    out.append("")
    out.append("Yahoo's numbers are its reading of the filings and can lag: check the filing for anything "
               "that matters.")
    return "\n".join(out) + "\n"


def metrics_row(sym, result):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"ticker": sym, "name": result["entity"], "period_end": cur.get("end"),
            "fiscal": cur.get("fiscal"), "revenue_ttm": t.get("revenue"),
            "net_income_ttm": t.get("net_income"), "eps_ttm": t.get("eps_diluted"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "net_income_growth_yoy": cur.get("net_income_yoy"),
            "gross_margin": cur.get("gross_margin"), "operating_margin": cur.get("operating_margin"),
            "net_margin": cur.get("net_margin"), "currency": "INR", "source": SOURCE}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tickers", nargs="*", help="NSE symbols, e.g. RELIANCE INFY")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--no-filings", action="store_true", help="skip NSE's results feed")
    ap.add_argument("--universe", default="trader/universe.txt", help="SYMBOL|Name list for company names")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per symbol into this JSON")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.tickers:
        ap.print_help()
        return 2
    fetcher = Fetcher(offline=args.offline)
    names = names_from_universe(args.universe)
    feed = None
    results, rows, failed = [], [], 0
    for t in args.tickers:
        sym = t.upper()
        sym = sym[:-3] if sym.endswith(".NS") else sym
        try:
            ts = parse_timeseries(fetcher.get(ts_url(sym), "timeseries-%s.json" % sym))
            result = analyse(ts, quarters=args.quarters, name=names.get(sym))
        except FactsError as exc:
            print("facts: %s: %s" % (sym, exc), file=sys.stderr)
            failed += 1
            continue
        filing, note = None, None
        if not args.no_filings:
            if not names.get(sym):
                note = ("no company name for %s to find its NSE filing: run trader-scanner's "
                        "`scan.py universe`" % sym)
            else:
                try:
                    if feed is None:
                        feed = fetcher.get(RESULTS_URL, "nse-results.xml", nse=True)
                    filing = latest_result(feed, names[sym])
                    if filing is None:
                        note = "no %s filing in NSE's results feed right now (it holds only the latest)" % sym
                except FactsError as exc:
                    note = "NSE results feed: %s" % exc
        results.append({"ticker": sym, "result": result, "filing": filing, "changes": changes(result),
                        "note": note})
        rows.append(metrics_row(sym, result))
        if not args.json:
            sys.stdout.write(render(sym, result, filing))
            if note:
                sys.stdout.write("note: %s\n" % note)
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps(results, indent=2))
    if args.metrics and rows:
        existing = {}
        if os.path.isfile(args.metrics):
            with open(args.metrics, encoding="utf-8") as fh:
                existing = {r["ticker"]: r for r in json.load(fh).get("companies", [])}
        for r in rows:
            existing[r["ticker"]] = r
        folder = os.path.dirname(args.metrics)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump({"as_of": _dt.date.today().isoformat(),
                       "companies": sorted(existing.values(), key=lambda r: r["ticker"])}, fh, indent=2)
            fh.write("\n")
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
