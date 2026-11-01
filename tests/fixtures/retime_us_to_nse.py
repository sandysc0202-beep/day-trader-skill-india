#!/usr/bin/env python3
"""How the chart fixtures in this folder were made.

The free endpoints this pack reads could not be reached from the machine that
built the India version, so the chart fixtures are the upstream pack's real
Yahoo Finance responses (US stocks, recorded in October 2026) moved onto the
NSE clock. They are synthetic test data: the prices and volumes are real US
prints, the times are not real NSE times. The tests check the logic (bars,
ranges, triggers, replays), not market facts.

The rule, per bar:
  1. Read its New York wall-clock time (EDT/EST).
  2. Keep the date; move the time 15 minutes earlier, so 9:30 ET -> 9:15 IST
     and the session lines up bar for bar.
  3. Drop it if the new time is before 9:15 or at/after 15:30 IST (that drops
     US pre-market, after hours and the last three 5-minute bars).
  4. Move a US date that is an NSE holiday to DATE_MAP when the fixture asks
     (a one-day "live" file), otherwise drop that day.
meta gets India's exchange fields (gmtoffset 19800, Asia/Kolkata, INR) and every\nsymbol gets .NS (BRK-B -> BRK.B.NS), the way Yahoo names NSE shares.

    python3 tests/fixtures/retime_us_to_nse.py IN.json OUT.json [--map 2026-10-20=2026-10-21]
"""
import argparse
import datetime
import gzip
import json

NSE_HOLIDAYS = {"2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
                "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
                "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25"}
IST = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def ny_offset(utc):
    start = datetime.datetime.combine(_nth_sunday(utc.year, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc < end else -5


def move(ts, date_map, keep_outside=False):
    """US unix seconds -> NSE unix seconds, or None to drop."""
    utc = EPOCH + datetime.timedelta(seconds=int(ts))
    ny = utc + datetime.timedelta(hours=ny_offset(utc))
    d = ny.date().isoformat()
    if d in date_map:
        ny = datetime.datetime.combine(datetime.date.fromisoformat(date_map[d]), ny.time())
    elif d in NSE_HOLIDAYS:
        return None
    ist = ny - datetime.timedelta(minutes=15)
    t = ist.time()
    if not keep_outside and (t < datetime.time(9, 15) or t >= datetime.time(15, 30)):
        return None
    return int((ist - IST - EPOCH).total_seconds())


def fix_result(res, date_map):
    ts = res.get("timestamp") or []
    daily = len(ts) > 1 and (ts[1] - ts[0]) >= 86400 - 3600
    keep = []
    for i, t in enumerate(ts):
        n = move(t, date_map, keep_outside=daily)
        if n is not None:
            keep.append((i, n))
    res["timestamp"] = [n for _, n in keep]
    ind = res.get("indicators") or {}
    for group in ind.values():
        for series in group:
            for k, v in list(series.items()):
                if isinstance(v, list) and len(v) == len(ts):
                    series[k] = [v[i] for i, _ in keep]
    for k, v in list(res.items()):           # spark's flat form
        if k not in ("timestamp", "meta", "indicators") and isinstance(v, list) and len(v) == len(ts):
            res[k] = [v[i] for i, _ in keep]
    meta = res.get("meta")
    if isinstance(meta, dict):
        fix_meta(meta, date_map)


def fix_meta(meta, date_map):
    for k in ("regularMarketTime", "firstTradeDate"):
        if isinstance(meta.get(k), int):
            n = move(meta[k], date_map, keep_outside=True)
            if n is not None:
                meta[k] = n
    ctp = meta.get("currentTradingPeriod")
    if isinstance(ctp, dict):
        for part in ctp.values():
            if isinstance(part, dict):
                for k in ("start", "end"):
                    if isinstance(part.get(k), int):
                        n = move(part[k], date_map, keep_outside=True)
                        if n is not None:
                            part[k] = n
                part["timezone"], part["gmtoffset"] = "IST", 19800
    meta.update(gmtoffset=19800, timezone="IST", exchangeTimezoneName="Asia/Kolkata", currency="INR",
                exchangeName="NSI", fullExchangeName="NSE")
    if meta.get("symbol"):
        meta["symbol"] = nse_symbol(meta["symbol"])
    meta.pop("tradingPeriods", None)


def nse_symbol(sym):
    """US ticker -> the NSE-style Yahoo symbol the fixture now stands for: BRK-B -> BRK.B.NS."""
    sym = str(sym).upper().replace("-", ".")
    return sym if sym.endswith(".NS") else sym + ".NS"


def walk(doc, date_map):
    if isinstance(doc, dict):
        if "symbol" in doc and "response" in doc:   # a spark item
            doc["symbol"] = nse_symbol(doc["symbol"])
        if "timestamp" in doc and ("indicators" in doc or "close" in doc or "meta" in doc):
            fix_result(doc, date_map)
            return
        for v in doc.values():
            walk(v, date_map)
    elif isinstance(doc, list):
        for v in doc:
            walk(v, date_map)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--map", action="append", default=[], help="US date=NSE date for a holiday")
    a = ap.parse_args()
    date_map = dict(m.split("=") for m in a.map)
    raw = open(a.src, "rb").read()
    gz = a.src.endswith(".gz")
    doc = json.loads(gzip.decompress(raw) if gz else raw)
    walk(doc, date_map)
    out = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    open(a.dst, "wb").write(gzip.compress(out, mtime=0) if a.dst.endswith(".gz") else out)


if __name__ == "__main__":
    main()
