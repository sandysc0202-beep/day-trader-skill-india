---
name: trader-budget
description: >-
  Budget Manager. Sizes every position to your budget and your max loss per
  trade. Reads the trade plan from trader-exit (entry, stop, target), finds
  the whole shares that fit inside four limits (max loss per trade, position
  cap, max order, cash after your buffer), says which limit decided it, and
  writes ONE intraday (INTRADAY) bracket order (entry, stop-loss, take-profit)
  to trader/orders.json for NSE, which trader-broker sends to FYERS after your
  yes. A stop too far away for your max loss is a plain refusal, not a
  smaller order. Nothing is placed. Use when the user says "how many shares",
  "size this trade", "how much should I buy", "I have ₹1 lakh", "I only want
  to risk ₹500 a trade", "set my rules", "keep 10% in cash", or after
  /trader-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# trader-budget

The Budget Manager turns a plan ("buy RELIANCE at 1,412.40, stop 1,404.10")
into an exact order ("buy 60 shares"), inside rules the user sets once. The stop
decides the size: the further away the stop, the fewer shares. It writes a
proposal only. trader-risk checks it and trader-broker asks the user yes or
no. Day trading loses most people money. Sizing to a max loss keeps each
loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                        # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write        # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1412.40 --json # a limit order at 1,412.40
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights RELIANCE=40,INFY=60 --prices trader/prices.json --config split.json   # weights mode
```

`--now "2026-10-19 10:15"` pins the clock (India time, IST). A plan for
another day is refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time (trader-exit writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (trader-alerts writes it) |
| `trader/portfolio.json` | optional: its cash caps the size (trader-broker keeps it) |
| `trader/orders.json` | the one proposed bracket order it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words:

- **budget**: how much money this is for. Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at the stop (default ₹500).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (₹50,000) and the smallest order (₹100).
- The day limits trader-risk enforces: **max_daily_loss_inr** (₹1,500), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "14:45") and **flat_by** ("15:00"), all India time (IST). FYERS squares off
  INTRADAY positions itself from 3:05 PM for stocks with F&O contracts.
- **tick_size**: leave it blank to use NSE's price bands; set it to the step FYERS shows if a
  stock's band differs.
- **blocked**: symbols they never want to trade.
- Keep **paper** true until they have watched it work: FYERS has no paper account, so paper
  means pretend fills recorded here.

The size uses cash only. Brokers give intraday leverage; this pack
never sizes on it.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only: NSE has no fractional shares. The size is the smallest
of four limits:

| limit | shares |
| --- | --- |
| max loss | `max_loss_per_trade_inr` / risk a share (entry minus stop) |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `trader/portfolio.json` |

The entry it sizes at: the limit price with `--limit`. For a market order, the
plan's entry, or the price in `trader/prices.json` if it is fresh (under 15
minutes old) and higher. A price that ran up since the plan means a bigger
loss at the same stop, so fewer shares. A lower price never shrinks the risk.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), the
plan is for another day, the ticker is blocked, or the plan is a short. Say
the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees
and the max loss at the stop. Say plainly that a stop is an order, not a
promise: in a fast market it can fill below the stop. Then hand over to
`/trader-risk`.

## Example: the worked day in the tests

The test plan (TSLA's real prices on the NSE clock, entry 364.18 at 10:00 AM,
stop 358.32 at the opening-range midpoint, target 370.04 at 1R, risk 5.86 a
share) with the example config: ₹1,00,000 budget, 10% cash buffer, ₹500 max
loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | ₹500 / 5.86 | **85 (decides it)** |
| position cap | 75% of ₹1,00,000 = ₹75,000 / 364.18 | 205 |
| max order | ₹50,000 / 364.18 | 137 |
| cash | ₹1,00,000 minus ₹10,000 = ₹90,000 / 364.18 | 247 |

85 shares = ₹30,955.30. Max loss at the stop: 85 x 5.86 = ₹498.10. It writes:

```json
{"symbol": "TSLA", "side": "buy", "qty": "85", "type": "market", "time_in_force": "day", "product": "INTRADAY",
 "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32",
 "why": "opening-range breakout on TSLA: 85 shares, bound by your ₹500 max loss per trade (85 x 5.86 = ₹498.10 at the stop)",
 "plan": {"entry": 364.18, "stop": 358.32, "target": 370.04, "risk_per_share": 5.86, "max_loss": 498.1,
          "setup": "orb", "entry_time": "10:00 AM", "flat_by": "15:15"}}
```

`why` and `plan` are notes for trader-risk and trader-broker. They are never
sent to FYERS. trader-broker sends the entry as a FYERS market order with
product INTRADAY; the stop-loss follows as its own SL-M ticket once the entry
fills, and trader-alerts watches the target. With the stop at 300.00 instead, one share would risk 64.18 and the
same rules buy 7: a stop that far is allowed, just smaller. With the tests'
small account (₹5,000 budget, ₹50 max loss) it is 8 shares and a refusal at
300.00. If the price has run to 366.48 by the time it sizes, reward:risk
falls to 0.44: refused, because the move it was waiting for has mostly
happened.

## Weights mode (kept from v1)

`--weights RELIANCE=30,HDFCBANK=25,INFY=20` or `--targets FILE`
(`targets.example.json` here, an example split, not a recommendation) splits
a budget across a list by weight, as delivery (CNC) buys: the cash buffer
comes off the top, weights are renormalised to 100, any stock over
`max_position_pct` is pinned at the cap and its excess shared out, slices
under the minimum order are dropped, and whole shares are bought at the
prices in `--prices trader/prices.json` (trader-alerts `quote --holdings
--write` gets them). NSE has no fractional shares and FYERS takes a quantity,
never a rupee amount, so without prices it shows the rupee split and writes
no orders. These are plain buys with no stop: trader-risk blocks them while
`require_stop` is true and counts each one against `max_trades_per_day`. Use
a separate config (`--config`) for a long-term split.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The broker's price is the one that fills.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes options,
margin or short positions.

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
