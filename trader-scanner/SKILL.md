---
name: trader-scanner
description: >-
  Chart Scanner. Scans thousands of charts for setups: opening-range
  breakouts, flags, oversold bounces. Builds the list of NSE shares you can
  day trade (series EQ) from NSE's own equity list, or just the Nifty 500,
  runs a pre-market scan of daily closes (20-day closing-high breakout, bull
  flag, reclaimed the 20-day average, oversold, oversold bounce) and an
  intraday opening-range breakout scan on 5-minute bars, then writes
  trader/watchlist.json for trader-news and trader-entry. Free Yahoo data,
  bar by bar. Use when the user says "scan the market", "find me setups",
  "what is breaking out", "opening range breakouts", "scan for bull flags",
  "oversold stocks", "pre-market scan", "build my watchlist", "what should I
  look at today", or at the start of a trading day before /trader-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads thousands of
free charts and lists the ones that match a rule. That is all it does. A scan
is a list of charts that match a rule, not a list of stocks to buy, and a
setup on a chart is not a prediction. Most people who day trade lose money;
the scanner does not change that. It only saves you from flipping through
2,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                       # NSE's EQ list -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --list nifty500       # or only the Nifty 500
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                          # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,M\&M  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                            # after 9:35 AM IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                 # cut the clock at 10:15 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price 20` (rupees) and `--min-volume` (daily 500,000 shares, orb 0),
`--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--now "2026-10-19 10:15"` (India time, IST) to pin the clock, `--offline PATH`
to read saved Yahoo files, `--no-write`, `--show N`, `--json`. Exit code 0
when the scan found something, 1 when it found nothing or could not run (the
reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb`, and by trader-news and trader-analyst to match NSE filings to a symbol by company name |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 14:45) |

## The universe

`universe` downloads NSE's list of listed equities,
`nsearchives.nseindia.com/content/equities/EQUITY_L.csv`, and keeps a row when:

1. The symbol is a plain NSE symbol: letters, digits, `&` and `-`, up to 20
   characters (`RELIANCE`, `M&M`, `BAJAJ-AUTO`, `3MINDIA`).
2. The series is `EQ`, the normal rolling-settlement series. Everything else
   is out and counted by name: `BE` and `BZ` are trade-for-trade (every trade
   settles by delivery, so a share bought today cannot be sold today), `SM`
   and `ST` are the SME platform, `IV` and `RR` are InvITs and REITs.

`--list nifty500` also downloads NSE's Nifty 500 list
(`nsearchives.nseindia.com/content/indices/ind_nifty500list.csv`) and keeps
only the EQ shares in it: the liquid end of the market, and a scan that takes
a few minutes instead of a quarter of an hour. NSE's archive turns away
requests that do not look like a browser, so the tool sends a browser-like
User-Agent for these two files.

Symbols are NSE's own. Yahoo gets them with `.NS` added (`RELIANCE.NS`), and
`M&M` goes into a URL as `M%26M.NS`.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so a full
EQ list of about 2,000 symbols takes about 100 requests and the Nifty 500
takes 25. Liquidity comes from the spark meta: last price at least
`--min-price` (₹20) and last session's volume at least `--min-volume`
(500,000 shares). Run it before the open or after the close: during the
session the volume is the day's so far, and today's daily bar is left out
until 3:30 PM.

The setups are computed from daily closes only (spark has no highs or lows),
so they are close cousins of the upstream pack's `refs/scan.py`, which used
highs and lows:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one. Hits are
listed most traded first (price times volume).

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 9:15,
9:20 and 9:25 five-minute bars. A hit is the first completed 5-minute bar
to CLOSE above the range high, at that bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. On a strong morning most charts are candidates, so stage 2
   checks at most `--max-confirm` (200) of them, the most traded first (price
   times volume so far), and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all.

Timing: about a third of a second per request with the polite pause, so 100
names take well under a minute and the Nifty 500 a couple of minutes, plus
up to 200 v8 charts in stage 2. For a fast answer, scan the daily watchlist
or a list of names you trade (`--watchlist`, `--symbols`).

A chart is usable only when every 5-minute bar from 9:15 to the cutoff is
there; a missing bar means a halt or a thin stock. Null closes are dropped,
a bar off the 5-minute grid (the live tick, like 10:43:07) is dropped, and a
bar counts only once it is complete (start + 5 minutes). Before 9:35 AM, on a
weekend or NSE holiday, after the close, or after `no_entries_after` (an
expired setup) it prints one plain line and writes nothing. A stock that hits
its circuit (price band) limit stops trading, so its chart has gaps and is
left out as unusable.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, bar by bar, never tick by tick. query2 first, query1 on a 429
  or 5xx, one retry after a 5-second wait on a 429.
- **FYERS instead of Yahoo.** After the morning FYERS login (trader-broker
  `fyers_api.py login`) the bars come from FYERS: official and free, but 50
  data calls a minute, so a FYERS scan takes a watchlist of up to 40 symbols
  (`--symbols` or `--watchlist`). For the whole market, run the scan with
  `TRADER_DATA_SOURCE=yahoo`.
- Partial results are said as partial: "Fetched 1,980 of 2,050 charts (70
  failed or had no data)". Never round a partial scan up to "the whole market".
- NSE's 2026 trading holidays are in the calendar (NSE has no half days). Add
  2027's when NSE publishes them in December.

## Steps

1. Once a week or so: `scan.py universe` (or `scan.py universe --list nifty500`).
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 9:35 AM IST (10:00 is a common time): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and that a later hit is
   closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news TICKER` (what is moving
   it), then `/trader-entry TICKER` (the entry bar by bar). Never pick for them.

## Example: the worked day in the tests

The tests run on real Yahoo charts of US stocks from Monday Oct 19 2026,
moved onto the NSE clock (real prices and volumes, synthetic times: see
`tests/fixtures/retime_us_to_nse.py`). A scan at 10:00 AM over that 234-chart
wall:

```
python3 scan.py orb --offline wall5m-2026-10-19.json.gz --now "2026-10-19 10:00" --min-price 0
Fetched 233 of 234 charts (1 failed or had no data); 231 usable; 95 candidates, 95 confirmed with real highs and lows.
42 of 231 charts closed above their opening-range high by 10:00 AM:
   9:20 AM  AMC         ₹2.97  range 2.90 to 2.96  +0.50% over
  ...
  10:00 AM  TSLA      ₹364.18  range 354.47 to 362.18  +0.55% over
```

The wall has no price floor (AMC at 2.97 is one of the 42), hence
`--min-price 0`; with the default ₹20 floor it would drop. TSLA's range is
the 9:15 to 9:25 bars, high 362.18, low 354.47; the 9:55 bar closed at 364.18,
0.55 % over the high, at 10:00 AM. The user picks one chart and Claude hands
it to `/trader-news SYMBOL`, then `/trader-entry SYMBOL`. On a live day the
symbols are NSE's (`RELIANCE`, `TATAMOTORS`, ...).

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). FYERS has no paper account, so paper means Claude records
  a pretend fill and nothing goes to FYERS. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker's `ticket.py place`, and only when all three
  hold: trader-risk passed that exact order today, the user typed an explicit yes for that one order in
  the chat, and the user clicked Allow once on Claude's own card for that command. Never add
  `ticket.py place` (or a FYERS MCP order tool) to an "Always allow" list.
- No F&O, margin (MTF), short selling or leverage unless the config allows it and the user confirms.
- The FYERS login happens on FYERS' own page, never in the chat. The app secret lives in an environment
  variable or `trader/fyers.secret`, never in the chat.
