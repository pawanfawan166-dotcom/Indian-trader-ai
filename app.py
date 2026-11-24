import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
SKILLS = ROOT / "skills"
WATCHLIST = ROOT / "trader" / "watchlist.json"

st.set_page_config(page_title="Indian NSE Trading Desk", page_icon="₹", layout="wide")

st.markdown("""
<style>
.block-container {padding-top: 1.5rem; padding-bottom: 2rem; max-width: 1180px;}
[data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.18); border-radius: 8px; padding: 10px;}
[data-testid="stDataFrame"] {border: 1px solid rgba(128,128,128,.18); border-radius: 8px;}
.stButton > button {width: 100%; border-radius: 6px; min-height: 42px;}
@media (max-width: 640px) {
  .block-container {padding-left: .75rem; padding-right: .75rem;}
  h1 {font-size: 1.7rem !important;}
}
</style>
""", unsafe_allow_html=True)

st.title("Indian NSE Trading Desk")
st.caption("NSE · INR (₹) · IST 9:15 AM–3:30 PM")


def nse_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if not ticker:
        return ""
    ticker = ticker.replace(".NS", "")
    return f"{ticker}.NS"


def run_skill(skill_name: str, script_name: str, args: list[str], timeout: int = 120):
    script = SKILLS / skill_name / script_name
    if not script.exists():
        return 127, "", f"Skill script not found: {script}"
    command = [sys.executable, str(script), *args]
    try:
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Timed out after {timeout} seconds"
    except Exception as exc:
        return 1, "", str(exc)


def parse_json_output(text: str):
    try:
        start = text.find("{")
        if start >= 0:
            return json.loads(text[start:])
        start = text.find("[")
        if start >= 0:
            return json.loads(text[start:])
    except json.JSONDecodeError:
        pass
    return None


def load_watchlist():
    if not WATCHLIST.exists():
        return {}, pd.DataFrame()
    try:
        doc = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": str(exc)}, pd.DataFrame()

    hits = doc.get("hits", [])
    rows = []
    for hit in hits:
        setups = hit.get("setups") or hit.get("setup") or []
        if isinstance(setups, str):
            setups = [setups]
        rows.append({
            "Symbol": hit.get("symbol", ""),
            "Price (₹)": hit.get("price"),
            "RSI": hit.get("rsi"),
            "Setup": ", ".join(setups),
            "Volume": hit.get("volume"),
            "Close Date": hit.get("close_date", ""),
        })
    frame = pd.DataFrame(rows)
    return doc, frame


def format_watchlist(frame: pd.DataFrame):
    if frame.empty:
        return frame
    return frame


scanner_tab, analyzer_tab = st.tabs(["NSE Scanner", "Quick Stock Analyzer"])

with scanner_tab:
    col1, col2 = st.columns(2)
    with col1:
        daily_clicked = st.button("Pre-Market · Daily", use_container_width=True)
    with col2:
        orb_clicked = st.button("Live · Opening Range", use_container_width=True)

    if daily_clicked or orb_clicked:
        mode = "daily" if daily_clicked else "orb"
        label = "pre-market daily scan" if mode == "daily" else "opening-range breakout scan"
        with st.spinner(f"Running {label}..."):
            code, stdout, stderr = run_skill(
                "trader-scanner",
                "scan.py",
                [mode, "--json", "--out", "trader/watchlist.json"],
                timeout=180,
            )
        if code not in (0, 1):
            st.error(stderr or stdout or f"Scanner exited with code {code}")
        else:
            result = parse_json_output(stdout)
            if result:
                st.success(f"Scanner completed: {result.get('count', result.get('scanned', 0))} scanned, {len(result.get('hits', []))} hits")
            elif stdout:
                st.info(stdout)

    doc, frame = load_watchlist()
    if isinstance(doc, dict) and doc.get("error"):
        st.error(f"Could not read watchlist: {doc['error']}")
    elif frame.empty:
        st.info("No watchlist data yet. Run a scanner mode above.")
    else:
        meta1, meta2, meta3 = st.columns(3)
        meta1.metric("Mode", str(doc.get("mode", "—")).upper())
        meta2.metric("As of", str(doc.get("as_of", "—")))
        meta3.metric("Watchlist", len(frame))

        search = st.text_input("Search symbols or setup", key="watchlist_search")
        filtered = frame
        if search:
            needle = search.casefold()
            mask = filtered.astype(str).apply(lambda column: column.str.casefold().str.contains(needle, regex=False)).any(axis=1)
            filtered = filtered[mask]

        st.dataframe(
            format_watchlist(filtered),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Price (₹)": st.column_config.NumberColumn(format="₹%.2f"),
                "RSI": st.column_config.NumberColumn(format="%.1f"),
                "Volume": st.column_config.NumberColumn(format="%,d"),
            },
        )

with analyzer_tab:
    col_input, col_action = st.columns([3, 1])
    with col_input:
        ticker_input = st.text_input("NSE ticker", placeholder="RELIANCE or RELIANCE.NS")
    with col_action:
        analyze = st.button("Analyze", type="primary", use_container_width=True)

    if analyze:
        ticker = nse_ticker(ticker_input)
        if not ticker:
            st.warning("Enter an NSE ticker.")
        else:
            st.subheader(ticker)

            with st.spinner("Running trader-entry..."):
                entry_code, entry_stdout, entry_stderr = run_skill(
                    "trader-entry", "entry.py",
                    ["check", ticker, "--json", "--write", "trader/entry.json"],
                )

            entry_doc = parse_json_output(entry_stdout)
            if entry_doc is None:
                entry_doc = json.loads((ROOT / "trader" / "entry.json").read_text(encoding="utf-8")) if (ROOT / "trader" / "entry.json").exists() else None

            entry_col, exit_col = st.columns(2)
            with entry_col:
                st.markdown("**Entry**")
                if entry_code not in (0, 1) or entry_doc is None:
                    st.error(entry_stderr or entry_stdout or f"Entry check failed with code {entry_code}")
                else:
                    status = str(entry_doc.get("status", "UNKNOWN")).upper()
                    st.metric("Status", status)
                    if entry_doc.get("reason"):
                        st.caption(entry_doc["reason"])

            entry_price = None
            or_high = or_low = None
            if entry_doc:
                entry_price = entry_doc.get("price")
                orr = entry_doc.get("opening_range") or {}
                or_high = orr.get("high")
                or_low = orr.get("low")
                if entry_price is None:
                    entry_price = entry_doc.get("level") or or_high

            with st.spinner("Running trader-exit..."):
                if entry_doc and entry_doc.get("status") == "triggered":
                    exit_args = ["plan", "--from", "trader/entry.json", "--json", "--write", "trader/plan.json"]
                elif entry_price:
                    exit_args = ["plan", "--symbol", ticker, "--entry", str(entry_price), "--json", "--write", "trader/plan.json"]
                    if or_high is not None and or_low is not None:
                        exit_args += ["--or-high", str(or_high), "--or-low", str(or_low), "--setup", "orb"]
                else:
                    exit_args = None

                if exit_args:
                    exit_code, exit_stdout, exit_stderr = run_skill("trader-exit", "exit.py", exit_args)
                else:
                    exit_code, exit_stdout, exit_stderr = 1, "", "Entry facts are unavailable for an exit plan"

            exit_doc = parse_json_output(exit_stdout)
            with exit_col:
                st.markdown("**Exit Plan**")
                if exit_code != 0 or exit_doc is None:
                    st.error(exit_stderr or exit_stdout or "Exit plan could not be created")
                else:
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Entry", f"₹{float(exit_doc.get('entry', 0)):,.2f}")
                    m2.metric("Stop", f"₹{float(exit_doc.get('stop', 0)):,.2f}")
                    m3.metric("Target", f"₹{float(exit_doc.get('target', 0)):,.2f}")

            risk_col, news_col = st.columns(2)
            with risk_col:
                st.markdown("**Risk Check**")
                with st.spinner("Running trader-risk..."):
                    if (ROOT / "trader" / "orders.json").exists():
                        risk_code, risk_stdout, risk_stderr = run_skill(
                            "trader-risk", "check.py", ["--json", "--no-write"],
                        )
                        risk_doc = parse_json_output(risk_stdout)
                        if risk_code not in (0, 1) or risk_doc is None:
                            st.error(risk_stderr or risk_stdout or "Risk check failed")
                        else:
                            verdicts = risk_doc.get("verdicts") or risk_doc.get("results") or []
                            if verdicts:
                                for verdict in verdicts:
                                    st.success(str(verdict)) if verdict.get("verdict") == "PASS" else st.error(str(verdict))
                            else:
                                st.json(risk_doc)
                    else:
                        st.info("No trader/orders.json exists yet. Create a budget-sized order to run risk validation.")

            with news_col:
                st.markdown("**Recent News**")
                with st.spinner("Running trader-news..."):
                    news_code, news_stdout, news_stderr = run_skill(
                        "trader-news", "news.py",
                        ["--tickers", ticker, "--json", "--no-sec", "--no-moves", "--dry-run", "--hours", "48"],
                        timeout=150,
                    )
                    news_doc = parse_json_output(news_stdout)
                    if news_code not in (0, 1) or news_doc is None:
                        st.warning(news_stderr or news_stdout or f"No news data available (exit {news_code})")
                    else:
                        items = news_doc if isinstance(news_doc, list) else news_doc.get("items", [])
                        if items:
                            for item in items[:8]:
                                title = item.get("title", "Untitled")
                                link = item.get("link")
                                source = item.get("source", "Source")
                                if link:
                                    st.markdown(f"- [{title}]({link}) — _{source}_")
                                else:
                                    st.markdown(f"- {title} — _{source}_")
                        else:
                            st.info("No recent items found.")
