---
name: trader-alerts
description: >-
  Price Alerts. Rings when a price, a stop or the opening bell hits. Turns
  the trade plan into three alerts in one go (the stop, the target and "flat
  by close" at your flat-by time), plus above / below / stop / target levels,
  percent moves from the previous close, any time of day, and the NSE
  opening bell, each ringing once so you are not spammed. Knows NSE hours
  (9:15 to 3:30 IST) and the 2026 NSE trading holidays, works out India time
  itself, fetches free delayed quotes (Yahoo chart JSON, .NS symbols) one
  symbol at a time, and writes trader/prices.json for sizing and the risk
  check. Use when the user says "alert me when", "tell me if RELIANCE hits",
  "watch my stop", "watch my trade", "remind me to close by 3:00", "is the
  market open", "when does the market open", "get me a price", or right
  after /trader-exit writes a plan.
argument-hint: "[ticker] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. On FYERS the stop-loss sits at the broker
as its own SL-M order (trader-broker `protect`), but no target order sits
there: the target alert is the one that tells the user it is time to
take the gain, and the flat-by alert says when to be out.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by close from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1404.10
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1420.70
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:15 --label "flat by close" --before-close
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol INFY --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE --write         # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan tickers
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-19 15:15"` pins the clock (India time, IST); `--offline DIR`
reads saved quote files instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes, merged in, for trader-budget and trader-risk |
| `trader/portfolio.json` | the tickers held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:15") on a trading day | once per trading day |
| `opening_bell` | the NSE is open | once per trading day |

A rule with `"date"` is watched on that India date only; `--from-plan`
rules carry the plan's date, so yesterday's stop never rings today. A `time`
rule with `"before_close": true` is tied to the close: it would move with an
early close, and NSE has no scheduled ones.

## Quotes

- `yahoo`: `query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS`, an
  unofficial free JSON endpoint, one request per symbol, half a second apart.
  An NSE symbol gets `.NS` (`RELIANCE.NS`, `M%26M.NS` for M&M). On a 429 it
  waits and tries once more; on a 429 or a 5xx after that it tries `query1`.
  It identifies itself as `day-trader-skill/2.0`. (The US pack's second
  source, Stooq, has no NSE quotes, so it is gone.)
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The broker's price is the
  one that fills. Each price in `trader/prices.json` keeps its quote time, and
  trader-risk only uses one under 15 minutes old.
- With today's FYERS token (trader-broker `fyers_api.py login`) the quotes come
  from FYERS instead of Yahoo. Otherwise, if Yahoo is down, read the price with
  `fyers_api.py quote SYM` and write it into `trader/prices.json` as
  `{"as_of": "...Z", "prices": {"RELIANCE": 1412.40}}`.

## The NSE clock

Regular session 9:15 a.m. to 3:30 p.m. India time (IST), closed on weekends
and on NSE's 2026 equity trading holidays (NSE's holiday circular):

- Jan 15 (Maharashtra municipal elections), Jan 26, Mar 3 (Holi), Mar 26,
  Mar 31, Apr 3 (Good Friday), Apr 14, May 1, May 28, Jun 26, Sep 14, Oct 2,
  Oct 20 (Dussehra), Nov 10 (Diwali Balipratipada), Nov 24, Dec 25.
- NSE has no scheduled half days. The one-hour Muhurat session on Diwali
  (Sunday, Nov 8 2026; NSE announces the time in a circular) is not covered.
- NSE publishes the 2027 list in December: add it to `NSE_HOLIDAYS` in the
  tools then. Until then the clock says the holiday list is unknown for 2027.

India has no daylight saving: IST is UTC+5:30 all year. The tool works it out
itself and ignores the computer's own time zone.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the flat-by time.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the tickers in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop alert means the SL-M
   stop-loss has probably filled: check `fyers_api.py positions` (read only) and
   record the fill with trader-broker. A target alert means the price reached
   the target and nothing at the broker sold it: if the user wants out, that is
   trader-broker `flatten` (cancel the open stop-loss first), then trader-risk,
   then the user's yes. "Flat by close" means the same for anything still
   open. FYERS also squares off INTRADAY positions itself from 3:05 PM (stocks
   with F&O contracts; 3:20 PM for the rest), with a charge. Never act for them.

## Example: the worked day in the tests

The tests replay TSLA's real prices of Monday 2026-10-19 moved onto the NSE
clock (synthetic times, real prices): entry 364.18 at 10:00 AM, stop 358.32,
target 370.04, flat by 15:15. `add --from-plan` writes `tsla-plan-stop`,
`tsla-plan-target` and `tsla-flat-by` for that day. A `check` at 10:38 reads
the 10:37 quote, 366.48, between the stop and the target:

```
NSE OPEN (2026-10-19 10:38 IST, regular session).
Nothing hit. 3 rule(s) watched today, TSLA ₹366.48.
```

At 3:15 PM:

```
ALERT Flat by close: it is 15:15 IST (set for 15:15), the market closes at 15:30. If TSLA is still
open, close it: trader-broker flatten --symbol TSLA builds the sell ticket, then trader-risk and your yes.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell`, the bell rings at 9:15.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: ticker, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

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
