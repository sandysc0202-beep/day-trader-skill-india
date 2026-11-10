---
name: trader-broker
description: >-
  Order Executor for FYERS. Sends approved orders to FYERS (API v3), paper
  trading by default, never without your yes. Shows each order as a plain
  ticket (a bracket shows its stop-loss, target and the max loss at the stop)
  plus the exact body FYERS will get, records your yes word for word, then
  Claude's own Allow once / Deny card asks again before ticket.py place sends
  it. The entry goes alone; after it fills, the stop-loss (an SL-M sell)
  follows as its own ticket. Builds the sell ticket that closes a day trade
  by your flat-by time, cancels the open stop first, records fills, and logs
  each trade's max loss so trader-risk can hold the daily limit. Also logs
  you in to FYERS each morning (tokens expire at 6:30 AM), and with that
  token the other skills read official NSE bars from FYERS instead of Yahoo.
  FYERS has no paper account: in paper mode Claude records pretend fills and
  nothing goes to FYERS. Use when the user says "place the order", "buy it",
  "execute", "send it to FYERS", "close my position", "flatten", "get me
  out", "put in my stop", "connect FYERS", "log in to FYERS", "set up FYERS",
  or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card for the
   `ticket.py place` command.

Two yeses from the user, one PASS from the rules. No yes, no order.
`ticket.py place` checks gates 1 and 2 itself and refuses without them, and
it uses each yes once.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" place --index 1          # LIVE: sends it to FYERS, records id and fill
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status paper --filled-qty 60 --filled-price 1412.45
python3 "${CLAUDE_SKILL_DIR}/ticket.py" protect --symbol RELIANCE        # the stop-loss ticket after the entry fills
python3 "${CLAUDE_SKILL_DIR}/ticket.py" cancel --broker-id 26110100012345 # LIVE: the open stop, before a flatten
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --qty 60
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 60 --price 1420.70
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log

python3 "${CLAUDE_SKILL_DIR}/fyers_api.py" login                 # each trading morning
python3 "${CLAUDE_SKILL_DIR}/fyers_api.py" positions | orders | funds | quote RELIANCE
```

`ticket.py` renders tickets, checks that a reply is a real yes for that
order, builds the stop-loss and flatten tickets, and keeps the log in
`trader/state/broker.json`. Only `place` (and `cancel`) talk to FYERS, through
`fyers_api.py` next to it. `--now "2026-10-19 10:15"` pins the clock (IST).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `protect` and `flatten` replace it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, paper, failed, cancelled and filled, with FYERS' order ids |
| `trader/portfolio.json` | cash and shares, updated by `place` (on a fill), `fill` and `result --filled-qty` |
| `trader/plan.json` | the stop for `protect`, the flat-by time for `flatten` |
| `trader/fyers.json` | your FYERS app id and redirect URL (not secret) |
| `trader/fyers.secret` or `FYERS_SECRET` | the app secret: the user puts it there, Claude never reads it out |
| `trader/state/fyers_token.json` | today's access token (expires 6:30 AM IST) |

## How an order becomes FYERS' Single Order body

| orders.json | FYERS (POST /api/v3/orders/sync) |
| --- | --- |
| `symbol` RELIANCE | `symbol` NSE:RELIANCE-EQ |
| `side` buy / sell | `side` 1 / -1 |
| `qty` | `qty` (whole shares) |
| `type` limit / market / stop / stop_limit | `type` 1 / 2 / 3 (SL-M) / 4 (SL-L) |
| `limit_price` | `limitPrice` (0 when unused) |
| `stop_price` | `stopPrice` (0 when unused) |
| `time_in_force` day / ioc | `validity` DAY / IOC |
| `product` INTRADAY / CNC | `productType` INTRADAY / CNC |
| | `disclosedQty` 0, `offlineOrder` false, `orderTag` "daytrader" |

A bracket in `orders.json` (the entry with a stop-loss and a target) is sent
as **the entry only**:

- After the entry fills, `ticket.py protect` writes the stop-loss: an SL-M
  sell for the shares held, at the plan's stop. It goes through trader-risk
  (an exit, never blocked by a day limit), its own yes and its own `place`.
- The target is NOT placed at FYERS. trader-alerts watches it. When it rings
  and the user wants out, that is `flatten`, after cancelling the open
  stop-loss. Two open sells that do not cancel each other could both fill and
  leave the user short.
- FYERS also offers a stop-loss and target attached to an order or a
  position, one cancelling the other. The pack does not use it yet: FYERS'
  docs do not say plainly whether those values are prices or offsets, and a
  wrong guess with real money is not worth it. Try it by hand in the FYERS
  app first if you want it.
- An SL-M order triggers at the stop and then sells at the market: in a fast
  drop it fills lower.

## Setup: FYERS (once)

1. A FYERS trading account, then an API app at **myapi.fyers.in** (an
   "individual" app). Give it a redirect URL; the dashboard uses
   `http://127.0.0.1:8765/fyers/callback`. Note the App ID (like
   `ABCD1234-100`) and the secret.
2. `fyers_api.py setup --app-id ABCD1234-100 --redirect <that URL>`.
3. The secret: the user sets `FYERS_SECRET` in their shell, or writes it
   into `trader/fyers.secret` themselves. **Keys never go in the chat.**
   Never ask for them, never print or read the secret back, and if the user
   pastes one anyway, tell them to delete the message and make a new secret
   at myapi.fyers.in.
4. **Each trading morning** (tokens expire at 6:30 AM IST): `fyers_api.py
   login` prints FYERS' login link; the user logs in on FYERS' own page
   (password and TOTP go there, never in the chat) and lands on the redirect
   URL; `fyers_api.py token "<that URL>"` saves today's token. The dashboard
   does this with one click.
5. **Live orders only:** SEBI's rules (from 1 April 2026) accept API orders
   only from a static IP registered with FYERS. Data needs no static IP.
6. **Permissions.** In Claude Code keep `ticket.py place` and `ticket.py
   cancel` on ask: never add them to `allow` in `.claude/settings.json`.

With today's token saved, the scanner, entry timer, exit planner, time
machine, alerts and news read FYERS' bars (official, free) instead of
Yahoo's. How fast the pack calls FYERS follows `"fyers_plan"` in
`trader/config.json`: `"standard"` (free: 4 a second, 45 a minute, 4,800 a
day, scans of up to 40 stocks on FYERS, bigger ones on Yahoo) or `"prime"`
(9 a second, 450 a minute, 4,90,000 a day, quotes 1,000 at a time, the whole
market on FYERS). Just under FYERS' own limits, counted across every tool in
`trader/state/fyers_calls.json`: the third minute over the limit blocks the
API for the day. When FYERS fails (down, logged out, the day's calls used
up), each tool says so on one line and uses Yahoo instead, unless
`"fyers_only": true` is set: then it stops with the FYERS error, so nothing is
ever decided on Yahoo's delayed prices without the user knowing. If FYERS
refuses Prime's bulk quote request (HTTP 403 on some accounts), quotes go in
normal batches of 50 and the bulk request is skipped for the day
(`trader/state/fyers_quotes.json`).
`TRADER_DATA_SOURCE=fyers` or `=yahoo` forces either; `FYERS_PLAN` overrides
the config.

**The FYERS MCP server** (optional, `claude_desktop_config.example.json`):
FYERS' own MCP at `https://mcp.fyers.in/mcp` lets Claude read positions,
orders, funds and quotes in the chat. Set every order tool there to **Needs
approval**, and place this pack's orders with `ticket.py place`, not with the
MCP's order tools: only `place` checks the risk PASS and the recorded yes.

FYERS has **no paper account**. With `"paper": true` (the default) `place`
refuses: after the user's yes, get the latest price (trader-alerts `quote`)
and record a pretend fill with `ticket.py result --status paper`. Going live
means `"paper": false` in the user's own config, set by the user.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", or a yes that names a different
   stock or amount). Ask again. Never turn a reply into a yes yourself.
4. Paper: record the pretend fill as `approve` prints. Live: only after
   "Recorded your yes", run `ticket.py place --index N`. Claude's card
   appears: Allow once or Deny (Deny: `ticket.py result --index N --status
   denied`).
5. `place` records FYERS' order id, and the fill when FYERS reports one
   within a few seconds. If FYERS refuses (margin, tick size, circuit limit,
   static IP), it is recorded as failed with FYERS' reason. A failed order is
   not retried without a new check and a new yes.

## The trailing stop (optional, off by default)

`"trailing_stop": true` in `trader/config.json` (or the **Trailing stop**
tickbox on the dashboard's Positions panel) turns on `trail.py`, long trades
only:

1. When the price reaches entry + 1R, the stop moves to the entry price.
2. After that, at each completed 5-minute candle, the stop rises to that
   candle's low if it is higher (on NSE's tick). The 1R candle itself does
   not count.
3. The stop only ever moves up. 4. The target stays as it is.

Paper: when the price reaches the stop, the exit is recorded as a paper sell
at the stop (`trail-exit` in the log). Live: the SL-M stop-loss order at
FYERS from `protect` is moved up (FYERS' modify order), so FYERS does the
selling, even with this computer asleep, and the fill is recorded when FYERS
reports it. Without that order at FYERS there is nothing to move: run
`protect` first. The app never sends a second sell for the same shares. The
dashboard checks every 5 seconds while it runs; without it, `python3
trail.py check` does one pass and `trail.py show` lists every move. Compare
it on past data first: `/trader-backtest` with `--trail`.

## Steps: while it is open, and out by the close

1. **The stop-loss, right after the entry fills:** `ticket.py protect
   --symbol SYM` (qty from `trader/portfolio.json`, or `--qty` from
   `fyers_api.py positions`; the stop from the plan, or `--stop`). Then
   `/trader-risk`, `ticket.py show`, the user's yes, and `ticket.py place`.
2. trader-alerts watches the stop, the target and the flat-by time
   (`alerts.py add --from-plan`). When the SL-M stop-loss fills, record it:
   `ticket.py fill --symbol SYM --side sell --qty Q --price P`.
3. **By `flat_by` (15:00 IST)**, or when the target alert rings and the user
   wants out:
   1. `ticket.py flatten --symbol SYM` writes a market sell (INTRADAY) and
      names the open stop-loss's FYERS id if it has one.
   2. `ticket.py cancel --broker-id ID` for that stop first (its own Allow
      once). Otherwise both sells can fill and leave the user short.
   3. `/trader-risk`. A sell that only closes what the user holds is never
      blocked by the daily limits or the entry window.
   4. `ticket.py show`, the user's yes for that one order, `ticket.py place`.
4. FYERS squares off open INTRADAY positions itself from 3:05 PM for stocks
   that have F&O contracts (3:20 PM for the rest), and charges for it. Being
   flat by 15:00 avoids that.
5. **Paper closes itself.** With `"paper": true` there is no broker to square
   off, so `flat.py` does: every open paper INTRADAY position that is past
   `flat_by` is sold at that day's 15:00 price (the close of the 5-minute
   candle that ends at 15:00; the last price if there is none). A position
   left open from an earlier day is closed at that day's price and dated that
   day, so it never counts against today's limits. The fill is logged with
   reason `flat`, the backtest's name for this exit, and the journal shows
   "flat 15:00". It never touches a live account or a CNC position. The
   dashboard runs it every few seconds and when it starts; from a terminal:
   `python3 "${CLAUDE_SKILL_DIR}/flat.py" check`.

## Example: the worked day in the tests

The test order (TSLA's real prices on the NSE clock, the tests' small ₹5,000
account), live mode:

```
ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, FYERS
  BUY 8 TSLA, market, good for today
  stop-loss 358.32
  target 370.04 (trader-alerts watches it; no target order sits at FYERS)
  max loss about ₹46.88 if the stop fills at 358.32 (it can fill worse in a fast market)
  why: opening-range breakout on TSLA: 8 shares, bound by your ₹50 max loss per trade (8 x 5.86 = ₹46.88 at the stop)
  READY: trader-risk PASS, waiting for your yes
  After your yes, ticket.py place sends FYERS (POST /api/v3/orders/sync) exactly:
    {
      "symbol": "NSE:TSLA-EQ",
      "qty": 8,
      "type": 2,
      "side": 1,
      "productType": "INTRADAY",
      "limitPrice": 0,
      "stopPrice": 0,
      "validity": "DAY",
      "disclosedQty": 0,
      "offlineOrder": false,
      "orderTag": "daytrader"
    }
  FYERS gets the entry only. After the entry fills,
  ticket.py protect writes the stop-loss (an SL-M sell at 358.32) as its own ticket, with its
  own yes. The target is watched by trader-alerts and closed with ticket.py flatten: two
  open sells that do not cancel each other could both fill and leave you short.
  Reply "yes" to place this one order, or "no" to skip it.
  Claude's own card then asks again before ticket.py place runs: Allow once sends it.
```

The user replies "yes". Claude records it and runs `ticket.py place --index
1`; the card shows that command with Allow once and Deny. After the fill,
`protect` writes the SL-M sell at 358.32 for 8 shares (`"type": 3, "side":
-1, "stopPrice": 358.32`), and that goes through its own check and its own
yes. The `why` and the plan stay in the file; they are never sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API secrets, passwords or TOTP codes.
- Never switches `"paper"` to false. The user does that themselves.
- Never places F&O, MTF (margin) or short orders on its own, never uses GTT or
  iceberg orders unasked, and never leaves two open sells for the same shares.
- Never promises a fill price: a market order and an SL-M stop fill at whatever the market gives.

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
