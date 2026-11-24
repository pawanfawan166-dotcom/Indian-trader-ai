---
name: trader-pelosi
description: >-
  NSE Deal Tracker. Reads the latest NSE bulk and block deal snapshot,
  flags counterparties or `.NS` symbols on your list, and alerts once per
  deal. Keeps the original index/new/fetch/parse commands and flags. NSE
  provides a current snapshot, not an equivalent historical 13F/PTR archive.
  Use when the user asks about Indian-market bulk or block deals.
argument-hint: "[counterparty or NSE ticker] [--read]"
---

# trader-pelosi

This skill's original `index`, `new`, `fetch`, and `parse` commands remain
available, but the live source is now NSE's latest official bulk/block deal
snapshot. It is not an insider-intent signal: a bulk/block deal may be
historical, and the snapshot does not establish a participant's current
position or future plans.

Tool in this folder (standard library Python):

```bash
python3 "${CLAUDE_SKILL_DIR}/ptr.py" --help
```

## What it reads and writes

- Reads `https://www.nseindia.com/api/snapshot-capital-market-largedeal?index=all`
  (bulk and block deals, quantity, counterparty, side and weighted average price).
- Writes `trader/state/pelosi.json` (deal IDs already shown) and caches the
  latest successful response in `trader/state/nse-deals-cache.json`.
- Reads `trader/portfolio.json` (if it exists) to flag NSE symbols you hold.
- `--offline FILE` accepts a saved NSE JSON snapshot (or the cache wrapper).
  `--year` filters deal dates; it does not select an NSE historical archive.

## The rules

- **Quote NSE.** Each row links to NSE's deal page. This snapshot covers
  reported bulk/block deals only; it is not promoter/insider filing coverage.
- **Amounts are INR.** Values are quantity × the reported weighted average
  price (WATP), not guaranteed execution prices.
- **Say the limitation.** The API is a latest snapshot, not a guaranteed
  complete historical archive. A cache fallback may be stale; its save time
  is reported. Without a usable cache, the command fails clearly and supports
  `--offline FILE`.

## Step 1: latest NSE deals

```bash
python3 "${CLAUDE_SKILL_DIR}/ptr.py" index
python3 "${CLAUDE_SKILL_DIR}/ptr.py" index --member RELIANCE
python3 "${CLAUDE_SKILL_DIR}/ptr.py" index --since 2026-10-01 --type BK
```

`--member` filters the counterparty or NSE symbol and may be repeated.
`--type B` selects bulk deals, `--type K` block deals, and `BK` (the default)
selects both. `--year` filters deal dates; it does not fetch historical
archive data.

## Step 2: watch for new ones

```bash
python3 "${CLAUDE_SKILL_DIR}/ptr.py" new --member RELIANCE --read
```

Shows deals not seen at the last check (on its first run, it shows those
within the last 45 days), includes their details with `--read`, and records
their IDs. Exit code 1 means nothing new. The source is a latest NSE snapshot,
not a year-by-year archive. If NSE blocks the request, a saved snapshot is
used with a note containing its save time; if no usable cache exists, the
command fails and suggests `--offline FILE`.

## Step 3: read a report

```bash
python3 "${CLAUDE_SKILL_DIR}/ptr.py" fetch DEAL_ID
python3 "${CLAUDE_SKILL_DIR}/ptr.py" parse nse-deals.json --json
```

`fetch` looks up one deal ID in the latest or cached snapshot. `parse` turns
a saved NSE response into the existing report-shaped output. The original
`--filed` and `--pdf-dir` flags remain accepted for CLI compatibility; they
do not apply to the live NSE feed.

## Example: report shape

```
NSE bulk deal (RELIANCE.NS), BUY. NSE deal DEAL_ID, disclosed YYYY-MM-DD.
  - NSE Bulk deal buy, Example Client, Reliance Industries (RELIANCE.NS):
    100 shares at ₹1,450, ₹145,000, traded YYYY-MM-DD
```

The values are examples of the output fields, not a real deal or current
quote. Amounts are computed from the reported quantity and weighted average
price; neither is a promise of an execution price.

## What it never does

- Never implies the data proves promoter/insider intent, current holdings,
  or a profitable trade.
- Never presents an NSE bulk/block deal as a live quote or recommendation.
- Never places an order.

## Safety

- Education and tooling, not financial advice. Nothing in this pack is a
  recommendation to buy or sell anything.
- Paper trading by default (`"paper": true` in `trader/config.json`).
- Nothing places an order except `/trader-broker`, and only after
  `/trader-risk` passes AND the user types an explicit yes for that order in
  the chat, AND the brokerage tool is set to "Needs approval" in Claude
  (Customize > Connectors > tool permissions) so Claude's own "Allow once /
  Deny" card appears too. Never "Always allow" an order tool.
- No options, margin, shorting or leverage unless the config allows it and
  the user confirms.
- NSE's current snapshot is not a full historical disclosure archive. It may
  be unavailable or stale; report the actual source and fallback status.
