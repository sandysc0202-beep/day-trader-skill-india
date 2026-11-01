---
name: trader-news
description: >-
  News Monitor. Tracks breaking news on every NSE stock you watch and flags
  what moves it: Yahoo Finance headlines per symbol, the company's own
  filings on NSE (announcements, financial results, board meetings,
  corporate actions) and how far each stock is from yesterday's close. Shows
  only what is new since the last check, newest first, in India time (IST),
  tags each item (earnings, analyst, legal, deal, capital, halt, regulatory,
  macro...) and never repeats one. Use when the user says "any news on
  RELIANCE", "why is this moving", "what's the news on my watchlist",
  "morning briefing", "pre-market news", "monitor the news", "did anything
  break", "check the NSE announcements", or after /trader-scanner finds setups.
argument-hint: "[symbols]"
---

# trader-news

A chart can look perfect and still be about to gap on a results date, a
circuit or a SEBI order. Before a setup becomes a trade, this skill reads
the feeds that carry those first, for every stock on the watchlist, and tells
you only what is new. It also prints how far each stock has moved today, so a
headline and a move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                               # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE INFY --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --feeds announcements,results,board,actions
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-filings --json
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| `trader/universe.txt` | NSE's company names (trader-scanner `scan.py universe`), to match filings to symbols |
| Yahoo Finance headline RSS | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=IN&lang=en-IN` |
| Yahoo Finance chart endpoint | the last price against yesterday's close (unofficial, can lag) |
| NSE's RSS feeds | `nsearchives.nseindia.com/content/RSS/`: `Online_announcements.xml` and `Financial_Results.xml` by default, `Board_Meetings.xml` and `Corporate_action.xml` with `--feeds` |
| `trader/state/news.json` | what has been shown (kept 30 days) |

NSE's feeds name the company ("Reliance Industries Limited"), not the
symbol, so a filing is matched to your symbol by the company name in
`trader/universe.txt`, exactly (case, "Ltd." or "Limited" and "&" or "and"
do not matter). A symbol with no name there gets headlines only, and the tool
says so: run `/trader-scanner` `scan.py universe` once. Each feed is fetched
once per check for all your symbols. The feeds hold only the latest filings,
so check often during the session; for older ones, look on nseindia.com.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the filing and read it. Quote the source and
  link it. Never summarise a paywalled story you could not read: say so.
- **Filings beat headlines.** If an NSE filing and a headline disagree, the
  filing is what the company told the exchange.
- **Say what it touches.** Every item lists the symbols it touches and its
  tags: earnings, analyst, legal, deal, capital, people, risk, halt,
  regulatory, macro. Filings marked `!` are financial results, or say
  outcome of board meeting, resignation, insolvency, default, fraud, credit
  rating, delisting, suspension, merger or amalgamation, acquisition,
  buyback, dividend, bonus, split or pledge.
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline is worth saying out loud too. A stock at its
  circuit limit cannot be traded until it comes off it.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` filing, anything tagged earnings,
   halt, legal, regulatory or risk, and every MOVING symbol first.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "results
   are this week: an overnight gap can jump past any stop").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: a results date inside the trade, a circuit, a block deal,
   a SEBI order. Never act on a headline alone.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before the 9:15 open.

## Example: the worked day in the tests

The tests read Yahoo Finance's real TSLA headline feed of Monday 2026-10-19
(absolute times, shown in IST) and hand-made feeds in NSE's exact shape for
invented filings. A check at 19:45 IST lists the 16 TSLA headlines published
since 17:30 IST, among them "Tesla-SpaceX Merger Mania Strikes Wall Street
Ahead of Earnings" (tags: earnings, deal), and leaves out the two published
after the pinned clock. For `--tickers INFY` with a wide window:

```
  2026-10-13 17:30 IST  INFY        NSE           Infosys Limited, NSE announcement: Infosys Limited has informed ...
      https://nsearchives.nseindia.com/corporate/INFY_13102026173002_AnalystMeet.pdf
! 2026-07-16 16:15 IST  INFY        NSE           Infosys Limited, NSE financial results: Quarterly, Unaudited, ...
      tags: earnings  https://nsearchives.nseindia.com/corporate/xbrl/INFY_Q1FY27_results.xml
```

The results filing carries the `!`; the analyst meet does not. When a symbol
has no news, say so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per headline feed per symbol, one per
  NSE feed per check, a pause between calls.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
