# The Day Trader skill, India edition

10 Claude skills for day trading on NSE: scan the market's charts, track breaking news and NSE filings, time every entry and exit, and replay every outcome before you risk a rupee.

Free, MIT, no signup. The tools are plain Python that read free data: FYERS'
official NSE bars and quotes once you log in to FYERS each morning (free API),
or Yahoo Finance charts for NSE shares (`.NS`) without it, plus NSE's own
equity list and filing feeds. Orders go to FYERS, only after your yes. And a
**live dashboard** puts the whole day on one screen.

This is a fork of [Jake Schincariol's Day Trader skill](https://github.com/Jakeschincariol/day-trader-skill)
(the Trader skill v2, built for the US market), moved to India: the NSE clock
and holidays, NSE symbols, rupees, NSE's price ticks, NSE filings instead of
the SEC, and FYERS instead of Alpaca. The US-only Pelosi tracker is gone.

One scans the market for setups. Another tracks breaking news and NSE filings
on every stock you watch. One watches a setup bar by bar and tells you the
moment to get in, and another sets your stop and your target before you do.
And the Time Machine: before you risk a rupee, it replays the rest of the
session 10,000 times from the stock's own recent bars, plus every past day
the same setup fired, and tells you the honest odds.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

## What changed from the US version

| | US original | this India edition |
| --- | --- | --- |
| Session | 9:30 to 4:00 New York time, daylight saving | 9:15 to 3:30 IST (UTC+5:30 all year) |
| Holidays | NYSE 2026 and 2027, half days | NSE 2026 equity holidays, no half days (add 2027 when NSE publishes it in December) |
| Stock list | Nasdaq Trader symbol directories | NSE's `EQUITY_L.csv`, series EQ only (BE/BZ trade-for-trade are out), or `--list nifty500` |
| Symbols | `TSLA`, `BRK.B` | `RELIANCE`, `M&M`, `BAJAJ-AUTO` (Yahoo gets `.NS`) |
| Money | dollars | rupees (₹), `_inr` config keys, crore in the analyst |
| Prices | to the cent | stops and targets rounded onto NSE's tick (₹0.01 / 0.05 / 0.10 / 0.50 / 1 / 5 by price band) |
| Filings | SEC EDGAR 8-Ks, XBRL facts | NSE announcements and results feeds; Yahoo fundamentals for `.NS` |
| Broker | Alpaca (paper account), IBKR | FYERS API v3 (`ticket.py place` after your yes). FYERS has no paper account, so paper mode records pretend fills |
| Data | Yahoo | FYERS' official bars and quotes after the morning login (free); Yahoo without it |
| Bracket | one bracket order at the broker | entry first, then the stop-loss (SL-M) as its own ticket; the target is watched by alerts |
| Flat by | 3:55 PM | 3:00 PM, last entry 2:45 PM (FYERS squares off INTRADAY from 3:05 PM for F&O stocks, 3:20 PM for the rest) |
| Pelosi tracker | Congress's trade filings | removed (no Indian equivalent) |

## Install

Paste this into Claude:

```
https://github.com/sandysc0202-beep/day-trader-skill-india

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add sandysc0202-beep/day-trader-skill-india
/plugin install day-trader-skill-india@day-trader-skill-india
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/day-trader-skill-india:trader-scanner` and so on. Copy the folders instead if
you want plain `/trader-scanner`:

```bash
git clone https://github.com/sandysc0202-beep/day-trader-skill-india.git
cp -r day-trader-skill-india/trader-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. No Claude Code at all? Paste any single `SKILL.md` at the
top of a chat and it runs as a mode. You lose the Python tools, but the method
works.

The tools need Python 3.8 or newer. Nothing to pip install. Start with
`trader-scanner/scan.py universe`: it writes `trader/universe.txt`, NSE's own
list of symbols and company names, which the news and analyst skills use to
match NSE filings to your symbols.

## The dashboard

A futuristic, local trading screen over the same tools: the NSE session
timeline, the scanner's breakouts, favourites with live sparklines, a
candlestick chart with the opening range, entry, stop and target drawn on it
and the **Time Machine's probability cone** projected forward, the trade
pipeline (scan, entry, plan, replay, size, risk, yes, fill, exit) lighting up
step by step, positions with live P&L and your daily limits, news and NSE
filings, and a journal of every closed trade with win rate, R and an equity
curve.

```bash
python3 dashboard/server.py --demo        # try it now: replays a real opening hour, no account needed
python3 dashboard/server.py               # your own trader/ folder, FYERS or Yahoo data
```

It opens http://127.0.0.1:8765 and listens on your own computer only.
Standard-library Python, no install, no outside scripts. **ANALYZE** runs
entry, plan, time machine, size and the risk check for the symbol on screen
and places nothing. An order needs the risk PASS, your typed "yes" (recorded
word for word), then a second click; a live order also asks you to type the
symbol. **CONNECT FYERS** saves your app id and secret on this computer only
and logs you in on FYERS' own page; after that the charts and quotes are
FYERS' and a live order goes through `ticket.py place`. See
`dashboard/README.md`.

## The ten

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans NSE's charts for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Tracks breaking news and NSE filings on every stock you watch and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, on NSE's tick, so you know when to get out. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a rupee: 10,000 simulated sessions plus every past time the setup fired. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade. |
| `/trader-broker` | Order Executor | Sends approved orders to FYERS, paper trading by default, never without your yes. Logs you in to FYERS each morning. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop, the target or the opening bell hits. |
| `/trader-analyst` | Earnings Analyst | Reads quarterly results and sums up what changed. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
broker protect: the stop-loss after the entry fills                         (right after)
alerts: stop, target, flat by 3:00 PM                                         (while it is open)
news / analyst                                                                (on a schedule)
```

A day, step by step:

1. **`/trader-scanner`** after 9:35 AM IST: which NSE charts have closed above
   their 15-minute opening range (the 9:15, 9:20 and 9:25 five-minute bars).
2. **`/trader-news`**: the stock's headlines and its NSE filings, in IST. A
   results date or a board meeting inside the trade is the thing to know.
3. **`/trader-entry`** watches the chart bar by bar and says "triggered" on
   the first 5-minute close above the range.
4. **`/trader-exit`** sets the plan before the entry: the stop at the range's
   midpoint, rounded down onto NSE's tick, the target at a multiple of the
   risk, rounded up, and flat by 3:00 PM either way.
5. **`/trader-timemachine`** replays the rest of the day 10,000 times from the
   stock's own 5-minute bars of the last 60 days (seed 19, so anyone can
   repeat it), re-runs the same rule on those real sessions, and gives one
   honest verdict. Often it is "close to a coin flip": that is the point of
   looking before you risk a rupee.
6. **`/trader-budget`** with a ₹1,00,000 account and a ₹500 max loss per
   trade sizes the trade in whole shares and writes one intraday
   (INTRADAY) order.
7. **`/trader-risk`** checks it against your limits: the max loss, the daily
   loss limit, trades per day, reward to risk, the entry window, position
   size, cash, and what FYERS accepts. PASS or BLOCK, with the reason.
8. **`/trader-broker`** shows you the ticket in plain words. You type yes for
   that exact order. In paper mode Claude records a pretend fill; live,
   Claude runs `ticket.py place` and Claude's own "Allow once / Deny" card
   asks you one more time. Then `protect` puts the stop-loss in, as its own
   ticket with its own yes.
9. **`/trader-alerts`** rings on the stop, the target and at 3:00 PM to
   flatten whatever is still open.

**FYERS, once:** open a FYERS account, create an individual API app at
myapi.fyers.in (redirect URL `http://127.0.0.1:8765/fyers/callback` if you
use the dashboard), then `python3 trader-broker/fyers_api.py setup --app-id
YOUR-APP-ID --redirect <that URL>` and put the secret in `FYERS_SECRET` or
`trader/fyers.secret` yourself, never in a chat. **Each trading morning:**
`fyers_api.py login` (or the dashboard's button); FYERS expires tokens at 6:30
AM. Live API orders also need a static IP registered with FYERS (SEBI, from 1
April 2026).

**FYERS Prime or Standard.** Set `"fyers_plan"` in `trader/config.json` (or
the plan switch in the dashboard's FYERS dialog). `"standard"` (free) paces
the pack at 4 calls a second, 45 a minute and 4,800 a day, and a FYERS scan
takes up to 40 stocks (bigger scans go to Yahoo). `"prime"` paces at 9 a
second, 450 a minute and 4,90,000 a day, quotes 1,000 stocks a request, and
scans the whole market on FYERS (one request a stock: about 5 minutes for
2,000). Each limit sits just under FYERS' own, because the third minute over
blocks the API for the day. Yahoo stays for company results (FYERS has
none), and as the automatic backup whenever FYERS is down, you are logged
out, or the day's FYERS calls are used up: the tools say so and carry on.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 trader-scanner/scan.py universe                                # NSE's EQ shares -> trader/universe.txt
python3 trader-scanner/scan.py universe --list nifty500                # or only the Nifty 500
python3 trader-scanner/scan.py daily                                   # pre-market setups
python3 trader-scanner/scan.py orb --at 10:00                          # opening-range breakouts so far
python3 trader-news/news.py --tickers RELIANCE --hours 12              # headlines, NSE filings, today's move
python3 trader-entry/entry.py check RELIANCE --setup orb               # waiting / triggered / expired
python3 trader-entry/entry.py watch RELIANCE --every 30                # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-broker/ticket.py place --index 1                        # live: send the approved order to FYERS
python3 trader-broker/ticket.py protect --symbol RELIANCE              # the stop-loss ticket
python3 trader-broker/fyers_api.py login                               # each morning: today's FYERS token
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by close
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/facts.py RELIANCE                               # quarters, growth, EPS, in crore
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline. NSE and Yahoo could not be reached from the machine
that built this edition, so the chart fixtures are the upstream pack's real
Yahoo data for US stocks, moved onto the NSE clock (real prices and volumes,
synthetic times: `tests/fixtures/retime_us_to_nse.py` shows exactly how).
The NSE list and filing fixtures are hand-made in NSE's exact file shapes,
and the analyst's fixtures are an invented company. They test the logic, not
market facts. **Before trading on it, run each tool once against the live
endpoints** (`scan.py universe`, `news.py --tickers RELIANCE`,
`facts.py RELIANCE`, `fyers_api.py quote RELIANCE`) and check the output
against NSE's website and the FYERS app. The FYERS calls are written from
FYERS' API v3 documentation and tested against canned answers, not yet
against a live account: place one paper day, then one 1-share live order,
before trusting them.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money: SEBI's 2024 study found that 7 in 10
individual intraday traders in the equity cash segment lost money in FY23.
You can lose money. Decide for yourself, and talk to a SEBI-registered
adviser if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat (or "yes" in the dashboard). `ticket.py place` checks
both itself. Never add it, or a FYERS MCP order tool, to an "Always allow"
list, so Claude's own "Allow once / Deny" card is a second check. No F&O,
margin (MTF), short selling or leverage unless you turn them on.

**Paper first.** `"paper": true` is the default in `config.example.json`.
FYERS has no paper account, so in paper mode nothing goes to FYERS and fills
are pretend, at the latest price. Your FYERS login happens on FYERS' own
page; the app secret lives in an environment variable or `trader/fyers.secret`
on your computer, never in the chat.

**The entry goes alone.** The stop-loss (an SL-M sell) follows as its own
ticket once the entry fills, and the target is watched by `/trader-alerts`,
not placed. Before a flatten, `ticket.py cancel` takes the open stop out
first: two open sells for the same shares could both fill and leave you
short.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: results, a circuit, a headline. Treat its odds as a reality
check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market, across a gap or into
a circuit, a stop order can fill well past its price, or not at all. The max
loss shown is the loss at the stop price.

**The data is free and unofficial.** Yahoo Finance's endpoints are not an
official API: they can lag, change or rate-limit you. NSE's archive files and
RSS feeds can change shape or block automated requests. The scanner and the
entry timer read 1-minute or 5-minute bars, not every tick. Check anything
that matters against the FYERS app before you act on it.

**Price ticks.** NSE's tick depends on the stock's price band (from April
2025: ₹0.01 under ₹250, ₹0.05 to ₹1,000, ₹0.10 to ₹5,000, ₹0.50 to ₹10,000,
₹1 to ₹20,000, ₹5 above), set from a reference price and reviewed by NSE. If
FYERS rejects a price for its tick, set `"tick_size"` in your config.

**Charges and taxes.** Every trade pays brokerage, STT, exchange charges,
SEBI fees, GST and stamp duty; small intraday moves can be eaten by them.
Intraday equity gains are treated as speculative business income in India.
Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, fyers_api.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
dashboard/          server.py, index.html, README.md: the local trading screen
tests/              unittest suite + offline fixtures
```

MIT. Original pack by [Jake Schincariol](https://opusjake.ai); India edition adapted from it.
