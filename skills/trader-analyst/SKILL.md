---
name: trader-analyst
description: >-
  The Earnings Analyst. Reads reported numbers from SEC XBRL for U.S. tickers
  and official NSE financial-result XBRL for Indian tickers. Summarizes
  quarterly revenue, margins, net income, diluted EPS, year-over-year changes,
  TTM results, and available market quotes. Use before trading around earnings.
argument-hint: "[tickers]"
---

# trader-analyst

Reads reported financial results and explains what changed, quarter by
quarter. It supports U.S. SEC filers and Indian NSE equities. This is reporting
and analysis, not a buy/sell recommendation.

Run the tool:

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" --help
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE.NS
```

For U.S. tickers, the SEC User-Agent (`sec_user_agent` in
`trader/config.json` or `SEC_USER_AGENT`) must contain your name and email.
NSE tickers ending in `.NS` use NSE filings and do not require an SEC
User-Agent. Tickers passed to this skill should use the `.NS` suffix.

## Indian NSE data

- Financial statements come from NSE's official Integrated Filing - Financials
  and quarterly corporate-results feeds, including their XBRL filing links.
- The tool prefers consolidated filings and uses standalone filings only when
  no consolidated filing is available. The selected basis is printed.
- Amounts in narrative output are INR (₹) millions; JSON metrics contain full
  INR values. EPS is in ₹ per share.
- Direct three-month values are preferred. If an explicit Q4 value is absent,
  the tool derives it from the annual filing less the nine-month cumulative
  filing when available; derived quarters and approximate EPS are marked.
- Gross and operating margins are left blank when NSE filings do not report
  comparable concepts. The tool does not infer them from unrelated line items.
- In the Streamlit dashboard, the Analyst tab falls back to Yahoo Finance
  quarterly revenue and net profit plus market cap, trailing P/E and ROE when
  NSE XBRL has no latest-quarter revenue/net-income pair. Those estimates are
  unofficial, shown in INR crore, and are separate from this CLI's filing
  output.
- An Angel One SmartAPI LTP quote is included when credentials are configured.
  Quotes are read-only and expressed in INR. Without credentials, the report
  explicitly notes that no quote was fetched. Use `--no-quote` to skip it.

Set these variables in the local environment to enable SmartAPI quotes:

```text
ANGELONE_API_KEY
ANGELONE_CLIENT_CODE
ANGELONE_PASSWORD  # Angel One client PIN
ANGELONE_TOTP_SECRET
ANGELONE_CLIENT_PUBLIC_IP
```

Optional overrides are `ANGELONE_CLIENT_LOCAL_IP` and `ANGELONE_MAC_ADDRESS`.
Do not paste credentials into chat or commit them to the repository. The tool
only uses SmartAPI login, scrip search, and the LTP quote endpoint; it does not
place or modify orders.

NSE cash-market hours are 09:15–15:30 IST. Quotes can be stale or unavailable
outside the session and on exchange holidays; verify the official NSE trading
calendar. A quote is not a forecast or investment recommendation.

## U.S. data

For U.S. tickers the tool reads SEC company facts and links the newest 10-Q or
10-K and earnings 8-K exhibit. Values remain in USD. SEC's User-Agent is
required for live requests.

## Usage

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" TSLA
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE.NS INFY.NS --quarters 8
python3 "${CLAUDE_SKILL_DIR}/facts.py" AAPL RELIANCE.NS --json
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE.NS --no-quote --metrics trader/metrics.json
```

`--json` emits machine-readable results, including currency, filing links, the
quote (when configured), and data-source notes. `--metrics FILE` merges one
row per ticker into a JSON metrics file and includes the quote when available.
`--offline DIR` is for SEC fixtures only; NSE filing analysis needs a live
connection.

## Reading the result

Compare a quarter with the same quarter a year earlier before drawing
conclusions from quarter-on-quarter growth; Indian companies can be seasonal.
Read the linked official filing when results need context, especially when a
quarter is derived or a reported margin is unavailable. The tool does not
fetch earnings-call transcripts or forecast the next report date.

Nothing in this skill places a trade. Orders belong to `/trader-broker` after
`/trader-risk` checks and the user's explicit confirmation.
