---
name: trader-broker
description: >-
  Order Executor. Sends approved orders to Zerodha through its Kite MCP
  server, paper trading by default, never without your yes. Shows each order
  as a plain ticket (a bracket shows its stop-loss, target and the max loss at
  the stop) plus the exact arguments for Kite's place_order, records your yes
  word for word, then Claude's own Allow once / Deny card asks again. Kite has
  no bracket order for shares, so after the entry fills it writes the
  stop-loss (an SL-M sell) as its own ticket; it builds the sell ticket that
  closes a day trade by your flat-by time, records fills, and logs each
  trade's max loss so trader-risk can hold the daily limit. Zerodha has no
  paper account: in paper mode Claude records pretend fills and never calls
  Kite. Covers setting up Kite's MCP server in Claude Desktop and setting
  order tools to "Needs approval". Use when the user says "place the order",
  "buy it", "execute", "send it to Zerodha", "close my position", "flatten",
  "get me out", "put in my stop", "connect Kite", "set up Zerodha", or after
  /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the order tool is set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id 250101000123456 --filled-qty 60 --filled-price 1412.45
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status paper --filled-qty 60 --filled-price 1412.45
python3 "${CLAUDE_SKILL_DIR}/ticket.py" protect --symbol RELIANCE        # the stop-loss ticket after the entry fills
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 60 --price 1420.70
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --qty 60
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, builds the stop-loss and flatten tickets, and
keeps the log in `trader/state/broker.json`. `--now "2026-10-19 10:15"` pins
the clock (India time, IST).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `protect` and `flatten` replace it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, paper, failed and filled, with each entry's max loss |
| `trader/portfolio.json` | cash and shares, updated by `fill` and by `result --filled-qty` |
| `trader/plan.json` | the stop for `protect`, the flat-by time for `flatten` |

## How an order becomes Kite's place_order

The pack's order in `trader/orders.json` maps onto the Kite MCP server's
`place_order` arguments like this:

| orders.json | Kite place_order |
| --- | --- |
| `symbol` | `tradingsymbol` (NSE's symbol, `exchange` "NSE") |
| `side` buy / sell | `transaction_type` BUY / SELL |
| `qty` | `quantity` (whole shares) |
| `type` market / limit / stop / stop_limit | `order_type` MARKET / LIMIT / SL-M / SL |
| `limit_price` | `price` |
| `stop_price` | `trigger_price` |
| `time_in_force` day / ioc | `validity` DAY / IOC |
| `product` MIS / CNC | `product` MIS (intraday) / CNC (delivery) |
| | `variety` "regular" |

A bracket in `orders.json` (the entry with a stop-loss and a target) is sent
as **the entry only**. Kite has no bracket order for shares and no
one-cancels-the-other link between two sells, so:

- After the entry fills, `ticket.py protect` writes the stop-loss: an SL-M
  sell for the shares held, at the plan's stop, same product. It goes through
  trader-risk (an exit, never blocked by a day limit) and its own yes.
- The target is NOT placed at the broker. trader-alerts watches it. When it
  rings and the user wants out, that is `flatten`, after cancelling the open
  stop-loss. Two open sells that do not cancel each other could both fill and
  leave the user short.
- An SL-M order triggers at the stop and then sells at the market: in a fast
  drop it fills lower. If Kite rejects SL-M for a stock, use a stop-limit
  (`type` stop_limit, a `limit_price` a little under the stop) and say that it
  may not fill at all in a fast drop.

## Setup: Zerodha's Kite MCP server (Claude Desktop)

Zerodha publishes an official MCP server, github.com/zerodha/kite-mcp-server.
Checked 2026-11-01.

- **The hosted server** (`https://mcp.kite.trade/mcp`) is the easy start. The
  user adds it in Claude Desktop (Settings > Developer > Edit Config, the
  block in `claude_desktop_config.example.json` here), restarts, and logs in
  with Kite's `login` tool: it gives a link that opens Zerodha's own login
  page. The password and the TOTP go into Zerodha's page, never into the
  chat. Zerodha's README says the hosted version leaves out the potentially
  destructive trading tools, so expect it to read (quotes, holdings,
  positions, orders) but not place orders. That is enough for paper mode.
- **Placing real orders** needs the server running on the user's own
  computer with their own Kite Connect API key and secret (a Kite Connect
  developer app; check Zerodha's current terms and pricing). The server's
  README covers building it, the `.env` file with `KITE_API_KEY` and
  `KITE_API_SECRET`, and the Claude Desktop entry. **Keys never go in the
  chat.** Never ask for them, never read the `.env` or the config back into
  the conversation, and if the user pastes a key anyway, tell them to delete
  the message and make a new key in the Kite Connect console.
  `EXCLUDED_TOOLS` in that `.env` can switch off tools they will never use
  (for example `place_gtt_order,modify_gtt_order,delete_gtt_order`).
- **Set the tool permissions.** Customize > Connectors (Settings > Connectors
  in older versions), open kite, and for each tool:
  - `place_order`: **Needs approval**. Never Always allow.
  - `modify_order`, `cancel_order`, `place_gtt_order`, `modify_gtt_order`,
    `delete_gtt_order`: Needs approval.
  - read-only tools (`get_profile`, `get_margins`, `get_holdings`,
    `get_positions`, `get_orders`, `get_trades`, `get_order_history`,
    `get_quotes`, `get_ltp`, `get_ohlc`, `get_historical_data`,
    `search_instruments`): Always allow is fine.
- In Claude Code instead of Claude Desktop, keep the order tool on ask in
  `.claude/settings.json`: `"permissions": {"ask": ["mcp__kite__place_order"]}`,
  and never put an order tool in `allow`.

Zerodha has **no paper account**. With `"paper": true` (the default) Claude
never calls `place_order`: after the user's yes it gets the latest price
(trader-alerts `quote`, or Kite's read-only `get_ltp`) and records a pretend
fill with `ticket.py result --status paper`. Going live means
`"paper": false` in the user's own config, set by the user.

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
   "Recorded your yes", call `place_order` with exactly the arguments the
   ticket printed. Claude's card appears: Allow once or Deny.
5. Record what happened: `ticket.py result --index N --status placed
   --broker-id <order id> --filled-qty Q --filled-price P` (the fill from
   `get_order_history` or `get_trades`), or `--status denied`, or
   `--status failed` with `--note` (Kite's rejection reason: margin, tick
   size, circuit limit). A failed order is not retried without a new check
   and a new yes.

## Steps: while it is open, and out by the close

1. **The stop-loss, right after the entry fills:** `ticket.py protect
   --symbol SYM` (qty from `trader/portfolio.json`, or `--qty` from
   `get_positions`; the stop from the plan, or `--stop`). Then
   `/trader-risk`, `ticket.py show`, the user's yes, and `place_order`.
2. trader-alerts watches the stop, the target and the flat-by time
   (`alerts.py add --from-plan`). When the SL-M stop-loss fills, record it:
   `ticket.py fill --symbol SYM --side sell --qty Q --price P`.
3. **By `flat_by` (15:15 IST)**, or when the target alert rings and the user
   wants out, anything still open gets closed:
   1. `get_positions` (read only): how many shares are held.
   2. `get_orders` (read only): is the stop-loss still open? If so,
      `cancel_order` it first (Needs approval, its own yes). Otherwise Kite
      could sell twice and leave the user short.
   3. `ticket.py flatten --symbol SYM --qty Q` writes a market sell (MIS).
   4. `/trader-risk`. A sell that only closes what the user holds is never
      blocked by the daily limits or the entry window.
   5. `ticket.py show`, the user's yes for that one order, `place_order`,
      then `result`.
4. Zerodha squares off open intraday (MIS) positions itself from about
   3:20 PM, and charges for it. Being flat by 15:15 avoids that.

## Example: the worked day in the tests

The test order (TSLA's real prices on the NSE clock, the tests' small ₹5,000
account), live mode:

```
ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, Zerodha Kite
  BUY 8 TSLA, market, good for today
  stop-loss 358.32
  target 370.04 (trader-alerts watches it; Kite holds no target order)
  max loss about ₹46.88 if the stop fills at 358.32 (it can fill worse in a fast market)
  why: opening-range breakout on TSLA: 8 shares, bound by your ₹50 max loss per trade (8 x 5.86 = ₹46.88 at the stop)
  READY: trader-risk PASS, waiting for your yes
  Claude will call place_order with exactly:
    {
      "variety": "regular",
      "exchange": "NSE",
      "tradingsymbol": "TSLA",
      "transaction_type": "BUY",
      "quantity": 8,
      "product": "MIS",
      "order_type": "MARKET",
      "validity": "DAY"
    }
  Kite places the entry only: it has no bracket order for shares. After the entry fills,
  ticket.py protect writes the stop-loss (an SL-M sell at 358.32) as its own ticket, with its
  own yes. The target is watched by trader-alerts and closed with ticket.py flatten: two
  open sells that do not cancel each other could both fill and leave you short.
  Reply "yes" to place this one order, or "no" to skip it.
  Claude's own card then asks again: Allow once places it, Deny stops it.
```

The user replies "yes". Claude records it and calls `place_order`; the card
reads "Claude wants to use Place order" with those arguments and two buttons:
Allow once, Deny. After the fill, `protect` writes the SL-M sell at 358.32
for 8 shares: `{"transaction_type": "SELL", "quantity": 8, "product": "MIS",
"order_type": "SL-M", "trigger_price": 358.32, ...}`, and that goes through
its own check and its own yes. The `why` and the plan stay in the file; they
are never sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, passwords or TOTP codes.
- Never switches `"paper"` to false. The user does that themselves.
- Never places F&O, MTF (margin) or short orders on its own, never uses GTT or
  iceberg orders unasked, and never leaves two open sells for the same shares.
- Never promises a fill price: a market order and an SL-M stop fill at whatever the market gives.

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
