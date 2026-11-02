---
name: trader-backtest
description: >-
  Backtester. Tests the pack's opening-range breakout on past NSE sessions:
  the last year of 5-minute bars from FYERS (Prime keeps five years and
  more) for your symbols, with the same rules the live pack trades by (the
  15-minute range, entries 9:30 to 2:45, the stop at the range midpoint, a
  2R target, flat by 3:00 PM, whole shares sized to your max loss, three
  trades and ₹1,500 of risk a day), FYERS' intraday charges and slippage.
  Reports the win rate, average R before and after charges, net P&L, the
  worst losing streak, max drawdown, profit factor and a month-by-month
  table, and saves every trade to a CSV. Use when the user says "backtest
  it", "does this strategy work", "what is the win rate", "how often does
  the breakout work", "test it on last year", "worst losing streak", "try
  1.5R instead", or before trading real money.
argument-hint: "[symbols]"
---

# trader-backtest

The Time Machine asks "what are the odds for this one trade today?" The
backtester asks the bigger question first: **over the last year, did this
strategy make money after charges, and how bad did the bad stretches get?**

```bash
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --symbols RELIANCE,HDFCBANK,ICICIBANK,INFY,TCS
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --watchlist trader/favorites.txt --days 180
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --universe trader/universe.txt --limit 200    # FYERS Prime
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --symbols SBIN --target 1.5R --stop or-low
python3 "${CLAUDE_SKILL_DIR}/backtest.py" show
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | budget, max loss a trade, the day limits, entry window, flat-by, `fyers_plan` (Prime brokerage is ₹15 an order) |
| FYERS 5-minute history | through `trader-broker/fyers_api.py`, after the morning FYERS login; 100 days a request |
| `trader/backtest-cache/` | the bars already fetched, so the next run asks FYERS only for new days |
| `trader/backtest.json` | the full result: parameters, summary, every trade |
| `trader/backtest-trades.csv` | one row a trade, for a spreadsheet |

Without today's FYERS login it falls back to Yahoo, which keeps only about
60 days of 5-minute bars, and it says so. A year needs FYERS.

## The rules it replays

Exactly the live pack's, so the result is about the strategy you will trade:

- **Opening range:** the three 5-minute bars from 9:15 to 9:30.
- **Entry:** the first bar that closes above the range high, closing from
  9:30 to `no_entries_after` (14:45), bought at that close plus slippage.
- **Stop:** the range midpoint (`--stop or-low` for the range low), rounded
  down onto NSE's tick. **Target:** 2R (`--target 1.5R`...), rounded up.
- **Exit:** the stop, the target, or the last close before `flat_by`
  (15:00). A bar that touches both counts as the stop: 5-minute bars cannot
  say which came first, so it assumes the worse. A gap fills at the open.
- **Size:** whole shares, the smallest of max loss / risk, `max_order_inr`,
  the position cap and the cash after the buffer.
- **A day:** at most `max_trades_per_day`, earliest triggers first, one per
  symbol, while the day's max losses fit `max_daily_loss_inr`.
- **Costs:** FYERS intraday charges (brokerage, STT on the sell, NSE
  transaction charges, SEBI fee, IPFT, stamp duty on the buy, GST), and
  `--slippage-pct` (0.02 %) against you on the entry, the stop and flat-by.

## Steps

1. Make sure today's FYERS login is done (`fyers_api.py status`). On the
   free plan keep the list short (a few hundred symbols for a year is
   thousands of requests at 45 a minute); Prime takes the big list.
2. Run it on the stocks you actually trade. Five to fifty liquid names is a
   fair test; one stock is an anecdote.
3. Read the result to the user in this order: the number of trades (under
   about 100, say the sample is small), the average R after charges (the
   real edge: above 0 makes money, below 0 loses it), the win rate (an ORB
   can win under half its trades and still pay, if the wins are bigger),
   the worst losing streak and the max drawdown (can they live through
   that?), and the months (one good month carrying the year is a warning).
4. Change one thing at a time to compare (`--target 1.5R`, `--stop or-low`,
   `--min-volume-x 1.5`), and say that trying many settings and keeping the
   best one fits the past, not the future.

## What it never does

- Never calls a backtest a prediction or a promise of returns.
- Never hides the charges: both the gross and the net are shown.
- Never places, sizes or recommends a live trade.

## Safety

Education and tooling, not financial advice. A backtest replays history with
fills at bar closes: no order book, no queue at the stop, no circuits, no
news, and today's list tested on the past (survivorship). Paper trade before
real money. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
