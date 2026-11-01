---
name: trader-risk
description: >-
  Risk Manager. Checks every trade against your limits and stops anything
  over them, before it can reach the broker. For a day trade: a stop is
  required, the loss at the stop must fit your max loss per trade (measured
  from the higher of the plan's entry and a fresh price), reward:risk must
  meet your minimum, the day's total risk must fit your daily loss limit, no
  more than your trades per day, and no new entries outside your entry
  window or when the market is closed. It also keeps the basics: blocked
  symbols, order size, position cap, cash buffer, margin, short sales, F&O,
  orders Zerodha Kite cannot take (a rupee amount instead of shares,
  fractions, trailing stops, gtc), prices off NSE's tick, duplicates and a
  daily order limit. Getting out is never blocked by a day limit. Prints PASS or
  BLOCK with the reason and records the verdicts so trader-broker only shows
  an order that passed. Use when the user says "check my order", "is this
  safe", "risk check", "does this break my rules", "can I take this trade",
  "how much have I risked today", or before /trader-broker, always.
argument-hint: "[path to orders.json]"
---

# trader-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by trader-budget or trader-broker's flatten passes through it. A
BLOCK is final for that order: it does not go to the user for a yes, and it
does not go to the broker. Most people who day trade lose money. These limits
keep each loss and each day's losses small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-19 10:15"  # pin the clock (India time, IST)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget, or trader-broker's flatten) |
| `trader/config.json` | the limits (trader-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry it measures risk from; also values holdings |
| `trader/state/broker.json` | what was placed today: duplicates, trades per day, daily loss, day trades |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset and short sales
only. Every other rule (day limits, entry window, loss, reward:risk, blocked
list, size, duplicates) turns into a warning for an exit, never a BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol, side not buy or sell, no qty or a qty that is not whole shares (NSE has no fractions), a rupee amount (`notional`: Kite takes a quantity), a limit order with no limit price, a trailing stop (Kite has none), a validity other than `day` or `ioc`, extended hours, a product other than `MIS` (intraday) or `CNC` (delivery), an exchange other than NSE; a bracket that is not `day` or misses its stop-loss or take-profit |
| asset | a futures or options contract (`NIFTY26NOV24500CE`, `RELIANCE26NOVFUT`) while `allow_options` is false; crypto or anything that does not look like an NSE share symbol |
| short | a sell of more than you hold (`allow_short` false) |
| blocked | (entries) the symbol is on the `blocked` list |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer, or need margin |
| stop | (entries) no stop while `require_stop` is true (a bracket `stop_loss_stop_price`, or the order's plan stop); a stop on the wrong side of the entry |
| loss | (entries) qty x (entry minus stop) over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` or after `no_entries_after`, or the market is closed |
| pdt | (entries, only with `pdt_guard` true) 3 day trades already in the last 5 trading days |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a limit order. For a
  market order, the HIGHER of the plan's entry and a fresh price from
  `trader/prices.json` (under 15 minutes old). A plan written at 10:00 cannot
  understate the risk at 10:40 after the price ran up. An older price is not
  used, and it says so.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as trader-broker logged it. Three full-size trades use up a ₹1,500 day.
- **The window** is India time (IST, UTC+5:30 all year), worked out in the
  tool, never from the computer's clock zone. Weekends and NSE holidays are
  closed; the session is 9:15 AM to 3:30 PM. Set both `no_entries_*` to null
  to switch the window off.
- **NSE's tick.** A limit, trigger, stop-loss or target price that is not on
  the stock's tick (₹0.05 for a share between ₹250 and ₹1,000, for example)
  is a warning: Kite may reject it. NSE assigns each stock its band from a
  reference price, so `"tick_size"` in the config overrides the guess.
- **pdt_guard** (off by default) counts a day trade as a symbol bought and
  sold on the same day in the log, plus any entry placed today. India has no
  pattern-day-trader rule (it is a US one); keep it on as a self-limit of 3
  day trades in 5 trading days if you want one.

Warnings do not block, but say them to the user: a LIVE account, a price off
NSE's tick, a stop that lives only in the plan and not at the broker, a price
too old to use.

## Steps

1. If the price may have moved since the plan, refresh it first
   (trader-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop, reward:risk, and how much of today's limits it uses. For a
   BLOCK, give the reason and the fix (fewer shares, a closer stop from
   trader-exit, wait for the window, or skip the trade). Never change a
   limit yourself.
4. If the order changes in any way, run the check again. trader-broker matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: the worked day in the tests

The test bracket from trader-budget at 10:00 AM on Monday 2026-10-19 (TSLA's
real prices on the NSE clock), on the tests' small paper account (₹5,000
budget, ₹50 max loss, ₹150 a day):

```
PASS  #1 buy TSLA 8 shares, entry
          entry 364.18 (the plan's entry), stop 358.32, target 370.04: risk 5.86 a share
          max loss ₹46.88 (limit ₹50), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, ₹0 of ₹150 daily loss

1 passed, 0 blocked at 2026-10-19 10:00 IST. Cash after the passed orders: ₹2,086.56 (paper account).
```

One more share and it is blocked:

```
BLOCK #1 buy TSLA 9 shares, entry
          entry 364.18 (the plan's entry), stop 358.32, target 370.04: risk 5.86 a share
          max loss ₹52.74 (limit ₹50), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, ₹0 of ₹150 daily loss
        - loss: 9 shares x 5.86 risk a share = ₹52.74 at the stop, over your ₹50 max loss per trade
```

The same 8 shares checked at 10:40 with the 10:37 quote (366.48) in
`trader/prices.json` are blocked twice: 8 x 8.16 = ₹65.28 at the stop, and
reward:risk 0.44. At 9:25 they are blocked by the window (no new entries
before 9:30). The 3:15 PM sell that closes the position passes, even at the
entry cutoff: it is an exit, and so is the SL-M stop-loss ticket from
trader-broker `protect`.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Free Yahoo prices are unofficial and can lag; the broker's price is the one that fills, and a
  stop can fill worse than its price in a fast market.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Zerodha has no paper account, so paper means Claude records
  a pretend fill and never calls Kite. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No F&O, margin (MTF), short selling or leverage unless the config allows it and the user confirms.
- The Zerodha login happens on Zerodha's own page, never in the chat. API keys, if any, live in the
  Claude Desktop config or environment variables, never in the chat.
