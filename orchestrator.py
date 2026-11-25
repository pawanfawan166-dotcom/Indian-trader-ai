"""Strict, paper-only NSE qualification pipeline over the eleven trader skills."""

import datetime
import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yfinance as yf

from nse_live import get_live_quote


SKILLS = (
    "trader-scanner",
    "trader-entry",
    "trader-exit",
    "trader-timemachine",
    "trader-analyst",
    "trader-news",
    "trader-pelosi",
    "trader-budget",
    "trader-risk",
    "trader-broker",
    "trader-alerts",
)

FNO_SYMBOLS = (
    "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "TCS.NS",
    "INFY.NS", "BHARTIARTL.NS", "ITC.NS", "LT.NS", "AXISBANK.NS",
    "BAJFINANCE.NS", "MARUTI.NS", "SUNPHARMA.NS", "TATAMOTORS.NS", "M&M.NS",
    "TITAN.NS", "KOTAKBANK.NS", "NTPC.NS", "ADANIENT.NS", "TATASTEEL.NS",
    "HINDALCO.NS", "JSWSTEEL.NS", "POWERGRID.NS", "ONGC.NS", "COALINDIA.NS",
    "INDUSINDBK.NS", "BAJAJFINSV.NS", "NESTLEIND.NS", "ASIANPAINT.NS", "TECHM.NS",
    "GRASIM.NS", "DRREDDY.NS", "CIPLA.NS", "APOLLOHOSP.NS", "EICHERMOT.NS",
    "TATACONSUM.NS", "BPCL.NS", "BRITANNIA.NS", "ADANIPORTS.NS", "BEL.NS",
    "TRENT.NS", "SHRIRAMFIN.NS", "JIOFIN.NS", "LTIM.NS", "BAJAJ-AUTO.NS",
    "HDFCLIFE.NS", "SBILIFE.NS", "HINDUNILVR.NS", "WIPRO.NS", "PFC.NS",
)


def ensure_portfolio(root):
    """Create the documented INR starter account only when no account exists."""
    portfolio = Path(root) / "trader" / "portfolio.json"
    if not portfolio.exists():
        portfolio.parent.mkdir(parents=True, exist_ok=True)
        portfolio.write_text(
            json.dumps(
                {"currency": "INR", "cash": 100000, "positions": []},
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    return portfolio


def _json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start >= 0:
            try:
                return json.loads(text[start:])
            except ValueError:
                pass
    return None


def _run(root, skill, script, args, timeout=150):
    path = Path(root) / "skills" / skill / script
    try:
        result = subprocess.run(
            [sys.executable, str(path), *map(str, args)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", f"{type(exc).__name__}: {exc}"


def _record(rows, index, skill, status, summary, output=""):
    rows[index] = {
        "skill": skill,
        "status": status,
        "summary": str(summary)[:500],
        "output": str(output or summary)[:6000],
    }


def _replay_passes(mc, minimum_win_probability=0.40):
    """An R:R >= 1.5 plan needs 40% wins to break even; require positive replay EV."""
    try:
        probability = float(mc.get("p_green"))
        expected_value = float(mc.get("avg_R"))
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        math.isfinite(probability)
        and math.isfinite(expected_value)
        and probability >= minimum_win_probability
        and expected_value > 0
    )


def _funnel_snapshot(items):
    return [dict(item["_funnel"]) for item in items if isinstance(item.get("_funnel"), dict)]


def _merge_order_proposals(existing, proposals):
    """Replace stale proposals for the same symbol/side; retain unrelated orders."""
    replaced = {(order.get("symbol"), order.get("side")) for order in proposals}
    retained = [
        order for order in existing
        if (order.get("symbol"), order.get("side")) not in replaced
    ]
    return retained + proposals


def _regime():
    try:
        nifty = yf.Ticker("^NSEI").history(period="3mo", interval="1d", auto_adjust=False)
        vix = yf.Ticker("^INDIAVIX").history(period="5d", interval="1d", auto_adjust=False)
        if nifty.empty or vix.empty or len(nifty["Close"].dropna()) < 20:
            return {
                "mood": "Caution",
                "reason": "Nifty 50 20-EMA or India VIX data is unavailable; regime is unconfirmed.",
            }
        close = float(nifty["Close"].dropna().iloc[-1])
        ema20 = float(nifty["Close"].dropna().ewm(span=20, adjust=False).mean().iloc[-1])
        vix_value = float(vix["Close"].dropna().iloc[-1])
        above = close > ema20
        mood = "Bullish" if above and vix_value < 20 else (
            "Bearish" if not above and vix_value >= 20 else "Caution"
        )
        return {
            "mood": mood,
            "nifty": close,
            "ema20": ema20,
            "india_vix": vix_value,
            "reason": (
                f"Nifty 50 {close:,.2f} is {'above' if above else 'at/below'} its 20-EMA "
                f"{ema20:,.2f}; India VIX {vix_value:.2f}."
            ),
        }
    except Exception as exc:
        return {"mood": "Caution", "reason": f"Market regime data unavailable: {exc}"}


def _symbol(value):
    text = "".join(str(value or "").upper().split())
    while text.endswith(".NS"):
        text = text[:-3]
    return text + ".NS" if text else ""


def _numeric_volume(*values):
    for value in values:
        if value is None:
            continue
        try:
            number = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            return number
    return None


def _candidate_volume(row):
    return _numeric_volume(
        row.get("average_daily_volume"),
        row.get("avg_daily_volume"),
        row.get("average_volume"),
        row.get("avg_volume"),
    )


def _scanner_setup_rows(scan_doc):
    """Join setup hits to full scanner rows and sort by available liquidity."""
    stocks = {
        _symbol(row.get("symbol")): row
        for row in (scan_doc.get("stocks") or [])
        if isinstance(row, dict) and row.get("symbol")
    }
    candidates = {}
    volume_fields = {
        "average_daily_volume",
        "avg_daily_volume",
        "average_volume",
        "avg_volume",
        "volume",
        "regularMarketVolume",
        "shares",
    }
    for hit in (scan_doc.get("hits") or []):
        if not isinstance(hit, dict) or not hit.get("symbol"):
            continue
        ticker = _symbol(hit["symbol"])
        row = dict(stocks.get(ticker, {}))
        for key, value in hit.items():
            if value is None or (
                key in volume_fields and _numeric_volume(value) is None
            ):
                continue
            if key in volume_fields:
                previous = _numeric_volume(row.get(key))
                incoming = _numeric_volume(value)
                row[key] = max(previous or 0, incoming or 0)
                continue
            row[key] = value
        row["symbol"] = ticker
        try:
            price = float(str(row.get("price") or 0).replace(",", "").strip())
        except (TypeError, ValueError):
            price = 0.0
        if not math.isfinite(price) or price <= 0:
            continue
        row["price"] = price
        volume = _candidate_volume(row) or 0
        row["_liquidity_volume"] = volume
        candidates[ticker] = row
    return sorted(
        candidates.values(),
        key=lambda row: (
            -row["_liquidity_volume"] * float(row["price"]),
            row["symbol"],
        ),
    )


def _scanner_setup_candidates(scan_doc, min_volume=100000, average_volumes=None):
    """Return setup hits whose populated average daily volume clears the threshold."""
    average_volumes = average_volumes or {}
    candidates = []
    for row in _scanner_setup_rows(scan_doc):
        ticker = row["symbol"]
        volume = _candidate_volume(row)
        if volume is None and ticker in average_volumes:
            volume = _numeric_volume(average_volumes[ticker])
            if volume is not None:
                row["average_daily_volume"] = volume
        if volume is None:
            continue
        row["average_daily_volume"] = volume
        row["liquid"] = volume >= min_volume
        if not row["liquid"]:
            continue
        row["_liquidity_volume"] = volume
        candidates.append(row)
    return sorted(
        candidates,
        key=lambda row: (
            -row["_liquidity_volume"] * float(row["price"]),
            row["symbol"],
        ),
    )


def _fetch_average_volumes(tickers):
    """Fetch recent average daily shares for a bounded set missing scan volume."""
    if not tickers:
        return {}, ""
    try:
        history = yf.download(
            tickers=tickers,
            period="1mo",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
            timeout=20,
        )
    except Exception as exc:
        return {}, f"Yahoo volume fallback failed: {exc}"
    if history is None or history.empty:
        return {}, "Yahoo volume fallback returned no daily bars."

    averages = {}
    multi_index = isinstance(history.columns, pd.MultiIndex)
    for ticker in tickers:
        try:
            ticker_frame = history[ticker] if multi_index else history
            if "Volume" not in ticker_frame.columns:
                continue
            volumes = ticker_frame["Volume"].dropna().tail(20)
            if volumes.empty:
                continue
            average = float(volumes.mean())
            if math.isfinite(average) and average > 0:
                averages[ticker] = average
        except (KeyError, TypeError, ValueError):
            continue
    return averages, ""


def _isolated_config(root, portfolio):
    config_path = Path(root) / "trader" / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, ValueError):
        config = {}
    try:
        cash = float(portfolio.get("cash", 100000))
    except (TypeError, ValueError):
        cash = 100000.0
    if not math.isfinite(cash) or cash <= 0:
        cash = 100000.0
    def configured_limit(key, legacy_key, default):
        value = config.get(key, config.get(legacy_key, default))
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        return parsed if math.isfinite(parsed) and parsed >= 0 else float(default)

    daily_loss_limit = configured_limit("max_daily_loss_inr", "max_daily_loss_usd", 150)
    per_trade_limit = configured_limit("max_loss_per_trade_inr", "max_loss_per_trade_usd", daily_loss_limit)
    cap = round(min(cash * 0.01, per_trade_limit, daily_loss_limit), 2)
    max_order = configured_limit("max_order_inr", "max_order_usd", 4000)
    try:
        min_rr = float(config.get("min_reward_risk", 1.5))
    except (TypeError, ValueError):
        min_rr = 1.5
    config.update({
        "budget": cash,
        "cash_buffer_pct": config.get("cash_buffer_pct", 0),
        "max_order_inr": max_order,
        "max_daily_loss_inr": daily_loss_limit,
        "max_loss_per_trade_inr": cap,
        "min_reward_risk": max(1.5, min_rr),
        "paper": True,
    })
    return config, cash


def run_autopilot(root, progress, mode="fo_intraday"):
    """Run the selected qualification pipeline and return plans plus funnel evidence."""
    if mode not in ("fo_intraday", "cash_swing"):
        raise ValueError("mode must be 'fo_intraday' or 'cash_swing'")
    root = Path(root)
    ensure_portfolio(root)
    market = _regime()
    verification = [None] * len(SKILLS)
    candidates = []
    qualified = []
    portfolio = json.loads((root / "trader" / "portfolio.json").read_text(encoding="utf-8"))
    config, budget = _isolated_config(root, portfolio)

    def report(index, message, tickers=None):
        names = list(dict.fromkeys(_symbol(ticker) for ticker in (tickers or []) if ticker))
        if names:
            message = f"{message} · {len(names)} passed: {', '.join(names)}"
        progress(index + 1, SKILLS[index], message)

    with tempfile.TemporaryDirectory(prefix="nse-autopilot-") as temp:
        temp_path = Path(temp)
        config_path = temp_path / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        scan_path = root / "trader" / "watchlist.json"
        if mode == "fo_intraday":
            scan_args = [
                "orb", "--symbols", ",".join(FNO_SYMBOLS), "--json",
                "--min-volume", "0", "--out", str(scan_path),
            ]
            scan_message = f"scanning {len(FNO_SYMBOLS)} liquid F&O underlyings with 5-minute ORB bars"
        else:
            scan_args = [
                "daily", "--json", "--min-volume", "1000", "--out", str(scan_path),
            ]
            scan_message = "scanning the NSE cash universe with daily bars"
        report(0, scan_message)
        code, stdout, stderr = _run(
            root, SKILLS[0], "scan.py",
            scan_args,
            timeout=240,
        )
        scan_doc = _json(stdout)
        if code not in (0, 1) or not isinstance(scan_doc, dict):
            _record(verification, 0, SKILLS[0], "FAIL", stderr or stdout or "No scanner JSON output.")
            _record_skipped(verification, 1, "No scanner output; candidates cannot be evaluated.")
            _record_skipped(verification, 2, "No scanner output; candidates cannot be evaluated.")
            _record_skipped(verification, 3, "No scanner output; candidates cannot be evaluated.")
            _record_skipped(verification, 4, "No candidates survived the scanner.")
            _record_skipped(verification, 5, "No candidates survived the scanner.")
            _record_skipped(verification, 6, "No candidates survived the scanner.")
            _record_skipped(verification, 7, "No candidate plans to size.")
            _record_skipped(verification, 8, "No proposed orders to risk-check.")
            _record_skipped(verification, 9, "No risk-approved orders to display.")
            _record_skipped(verification, 10, "No qualified trades for alerts.")
            report(0, "scanner failed; all candidates rejected")
            return {"market": market, "qualified": [], "candidates": [], "verification": verification}
        scanned_rows = scan_doc.get("stocks") or scan_doc.get("hits") or []
        setup_rows = _scanner_setup_rows(scan_doc)
        fallback_tickers = [
            row["symbol"]
            for row in setup_rows
            if not _numeric_volume(
                row.get("average_daily_volume"),
                row.get("avg_daily_volume"),
                row.get("average_volume"),
                row.get("avg_volume"),
            )
        ][:100]
        fallback_volumes, volume_fallback_note = _fetch_average_volumes(fallback_tickers)
        liquid_candidates = _scanner_setup_candidates(
            scan_doc, min_volume=100000, average_volumes=fallback_volumes
        )
        shortlisted = liquid_candidates[:10]
        quote_outputs = []
        candidates = []
        for row in shortlisted:
            ticker = _symbol(row["symbol"])
            quote = get_live_quote(ticker, timeout=3)
            row_with_quote = dict(row)
            row_with_quote["live_quote"] = quote
            row_with_quote["_funnel"] = {
                "Symbol": ticker,
                "Direction": str(row.get("direction") or "LONG").upper(),
                "Entry": None,
                "SL": None,
                "Target": None,
                "R:R": None,
                "Time Machine Win %": None,
                "Expected Value (R)": None,
                "Passed/Rejected Reason": "Awaiting trader-entry validation",
            }
            candidates.append(row_with_quote)
            if quote is None:
                quote_outputs.append(f"{ticker}: no live quote; entry checks still proceed.")
            else:
                quote_outputs.append(
                    f"{ticker}: {quote['source']}, LTP={quote['price']}, "
                    f"day_high={quote.get('day_high')}, day_low={quote.get('day_low')}, "
                    f"change_pct={quote.get('change_pct')}, volume={quote.get('volume')}."
                )
        if volume_fallback_note:
            quote_outputs.append(volume_fallback_note)
        if len(fallback_tickers) > 100:
            quote_outputs.append(
                f"Yahoo average-volume fallback bounded to first 100 of "
                f"{len(fallback_tickers)} setup candidates without scanner averages."
            )
        _record(
            verification, 0, SKILLS[0], "PASS" if candidates else "WARN",
            f"Scanned {scan_doc.get('scanned', 0)}; full rows {len(scanned_rows)}; "
            f"{len(liquid_candidates)} setup candidates passed the 100,000-share liquidity "
            f"threshold; top {len(candidates)} were sent to trader-entry.",
            (stdout or stderr) + "\nQuote checks:\n" + "\n".join(quote_outputs),
        )
        report(
            0,
            f"{scan_doc.get('scanned', 0)} scanned; {len(candidates)} top liquid setup "
            "candidates passed to trader-entry",
            [row["symbol"] for row in candidates],
        )

        entry_ok = []
        entry_outputs = []
        for row in candidates:
            ticker = _symbol(row["symbol"])
            code, out, err = _run(root, SKILLS[1], "entry.py", ["check", ticker, "--json"])
            entry_outputs.append(f"{ticker} [{code}]: {out or err or 'No output.'}")
            result = _json(out)
            if code not in (0, 1) or not isinstance(result, dict):
                row["_funnel"]["Passed/Rejected Reason"] = f"Entry check unavailable: {err or 'invalid output'}"
                continue
            if _symbol(result.get("symbol")) != ticker or result.get("fallback_session"):
                row["_funnel"]["Passed/Rejected Reason"] = "Entry rejected: stale fallback or symbol mismatch"
                continue
            trigger = result.get("trigger") or {}
            opening = result.get("opening_range") or {}
            price = trigger.get("price")
            if result.get("status") != "triggered" or not price or not opening.get("high") or not opening.get("low"):
                row["_funnel"]["Passed/Rejected Reason"] = (
                    f"Entry not triggered ({result.get('status') or 'incomplete data'})"
                )
                continue
            row["_funnel"].update({
                "Entry": price,
                "Passed/Rejected Reason": "Passed trader-entry; awaiting exit plan",
            })
            entry_ok.append({
                "ticker": ticker,
                "symbol": ticker,
                "entry": result,
                "source": out or err,
                "quote": row["live_quote"],
                "_funnel": row["_funnel"],
            })
        _record(
            verification, 1, SKILLS[1],
            "PASS" if entry_ok else "WARN",
            f"Fresh, ticker-matched triggered entries: {len(entry_ok)} of {len(candidates)}; "
            "NO-DATA, waiting, stale-fallback, and mismatched outputs were rejected.",
            "\n".join(entry_outputs) or "No scanner candidates were available for entry checks.",
        )
        report(1, f"{len(entry_ok)} fresh entries passed; all others rejected", [item["ticker"] for item in entry_ok])

        exit_ok = []
        exit_outputs = []
        for item in entry_ok:
            result = item["entry"]
            opening = result["opening_range"]
            args = [
                "plan", "--symbol", item["ticker"], "--entry", str(result["trigger"]["price"]),
                "--or-high", str(opening["high"]), "--or-low", str(opening["low"]),
                "--setup", "orb", "--date", str(result.get("date") or datetime.date.today()),
                "--json", "--config", str(config_path),
            ]
            entry_time = (result.get("trigger") or {}).get("time") or result.get("time")
            if entry_time:
                args += ["--time", str(entry_time)]
            code, out, err = _run(root, SKILLS[2], "exit.py", args)
            exit_outputs.append(f"{item['ticker']} [{code}]: {out or err or 'No output.'}")
            plan = _json(out)
            if (
                code != 0 or not isinstance(plan, dict)
                or _symbol(plan.get("symbol")) != item["ticker"]
                or float(plan.get("reward_risk") or 0) < 1.5
            ):
                item["_funnel"]["Passed/Rejected Reason"] = (
                    f"Exit plan rejected: {err or 'invalid plan or R:R below 1.5'}"
                )
                continue
            item.update(plan=plan, exit_output=out or err)
            item["_funnel"].update({
                "Entry": plan.get("entry"),
                "SL": plan.get("stop"),
                "Target": plan.get("target"),
                "R:R": plan.get("reward_risk"),
                "Passed/Rejected Reason": "Passed trader-exit; awaiting Time Machine",
            })
            exit_ok.append(item)
        _record(
            verification, 2, SKILLS[2], "PASS" if exit_ok else "WARN",
            f"Fresh exit plans with R:R >= 1.5: {len(exit_ok)} of {len(entry_ok)}.",
            "\n".join(exit_outputs) or "No fresh entries were available for exit-plan checks.",
        )
        report(2, f"{len(exit_ok)} exit plans passed R:R >= 1.5", [item["ticker"] for item in exit_ok])

        replay_ok = []
        replay_outputs = []
        for item in exit_ok:
            plan_path = temp_path / f"{item['ticker'].replace('.', '_')}-plan.json"
            plan_path.write_text(json.dumps(item["plan"]), encoding="utf-8")
            code, out, err = _run(
                root, SKILLS[3], "timemachine.py",
                ["run", "--plan", str(plan_path), "--replays", "500", "--until", "15:20", "--json"],
                timeout=240,
            )
            replay = _json(out)
            replay_outputs.append(out or err)
            mc = replay.get("mc") if isinstance(replay, dict) else None
            if isinstance(mc, dict):
                item["_funnel"].update({
                    "Time Machine Win %": round(float(mc.get("p_green") or 0) * 100, 1),
                    "Expected Value (R)": mc.get("avg_R"),
                })
            if code != 0 or not isinstance(mc, dict) or not _replay_passes(mc):
                if isinstance(mc, dict):
                    item["_funnel"]["Passed/Rejected Reason"] = (
                        f"Time Machine rejected: win {float(mc.get('p_green') or 0):.1%}, "
                        f"EV {float(mc.get('avg_R') or 0):+.3f}R; requires >=40% and EV > 0"
                    )
                else:
                    item["_funnel"]["Passed/Rejected Reason"] = f"Time Machine unavailable: {err or 'no replay output'}"
                continue
            item["timemachine"] = replay
            item["_funnel"]["Passed/Rejected Reason"] = "Passed Time Machine; awaiting research and risk checks"
            replay_ok.append(item)
        _record(
            verification, 3, SKILLS[3], "PASS" if replay_ok else "WARN",
            f"500-path replay passed win probability >= 40% and expected value > 0: "
            f"{len(replay_ok)} of {len(exit_ok)}.",
            "\n".join(replay_outputs) or "No candidate had a fresh exit plan to replay.",
        )
        report(3, f"{len(replay_ok)} passed replay probability and expected-value gates", [item["ticker"] for item in replay_ok])

        research = []
        research_outputs = []
        for item in replay_ok:
            ticker = item["ticker"]
            plan_path = temp_path / f"{ticker.replace('.', '_')}-plan.json"
            plan_path.write_text(json.dumps(item["plan"]), encoding="utf-8")
            analyst_code, analyst_out, analyst_err = _run(
                root, SKILLS[4], "facts.py",
                [ticker, "--no-quote", "--json", "--no-filings"],
                timeout=180,
            )
            analyst = _json(analyst_out)
            news_code, news_out, news_err = _run(
                root, SKILLS[5], "news.py",
                ["--tickers", ticker, "--watchlist", str(scan_path), "--plan", str(plan_path),
                 "--json", "--no-sec", "--no-moves", "--dry-run", "--all", "--hours", "48"],
                timeout=150,
            )
            news = _json(news_out)
            deal_code, deal_out, deal_err = _run(
                root, SKILLS[6], "ptr.py",
                ["index", "--json", "--limit", "200", "--type", "BK", "--member", ticker],
                timeout=180,
            )
            deals = _json(deal_out)
            if analyst_code in (0, 1) and isinstance(analyst, list):
                item["analyst"] = analyst
            if not item.get("analyst"):
                try:
                    info = yf.Ticker(ticker).get_info() or {}
                    item["analyst"] = [{
                        "ticker": ticker,
                        "result": {
                            "entity": info.get("shortName") or info.get("longName") or ticker,
                            "source": "Yahoo Finance fallback · unofficial",
                            "currency": info.get("currency"),
                            "market_cap": info.get("marketCap"),
                            "trailing_pe": info.get("trailingPE"),
                            "return_on_equity": info.get("returnOnEquity"),
                        },
                    }]
                    analyst_err = (analyst_err + "\n" if analyst_err else "") + \
                        "NSE facts unavailable; supplied yfinance info fallback."
                except Exception as exc:
                    analyst_err = (analyst_err + "\n" if analyst_err else "") + \
                        f"yfinance fallback unavailable: {exc}"
            if news_code in (0, 1) and isinstance(news, dict):
                item["news"] = news
                if any(term in f"{row.get('title', '')} {row.get('summary', '')}".lower()
                       for row in news.get("items", [])
                       for term in ("fraud", "default", "lawsuit", "probe", "bankruptcy", "recall")):
                    item["negative_news"] = True
            if deal_code in (0, 1) and isinstance(deals, list):
                item["deals"] = deals
                item["conviction_score"] = min(3, len(deals))
            research.append(item)
            research_outputs.append(
                f"{ticker} analyst[{analyst_code}]: {analyst_out or analyst_err}\n"
                f"news[{news_code}]: {news_out or news_err}\n"
                f"deals[{deal_code}]: {deal_out or deal_err}"
            )
        for index, code, label, field in (
            (4, "analyst", "Fundamentals", "analyst"),
            (5, "news", "Ticker-matched headlines and negative-news scan", "news"),
            (6, "deals", "NSE bulk/block-deal lookup", "deals"),
        ):
            success = any(item.get(field) is not None for item in research)
            _record(
                verification, index, SKILLS[index], "PASS" if success else "WARN",
                f"{label}: outputs received for {sum(item.get(field) is not None for item in research)} "
                f"of {len(research)} candidates.",
                "\n".join(research_outputs) or "No candidates survived the replay gate.",
            )
            report(index, f"{label}: results recorded for {len(research)} candidate(s)", [item["ticker"] for item in research])

        sized = []
        risk_passed = []
        budget_outputs = []
        risk_outputs = []
        for item in research:
            ticker = item["ticker"]
            plan_path = temp_path / f"{ticker.replace('.', '_')}-plan.json"
            orders_path = temp_path / f"{ticker.replace('.', '_')}-orders.json"
            risk_path = temp_path / f"{ticker.replace('.', '_')}-risk.json"
            plan_path.write_text(json.dumps(item["plan"]), encoding="utf-8")
            size_code, size_out, size_err = _run(
                root, SKILLS[7], "size.py",
                ["--plan", str(plan_path), "--config", str(config_path),
                 "--portfolio", str(root / "trader" / "portfolio.json"),
                 "--budget", str(budget), "--write", str(orders_path), "--json"],
            )
            sizing_doc = _json(size_out)
            budget_outputs.append(f"{ticker} [{size_code}]: {size_out or size_err}")
            if (
                size_code != 0 or not isinstance(sizing_doc, dict)
                or not sizing_doc.get("orders")
            ):
                reason = (sizing_doc.get("sizing") or {}).get("refusal") if isinstance(sizing_doc, dict) else None
                item["_funnel"]["Passed/Rejected Reason"] = f"Budget rejected: {reason or size_err or 'no paper order'}"
                continue
            sizing = sizing_doc.get("sizing") or {}
            if float(sizing.get("max_loss") or 0) > budget * 0.01 + 0.01:
                item["_funnel"]["Passed/Rejected Reason"] = "Budget rejected: calculated loss exceeds 1% account cap"
                continue
            item["sizing"] = sizing
            item["_funnel"]["Passed/Rejected Reason"] = "Passed INR sizing; awaiting trader-risk"
            sized.append(item)
            risk_code, risk_out, risk_err = _run(
                root, SKILLS[8], "check.py",
                ["--orders", str(orders_path), "--config", str(config_path),
                 "--portfolio", str(root / "trader" / "portfolio.json"), "--json",
                 "--out", str(risk_path)],
            )
            risk_doc = _json(risk_out)
            risk_outputs.append(f"{ticker} [{risk_code}]: {risk_out or risk_err}")
            results = risk_doc.get("results") if isinstance(risk_doc, dict) else []
            if risk_code != 0 or not results or any(row.get("verdict") != "PASS" for row in results):
                reasons = [reason for row in results for reason in row.get("reasons", [])]
                item["_funnel"]["Passed/Rejected Reason"] = (
                    "Risk rejected: " + "; ".join(reasons)
                    if reasons else f"Risk rejected: {risk_err or 'risk verdict did not PASS'}"
                )
                continue
            item.update(sizing=sizing, paper_order=sizing_doc["orders"][0], risk=risk_doc)
            item["_orders_path"] = str(orders_path)
            item["_risk_path"] = str(risk_path)
            item["_funnel"].update({
                "Direction": "LONG" if item["plan"].get("side", "buy") == "buy" else "SHORT",
                "Passed/Rejected Reason": "Passed budget and risk; awaiting paper ticket and alerts",
            })
            risk_passed.append(item)
        _record(
            verification, 7, SKILLS[7], "PASS" if sized else "WARN",
            f"INR sizing at <= 1% account risk: {len(sized)} candidate(s) sized.",
            "\n".join(budget_outputs) or "No candidates reached position sizing.",
        )
        report(7, f"{len(sized)} passed INR sizing and the 1% risk cap", [item["ticker"] for item in sized])
        _record(
            verification, 8, SKILLS[8], "PASS" if risk_passed else "WARN",
            f"trader-risk PASS for {len(risk_passed)} candidate(s); failing verdicts were rejected.",
            "\n".join(risk_outputs) or "No sized orders were available for risk validation.",
        )
        report(8, f"{len(risk_passed)} passed trader-risk", [item["ticker"] for item in risk_passed])

        final = sorted(
            risk_passed,
            key=lambda item: (
                bool(item.get("negative_news")),
                -item.get("conviction_score", 0),
                -float(item["timemachine"]["mc"].get("p_green", 0)),
            ),
        )[:3]
        if final:
            orders_file = root / "trader" / "orders.json"
            existing = {}
            if orders_file.exists():
                try:
                    existing = json.loads(orders_file.read_text(encoding="utf-8"))
                except ValueError:
                    existing = {}
            existing_orders = existing.get("orders", []) if isinstance(existing, dict) else []
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update({
                "created_by": "trader-autopilot",
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "status": "proposed",
                "paper": True,
                "note": "Paper proposals only; no order was placed.",
                "orders": _merge_order_proposals(
                    existing_orders, [item["paper_order"] for item in final],
                ),
            })
            orders_file.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
            broker_outputs = []
            alert_outputs = []
            for item in final:
                ticker = item["ticker"]
                plan_path = temp_path / f"{ticker.replace('.', '_')}-plan.json"
                plan_path.write_text(json.dumps(item["plan"]), encoding="utf-8")
                broker_code, broker_out, broker_err = _run(
                    root, SKILLS[9], "ticket.py",
                    ["show", "--orders", item["_orders_path"], "--risk", item["_risk_path"],
                     "--config", str(config_path), "--json"],
                )
                broker_outputs.append(f"{ticker} [{broker_code}]: {broker_out or broker_err}")
                alert_code, alert_out, alert_err = _run(
                    root, SKILLS[10], "alerts.py",
                    ["add", "--from-plan", "--plan", str(plan_path),
                     "--rules", str(root / "trader" / "alerts.json")],
                )
                alert_outputs.append(f"{ticker} [{alert_code}]: {alert_out or alert_err}")
                if broker_code == 0:
                    item["broker_output"] = broker_out
                if alert_code == 0:
                    item["alert_output"] = alert_out
            _record(
                verification, 9, SKILLS[9], "PASS" if all(item.get("broker_output") for item in final) else "WARN",
                f"Rendered {len(final)} paper ticket(s); no broker submission was made.",
                "\n".join(broker_outputs),
            )
            for item in final:
                item["_funnel"]["Passed/Rejected Reason"] = "Passed all 11 skills; paper ticket and alerts ready"
            report(9, f"paper tickets rendered for {len(final)} final trade(s); no order placed")
            _record(
                verification, 10, SKILLS[10], "PASS" if all(item.get("alert_output") for item in final) else "WARN",
                f"Entry/SL/Target/flat-by alerts configured for {sum(bool(item.get('alert_output')) for item in final)} "
                f"of {len(final)} final trade(s).",
                "\n".join(alert_outputs),
            )
            report(10, "final-trade alerts set", [item["ticker"] for item in final])
            qualified = final
        else:
            _record_skipped(verification, 9, "No risk-approved trades; no tickets generated.")
            _record_skipped(verification, 10, "No risk-approved trades; no alerts generated.")
            report(9, "no risk-approved tickets to render")
            report(10, "no qualified trades; no alerts written")

    for item in qualified:
        item.pop("_orders_path", None)
        item.pop("_risk_path", None)
    return {
        "market": market,
        "qualified": qualified,
        "candidates": _funnel_snapshot(candidates),
        "verification": verification,
        "mode": mode,
    }


def _record_skipped(rows, index, reason):
    _record(rows, index, SKILLS[index], "WARN", reason, reason)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the paper-only 11-skill NSE Auto-Pilot.")
    parser.add_argument(
        "--mode", choices=("fo_intraday", "cash_swing"), default="fo_intraday",
        help="F&O 5-minute opening-range mode (default) or daily cash-swing mode",
    )
    args = parser.parse_args(argv)

    def progress(step, skill, message):
        print(f"{step}/11 · {skill} · {message}", file=sys.stderr, flush=True)

    result = run_autopilot(Path(__file__).resolve().parent, progress, mode=args.mode)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
