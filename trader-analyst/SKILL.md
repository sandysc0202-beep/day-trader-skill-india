---
name: trader-analyst
description: >-
  The Earnings Analyst. Reads an NSE company's reported quarterly numbers
  (Yahoo Finance's fundamentals series for the .NS symbol) and its latest
  results filing on NSE, and sums up what changed: revenue, gross, operating
  and net margins, net profit and diluted EPS, quarter by quarter, each
  against the same quarter a year earlier, with the trailing twelve months,
  in rupees crore and India's April to March financial year (Q2 FY27).
  Reads an earnings call transcript when you paste one. Use when the user
  says "how did Reliance's results look", "analyze the results", "what
  changed this quarter", "is revenue growing", "check the quarterly
  numbers", "compare margins", "sum up the call", "does it report this week",
  or before trading a stock around its results.
argument-hint: "[NSE symbols]"
---

# trader-analyst

Results move stocks more than anything else on the calendar, and the press
release is written to put the best number first. This skill lines up the
numbers the company reported, quarter by quarter, and says plainly what
changed. For a day trader it answers two questions before a trade: what did
the last results say, and is the next one inside this trade?

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" --help
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE INFY TCS --metrics trader/metrics.json
```

## What it reads and writes

- Reads Yahoo Finance's fundamentals time series for `<SYMBOL>.NS`:
  `query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/<SYMBOL>.NS`
  with the quarterly total revenue, cost of revenue, gross profit, operating
  income, net income and diluted EPS series. It is free and unofficial, keeps
  about five quarters, and can lag or change without notice. If Yahoo answers
  401 or 403 (it sometimes wants a browser cookie), the tool says so: read
  the filing on nseindia.com instead.
- Reads NSE's financial results feed
  (`nsearchives.nseindia.com/content/RSS/Financial_Results.xml`) for the
  company's newest results filing and its link, matched by the company name
  in `trader/universe.txt` (trader-scanner writes it). The feed holds only
  the latest filings, so an older one may not be there; the tool says so.
- Writes `trader/metrics.json` with `--metrics`: one row per company (TTM
  revenue, net profit and diluted EPS, the latest margins and growth), for
  your notes or a spreadsheet. `metrics.example.json` in this folder shows
  the shape, from the invented company in the tests.
- Earnings calls: the tool does not fetch transcripts. If the user pastes one
  (or the company's investor relations page has it), read it and sum up what
  management said changed: guidance, margins, demand, anything they declined
  to answer.
- The next results date: companies announce the board meeting that approves
  results in advance. `/trader-news --feeds board` shows those filings; the
  company's investor relations page has it too. Say where the date came from.

## The rules

- **Reported numbers first, and check the filing.** The table is Yahoo's
  reading of the filings. Use the filing for context (segments, one-offs,
  standalone versus consolidated), and say which numbers came from where.
- **Compare like with like.** Growth is against the same quarter a year
  earlier. Quarter-on-quarter changes are shown but many businesses are
  seasonal (festive-season quarters, monsoon effects).
- **Every quarter is filed.** Indian listed companies report all four
  quarters, the January to March quarter included, under SEBI's listing
  rules, so no quarter is derived here.
- **Flag one-offs.** When net profit is above operating profit, other income
  or gains outside the business are inside it. The tool says so. Banks and
  insurers have no gross margin: blank, not zero.
- **Crore, not millions.** Amounts are rupees crore (1 crore = 10 million).

## Steps

1. Run it for the companies in question (or the symbols on
   `trader/watchlist.json`). `--quarters 4` shows fewer rows, `--json`
   everything machine readable.
2. Open the latest results filing (the NSE link) when the numbers need
   context, and read it.
3. Write the summary: one line on the quarter (revenue, EPS, growth), one on
   margins, one on anything unusual (other income, a one-off, data that
   lags), and the links. Plain words, real numbers, no adjectives the numbers
   do not support.
4. If the next results fall inside the planned trade (an overnight hold, or
   results before the open), say it before `/trader-exit` sets the stop: a
   stop does not protect against a gap, the stock can open past it.

## Example: the tests' invented company

`facts.py DEMO` on the test data (Demo Industries is made up):

```
Demo Industries Limited (DEMO). ₹ crore except EPS. Source: Yahoo Finance fundamentals (unofficial).

quarter end fiscal      revenue  rev yoy   gross    oper     net   net prof  EPS dil EPS yoy
2026-09-30  Q2 FY27       9,660   +15.0%   42.0%   16.0%   11.6%      1,120    14.93  +17.8%
2026-06-30  Q1 FY27       9,200   +15.0%   41.3%   15.8%   11.4%      1,050    14.00  +16.7%
...
Last 4 quarters: revenue ₹37,560.0 cr, net profit ₹4,520.0 cr, diluted EPS ₹60.27.
```

For the invented bank in the tests, net profit is above operating profit in
the latest quarter, so the tool flags it: read the filing before calling that
growth.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company reported.
- Never invents a number the data does not have.
- Never hammers a server: one request per company, one for the NSE feed, a
  pause between calls.

## Safety

Education and tooling, not financial advice. This skill only reads reported
numbers. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
