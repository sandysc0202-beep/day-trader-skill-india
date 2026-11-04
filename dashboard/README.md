# The dashboard

A local trading screen over the pack's own tools. Standard-library Python and
one HTML page: nothing to install, no outside scripts, and it listens on
`127.0.0.1` only.

```bash
python3 dashboard/server.py --demo                     # replay a real opening hour, no account needed
python3 dashboard/server.py                            # live: trader/ in the folder you run it from
python3 dashboard/server.py --workdir ~/trading --port 8765 --no-browser
```

Then open http://127.0.0.1:8765 (it opens by itself unless `--no-browser`).

## What is on the screen

| panel | what | from |
| --- | --- | --- |
| Session bar | IST clock, the NSE session from 9:15 to 3:30 with the opening range, 9:30, last entry and flat-by marked | trader-alerts' NSE calendar, your config |
| 01 Scanner | opening-range breakouts, best score first: each with its score out of 100 (volume vs usual, how far past the range, the range's width, the Nifty, room under yesterday's high) and why | `scan.py orb` |
| 02 Favourites | your symbols with live prices and sparklines (`trader/favorites.txt`) | FYERS quotes, or Yahoo |
| Chart | 5-minute candles, the opening range, entry, stop and target, and the Time Machine's cone (5 to 95 % and 25 to 75 % of 10,000 replays, and the median) | the chart data, `trader/plan.json`, `trader/timemachine.json` |
| Pipeline | scan, entry, plan, replay, size, risk, yes, fill, exit: what is done for the symbol on screen | the files in `trader/` |
| 03 Entry & Plan | waiting / triggered and why; entry, stop, target, R, flat-by; re-plan with another stop or target | `entry.py check`, `exit.py plan` |
| 04 Time Machine | target first, stop first or neither by flat-by, the past triggers, the verdict in plain words | `timemachine.py run` |
| 05 Size, Risk, Order | shares and what bound them, PASS or BLOCK with every reason, the ticket, your yes, the fill | `size.py`, `check.py`, `ticket.py` |
| 06 Positions & Limits | open positions with live P&L and where the price sits between stop and target, trades used today, open risk plus today's losses against your daily limit, today's log | `trader/portfolio.json`, `trader/state/broker.json`; FYERS positions when live |
| 07 News & NSE filings | headlines and filings for your favourites, today's movers | `news.py` |
| 08 Journal | every closed trade with P&L and R, win rate, net, the equity curve | the fill log |
| RESULTS | the quarterly numbers for the symbol on screen | `facts.py` |

**BACKTEST** replays the strategy over past sessions for your favourites (or
the symbols you type), the last year by default, with your rules and FYERS'
charges: trades, win rate, average R after charges, net P&L, the worst losing
run, max drawdown, profit factor, an equity curve, every month, and the
SCORE CHECK: whether higher-scored trades had a better average R. It runs
`trader-backtest/backtest.py`; a year needs today's FYERS login (Yahoo keeps
60 days), and the bars are cached so the next run is quick.

**ANALYZE ▸** runs the entry check, and if it triggered, the plan, the Time
Machine, the size and the risk check for the symbol on screen. It places
nothing.

Alerts show on screen: the stop, the target, ten minutes before flat-by and
flat-by itself. An alert is information, never an order.

## Orders

The same gates as the skills, every time:

1. trader-risk passed this exact order today.
2. You type **yes** for this one order. It is recorded word for word, and
   "sure", "ok" or "yes to all" are refused.
3. A second click. In paper mode (`"paper": true`, the default) that records
   a pretend fill at the latest price and nothing goes to FYERS. Live, you
   also type the symbol, and then `ticket.py place` sends exactly the body
   shown to FYERS.

The **Trailing stop** tickbox on the Positions panel (off by default) moves
each open position's stop up: to breakeven when the price reaches 1R, then to
each completed 5-minute candle's low. Each position shows its stop now
(`STOP 362.10 ↑ from 358.30 · breakeven ✓`) and the chart draws it as an
amber TRAIL line. Paper: the exit is recorded when the price reaches the
stop. Live: your STOP-LOSS order at FYERS is moved up and FYERS sells. It
runs while the dashboard runs. The BACKTEST window has a "with trailing stop"
box to compare.

Positions get **FLATTEN** (a market sell ticket, through the risk check and
your yes again) and, live, **STOP-LOSS** (the SL-M sell from `ticket.py
protect`). Before a live flatten, cancel the open stop-loss (`ticket.py
cancel --broker-id ID`, shown in the flatten message) so both sells cannot
fill.

## FYERS

**CONNECT FYERS** in the top bar:

1. Create an individual app at myapi.fyers.in with the redirect URL the
   dialog shows (`http://127.0.0.1:8765/fyers/callback` by default).
2. Paste the App ID and the secret into the dialog. They are saved on this
   computer only (`trader/fyers.json`, `trader/fyers.secret`). Never paste
   the secret into a chat.
3. **LOG IN WITH FYERS** each trading morning: FYERS' own page, your password
   and TOTP there. FYERS expires tokens at 6:30 AM IST.

With today's token every price on the screen comes from FYERS. Pick your
plan in the dialog (it writes `"fyers_plan"` to `trader/config.json`):

| | Standard (free) | Prime |
| --- | --- | --- |
| quotes refresh | every 3 s, 50 a request | every second, 1,000 a request |
| chart refresh | 20 s | 5 s |
| sparklines | every 5 min | every minute |
| Market scan | Yahoo (FYERS scans up to 40) | FYERS, the whole market |

Tick **FYERS only** in the same dialog to turn the Yahoo backup off: a panel
then shows a red **NO DATA** badge and the FYERS error instead of Yahoo's
delayed prices (log in, or wait for FYERS). News headlines and results stay
on Yahoo.

Every panel shows a badge, **FYERS**, **YAHOO** or **REPLAY**, for where its
data came from last. Hover it for the reason when FYERS failed and Yahoo
stepped in (FYERS down, logged out, or the day's calls used up). Company
results are always Yahoo's: FYERS has no fundamentals. Live API orders also need a static IP registered with FYERS
(SEBI's rule from 1 April 2026).

## The demo

`--demo` replays Monday 2026-10-19 from `tests/fixtures`: real 5-minute
charts of 234 US stocks moved onto the NSE clock (real prices, synthetic
times), the TSLA trade the tests use, sample news and an invented company's
results. The saved charts cover 9:15 to 10:45, so the replay clock runs
there: play, pause, change the speed or drag it. Its files live in
`./trader-demo` (RESET clears them). The Time Machine has 60 days of bars for
TSLA only in the demo.

Education and tooling, not financial advice.
