---
name: trader-budget
description: >-
  Budget Manager. Sizes every position to your budget and your max loss per
  trade. Reads the trade plan from trader-exit (entry, stop, target), finds
  the whole shares that fit inside four limits (max loss per trade, position
  cap, max order, cash after your buffer), says which limit decided it, and
  writes ONE bracket ticket (entry, stop-loss, take-profit) to
  trader/orders.json in the existing Alpaca-shaped schema. A stop
  too far away for your max loss is a plain refusal, not a smaller order.
  Nothing is placed; the schema does not make Alpaca an NSE broker. Uses NSE symbols (`.NS` for Yahoo Finance), INR (₹),
  and IST market hours (9:15-15:30). Use when the user says "how many shares",
  "size this trade", "how much should I buy", "I have ₹5,000", "I only want to risk ₹50
  a trade", "set my rules", "keep 10% in cash", or after /trader-exit writes
  a plan.
argument-hint: "[budget in INR]"
---

# trader-budget

Bare tickers default to NSE (`RELIANCE` becomes `RELIANCE.NS`). All money
values and calculations are INR (₹); existing config/output keys ending in
Use `_inr` keys for rupee limits. Legacy `_usd` keys remain read-only compatibility aliases and are interpreted as INR.
The Budget Manager turns a plan ("buy RELIANCE at ₹1,464.18, stop ₹1,458.32") into an
exact order ("buy 8 shares"), inside rules the user sets once. The stop
decides the size: the further away the stop, the fewer shares. It writes a
proposal only. trader-risk checks it and trader-broker asks the user yes or
no. Day trading loses most people money. Sizing to a max loss keeps each
loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                        # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write        # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1464.00 --json  # a limit order at ₹1,464
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights RELIANCE=40,TCS=60 --config split.json
```

`--now "2026-10-19 10:15"` pins the clock (IST). A plan for
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
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills (default ₹50).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: for the biggest position (75% of the
  budget), biggest order (₹4,000) and smallest order (₹5).
- The day limits trader-risk enforces: **max_daily_loss_inr** (₹150), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:45" / "15:30") and **flat_by** ("15:25"), all IST.
- **blocked**: tickers they never want to trade.
- Keep **paper** true until they have watched it work on a paper account.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only: the preserved Alpaca-shaped paper ticket takes no fractions in a bracket order. The size is
the smallest of four limits:

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
under the minimum, reward:risk is under `min_reward_risk`, the stop or target
is on the wrong side of the entry, the plan is for another day, the ticker is
blocked, or the plan is a short while `allow_short` is false. Directional
short paper proposals do not authorize live short selling.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupee amount
and the max loss at the stop. Say plainly that a stop is an order, not a
promise: in a fast market it can fill below the stop. Then hand over to
`/trader-risk`.

## Example: the reel

Illustrative RELIANCE.NS values (not a live quote): entry ₹1,464.18 at 10:15
IST, stop ₹1,458.32 (the opening-range midpoint), target ₹1,470.04 (1R).
Risk ₹5.86 a share. Example budget ₹50,000, 10% cash buffer, ₹50 max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | ₹50 / 5.86 | **8 (decides it)** |
| position cap | 75% of ₹50,000 = ₹37,500 / 1,464.18 | 25 |
| max order | ₹40,000 / 1,464.18 | 27 |
| cash | ₹50,000 minus ₹5,000 = ₹45,000 / 1,464.18 | 30 |

8 shares = ₹11,713.44. Max loss at the stop: 8 x 5.86 = ₹46.88. It writes:

```json
{"symbol": "RELIANCE.NS", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
 "order_class": "bracket", "take_profit_limit_price": "1470.04", "stop_loss_stop_price": "1458.32",
 "why": "opening-range breakout on RELIANCE.NS: 8 shares, bound by your ₹50 max loss per trade (8 x 5.86 = ₹46.88 at the stop)",
 "plan": {"entry": 1464.18, "stop": 1458.32, "target": 1470.04, "risk_per_share": 5.86, "max_loss": 46.88,
          "setup": "orb", "entry_time": "10:15 AM", "flat_by": "15:25"}}
```

`why` and `plan` are notes for trader-risk and trader-broker. They are never
sent to a broker. With the stop at ₹1,400 instead, one share would risk more
than the ₹50 max loss: no order.

## Weights mode (kept from v1)

`--weights RELIANCE=30,TCS=25` or `--targets FILE` (`targets.example.json`
here) splits a budget across a list by weight: the cash buffer comes off the
top, weights are renormalised to 100, any stock over `max_position_pct` is
pinned at the cap and its excess shared out, slices under the minimum order
are dropped, and amounts round to whole rupees so nothing is left idle by
accident. With `"max_position_pct": 25` and `"fractional": true`, the example
file gives an illustrative NSE split; the example data is not an investment recommendation.
With `"fractional": false` (the example config) it buys whole shares and
needs prices (`--prices trader/prices.json`). These are plain buys with no stop: trader-risk blocks them while
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
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
