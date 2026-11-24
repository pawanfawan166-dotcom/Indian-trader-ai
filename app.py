import datetime
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots

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
.stTabs [data-baseweb="tab-list"] {gap: .25rem; flex-wrap: wrap;}
.stTabs [data-baseweb="tab"] {white-space: normal;}
@media (max-width: 640px) {
  .block-container {padding-left: .75rem; padding-right: .75rem;}
  h1 {font-size: 1.7rem !important;}
    .stTabs [data-baseweb="tab"] {padding: .4rem .55rem; font-size: .85rem;}
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


def show_skill_output(code: int, stdout: str, stderr: str):
    payload = parse_json_output(stdout)
    if code not in (0, 1):
        st.error(stderr or stdout or f"Skill exited with code {code}")
        if stdout and stderr:
            st.code(stdout)
        return None
    if payload is not None:
        st.json(payload)
    elif stdout:
        st.code(stdout)
    if stderr:
        st.warning(stderr) if code else st.caption(stderr)
    if code == 1 and not stdout and not stderr:
        st.info("No matching results were returned.")
    return payload


def show_deals(rows):
    if not rows:
        st.info("No matching NSE deals in the available snapshot.")
        return
    table = []
    for row in rows:
        deal = row.get("deal") or {}
        table.append({
            "Date": row.get("filed"),
            "Deal": row.get("type_name") or row.get("type"),
            "Symbol": deal.get("symbol") or row.get("district"),
            "Company": deal.get("company"),
            "Counterparty": deal.get("client") or row.get("name"),
            "Side": deal.get("side"),
            "Shares": deal.get("quantity"),
            "Price (₹)": deal.get("price"),
            "Value (₹)": deal.get("amount_inr"),
            "NSE page": row.get("url"),
        })
    st.dataframe(
        pd.DataFrame(table),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Price (₹)": st.column_config.NumberColumn(format="₹%.2f"),
            "Value (₹)": st.column_config.NumberColumn(format="₹%.2f"),
            "NSE page": st.column_config.LinkColumn(display_text="Open"),
        },
    )


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


@st.cache_data(ttl=900, show_spinner=False)
def load_price_history(ticker: str, start_date: datetime.date, end_date: datetime.date, interval: str):
    frame = yf.Ticker(ticker).history(
        start=start_date.isoformat(),
        end=(end_date + datetime.timedelta(days=1)).isoformat(),
        interval=interval,
        auto_adjust=False,
        prepost=False,
    )
    if frame.empty:
        return frame
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    required = ("Open", "High", "Low", "Close")
    if any(column not in frame.columns for column in required):
        return pd.DataFrame()
    if "Volume" not in frame.columns:
        frame["Volume"] = 0
    return frame.loc[:, [*required, "Volume"]].dropna(subset=list(required)).sort_index()


def build_candlestick_chart(
    frame: pd.DataFrame,
    title: str,
    levels: list[tuple[str, float, str]] | None = None,
    markers: list[dict] | None = None,
    show_emas: bool = True,
):
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[0.76, 0.24],
    )
    fig.add_trace(
        go.Candlestick(
            x=frame.index,
            open=frame["Open"],
            high=frame["High"],
            low=frame["Low"],
            close=frame["Close"],
            name="NSE",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        ),
        row=1,
        col=1,
    )
    if show_emas:
        for span, color in ((20, "#2962ff"), (50, "#ff9800")):
            fig.add_trace(
                go.Scatter(
                    x=frame.index,
                    y=frame["Close"].ewm(span=span, adjust=False).mean(),
                    mode="lines",
                    name=f"{span}-EMA",
                    line={"color": color, "width": 1.4},
                ),
                row=1,
                col=1,
            )

    volume_colors = [
        "#26a69a" if close >= open_price else "#ef5350"
        for open_price, close in zip(frame["Open"], frame["Close"])
    ]
    fig.add_trace(
        go.Bar(
            x=frame.index,
            y=frame["Volume"],
            name="Volume",
            marker_color=volume_colors,
            opacity=0.75,
        ),
        row=2,
        col=1,
    )
    for label, price, color in levels or []:
        fig.add_hline(
            y=price,
            line_dash="dash",
            line_color=color,
            annotation_text=f"{label} ₹{price:,.2f}",
            annotation_position="top left",
            row=1,
            col=1,
        )
    for marker in markers or []:
        fig.add_trace(
            go.Scatter(
                x=[marker["x"]],
                y=[marker["y"]],
                mode="markers+text",
                text=[marker["label"]],
                textposition="top center",
                name=marker["label"],
                marker={
                    "color": marker["color"],
                    "size": 12,
                    "symbol": marker.get("symbol", "circle"),
                },
            ),
            row=1,
            col=1,
        )

    fig.update_layout(
        title=title,
        template="plotly_white",
        height=570,
        margin={"l": 8, "r": 8, "t": 48, "b": 8},
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "x": 0},
        xaxis_rangeslider_visible=False,
    )
    fig.update_yaxes(title_text="Price (INR)", tickprefix="₹", tickformat=",.2f", row=1, col=1)
    fig.update_yaxes(title_text="Volume", tickformat="~s", row=2, col=1)
    fig.update_xaxes(showgrid=True, gridcolor="rgba(128,128,128,.15)")
    return fig


def show_stock_chart(
    ticker: str,
    start_date: datetime.date,
    end_date: datetime.date,
    interval: str,
    title: str,
    levels: list[tuple[str, float, str]] | None = None,
    markers: list[dict] | None = None,
    show_emas: bool = True,
):
    frame = load_price_history(ticker, start_date, end_date, interval)
    if frame.empty:
        st.info(
            f"No price bars were returned for {ticker} in this date range. "
            "Check the NSE ticker, interval, and Yahoo Finance data limits."
        )
        return frame
    fig = build_candlestick_chart(frame, title, levels=levels, markers=markers, show_emas=show_emas)
    st.plotly_chart(fig, width="stretch", config={"responsive": True, "displaylogo": False})
    st.caption("Yahoo Finance data is unofficial and may be delayed.")
    return frame


scanner_tab, analyzer_tab, analyst_tab, budget_broker_tab, alerts_deals_tab, timemachine_tab = st.tabs([
    "NSE Scanner", "Quick Stock Analyzer", "Analyst", "Budget & Broker", "Alerts & Deals", "Time Machine",
])

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
        chart_symbols = sorted({
            nse_ticker(str(symbol))
            for symbol in frame["Symbol"]
            if str(symbol).strip()
        })
        if chart_symbols:
            chart_ticker = st.selectbox(
                "Chart a scanned NSE stock",
                chart_symbols,
                format_func=lambda symbol: symbol.removesuffix(".NS"),
                key="scanner_chart_ticker",
            )
            st_stock_chart_start = datetime.date.today() - datetime.timedelta(days=365)
            show_stock_chart(
                chart_ticker,
                st_stock_chart_start,
                datetime.date.today(),
                "1d",
                f"{chart_ticker} · Daily",
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

            chart_levels = []
            chart_entry = exit_doc.get("entry") if exit_doc else entry_price
            if chart_entry is not None:
                chart_levels.append(("Entry", float(chart_entry), "#2962ff"))
            if exit_doc:
                for key, label, color in (
                    ("stop", "Stop-Loss", "#ef5350"),
                    ("target", "Target", "#26a69a"),
                ):
                    if exit_doc.get(key) is not None:
                        chart_levels.append((label, float(exit_doc[key]), color))
            st.markdown("#### Price Chart")
            show_stock_chart(
                ticker,
                datetime.date.today() - datetime.timedelta(days=365),
                datetime.date.today(),
                "1d",
                f"{ticker} · Daily",
                levels=chart_levels,
            )

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

with analyst_tab:
    st.subheader("NSE Fundamentals & Filings")
    st.caption("Official NSE financial filings in INR. This report is analysis, not a buy/sell recommendation.")
    with st.form("analyst_form"):
        analyst_col, quarters_col = st.columns([3, 1])
        with analyst_col:
            analyst_ticker_input = st.text_input("NSE ticker", value="RELIANCE", placeholder="RELIANCE or RELIANCE.NS")
        with quarters_col:
            analyst_quarters = st.number_input("Quarters", min_value=1, max_value=12, value=6, step=1)
        include_quote = st.checkbox("Include read-only Angel One quote when configured")
        analyst_submit = st.form_submit_button("Run NSE analyst", type="primary")

    if analyst_submit:
        ticker = nse_ticker(analyst_ticker_input)
        if not ticker:
            st.warning("Enter an NSE ticker.")
        else:
            args = [ticker, "--quarters", str(int(analyst_quarters)), "--json"]
            if not include_quote:
                args.append("--no-quote")
            with st.spinner(f"Reading NSE filings for {ticker}..."):
                code, stdout, stderr = run_skill("trader-analyst", "facts.py", args, timeout=240)
            reports = parse_json_output(stdout)
            if code not in (0, 1):
                show_skill_output(code, stdout, stderr)
            elif isinstance(reports, list):
                if stderr:
                    st.warning(stderr)
                for report in reports:
                    result = report.get("result") or {}
                    title = "%s · %s" % (report.get("ticker", ticker), result.get("entity") or "NSE filing report")
                    with st.expander(title, expanded=True):
                        st.caption("%s · %s · %s" % (
                            result.get("source", "NSE"), result.get("currency", "INR"), result.get("basis", "")))
                        ttm = result.get("ttm") or {}
                        if ttm:
                            metric_cols = st.columns(3)
                            for col, key, label in zip(
                                    metric_cols, ("revenue", "net_income", "eps_diluted"),
                                    ("TTM Revenue", "TTM Net Income", "TTM EPS")):
                                value = ttm.get(key)
                                shown = "—" if value is None else (
                                    "₹%.2f" % value if key == "eps_diluted" else "₹{:,.0f}".format(value))
                                col.metric(label, shown)
                        rows = result.get("rows") or []
                        if rows:
                            st.dataframe(
                                pd.DataFrame(rows),
                                use_container_width=True,
                                hide_index=True,
                                column_config={
                                    "revenue": st.column_config.NumberColumn(format="₹%.0f"),
                                    "gross_profit": st.column_config.NumberColumn(format="₹%.0f"),
                                    "operating_income": st.column_config.NumberColumn(format="₹%.0f"),
                                    "net_income": st.column_config.NumberColumn(format="₹%.0f"),
                                    "eps_diluted": st.column_config.NumberColumn(format="₹%.2f"),
                                    "revenue_yoy": st.column_config.NumberColumn(format="%.1f%%"),
                                    "net_income_yoy": st.column_config.NumberColumn(format="%.1f%%"),
                                    "eps_yoy": st.column_config.NumberColumn(format="%.1f%%"),
                                    "url": st.column_config.LinkColumn(display_text="Filing"),
                                },
                            )
                        else:
                            st.info("No quarterly rows were reported for this ticker.")
                        if result.get("filing"):
                            st.markdown("**Latest filing**")
                            st.json(result["filing"])
                        if result.get("quote_note"):
                            st.caption(result["quote_note"])
                        if result.get("warnings"):
                            for warning in result["warnings"]:
                                st.warning(warning)
            else:
                show_skill_output(code, stdout, stderr)

with budget_broker_tab:
    st.subheader("Position Sizing · INR")
    plan_path = ROOT / "trader" / "plan.json"
    config_path = ROOT / "trader" / "config.json"
    try:
        plan_doc = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else None
    except (OSError, json.JSONDecodeError):
        plan_doc = None
    try:
        budget_config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        budget_config = {}
    if plan_doc:
        st.caption("Plan: %s · entry ₹%s · stop ₹%s · target ₹%s" % (
            plan_doc.get("symbol", "—"), plan_doc.get("entry", "—"),
            plan_doc.get("stop", "—"), plan_doc.get("target", "—")))
    else:
        st.info("Create a trade plan in Quick Stock Analyzer before sizing a position.")
    configured_budget = budget_config.get("budget")
    try:
        configured_budget = max(0.0, float(configured_budget or 0))
    except (TypeError, ValueError):
        configured_budget = 0.0
    configured_loss = budget_config.get(
        "max_loss_per_trade_inr", budget_config.get("max_loss_per_trade_usd", 50))
    st.caption("Configured max loss per trade: ₹%s. Change rules in trader/config.json." % configured_loss)

    with st.form("budget_form"):
        budget_amount = st.number_input(
            "Trade budget (₹)", min_value=0.0, value=configured_budget, step=1000.0, format="%.2f")
        use_limit = st.checkbox("Use the plan entry as a limit price")
        size_submit = st.form_submit_button("Size and save paper ticket", type="primary", disabled=not bool(plan_doc))

    def run_risk_and_show_tickets():
        st.markdown("#### Risk Check")
        with st.spinner("Checking the proposed order against your rules..."):
            risk_code, risk_stdout, risk_stderr = run_skill("trader-risk", "check.py", ["--json"])
        show_skill_output(risk_code, risk_stdout, risk_stderr)
        st.markdown("#### Broker Ticket")
        with st.spinner("Rendering paper ticket..."):
            broker_code, broker_stdout, broker_stderr = run_skill(
                "trader-broker", "ticket.py", ["show", "--json"])
        show_skill_output(broker_code, broker_stdout, broker_stderr)

    if size_submit:
        if budget_amount <= 0:
            st.warning("Enter a budget greater than ₹0.")
        else:
            size_args = ["--budget", str(budget_amount), "--write", "--json"]
            if use_limit:
                size_args.append("--limit")
            with st.spinner("Sizing whole shares against your INR limits..."):
                size_code, size_stdout, size_stderr = run_skill("trader-budget", "size.py", size_args)
            if size_code != 0:
                show_skill_output(size_code, size_stdout, size_stderr)
            else:
                st.success("Proposed paper order saved to trader/orders.json.")
                show_skill_output(size_code, size_stdout, size_stderr)
                run_risk_and_show_tickets()

    st.divider()
    st.subheader("Broker Paper Tickets")
    st.caption("NSE tickets are proposals only. This interface does not submit orders or record approval.")
    ticket_col, log_col = st.columns(2)
    with ticket_col:
        refresh_tickets = st.button("Run Risk Check & Refresh Tickets", key="broker_refresh")
    with log_col:
        show_broker_log = st.button("Show Broker Log", key="broker_log")
    if refresh_tickets:
        if not (ROOT / "trader" / "orders.json").exists():
            st.info("No trader/orders.json exists yet. Size a plan above first.")
        else:
            run_risk_and_show_tickets()
    if show_broker_log:
        with st.spinner("Loading today's paper-ticket log..."):
            log_code, log_stdout, log_stderr = run_skill("trader-broker", "ticket.py", ["log"])
        show_skill_output(log_code, log_stdout, log_stderr)

with alerts_deals_tab:
    st.subheader("Price Alerts")
    st.caption("Quotes are delayed and unofficial. Alerts are informational; they never place or modify orders.")
    clock_col, check_col, rules_col = st.columns(3)
    with clock_col:
        clock_clicked = st.button("NSE Clock", key="alerts_clock")
    with check_col:
        check_alerts = st.button("Check Alerts", key="alerts_check")
    with rules_col:
        list_alerts = st.button("List Rules", key="alerts_list")
    if clock_clicked:
        with st.spinner("Checking NSE session status..."):
            code, stdout, stderr = run_skill("trader-alerts", "alerts.py", ["clock", "--json"])
        show_skill_output(code, stdout, stderr)
    if check_alerts:
        with st.spinner("Checking today's alert rules and delayed quotes..."):
            code, stdout, stderr = run_skill("trader-alerts", "alerts.py", ["check", "--json"], timeout=150)
        show_skill_output(code, stdout, stderr)
    if list_alerts:
        code, stdout, stderr = run_skill("trader-alerts", "alerts.py", ["list"])
        show_skill_output(code, stdout, stderr)

    with st.form("alert_add_form"):
        alert_type = st.selectbox("New alert", [
            "Price above", "Price below", "Stop", "Target", "Percent move",
            "Opening bell", "Time reminder", "From current plan",
        ])
        alert_symbol = ""
        alert_value = None
        alert_time = "15:25"
        alert_label = ""
        alert_direction = "Either"
        if alert_type in ("Price above", "Price below", "Stop", "Target", "Percent move"):
            alert_symbol = st.text_input("NSE ticker", placeholder="RELIANCE or RELIANCE.NS")
            if alert_type == "Percent move":
                alert_value = st.number_input("Move (%)", min_value=0.1, value=3.0, step=0.5)
                alert_direction = st.selectbox("Direction", ["Either", "Up", "Down"])
            else:
                alert_value = st.number_input("Price (₹)", min_value=0.01, value=100.0, step=1.0)
        elif alert_type == "Time reminder":
            alert_time = st.text_input("IST time (HH:MM)", value="15:25")
            alert_label = st.text_input("Reminder label", value="Flat by close")
        alert_submit = st.form_submit_button("Add alert")

    if alert_submit:
        alert_args = ["add"]
        if alert_type == "From current plan":
            alert_args.append("--from-plan")
        elif alert_type == "Opening bell":
            alert_args.append("--opening-bell")
        elif alert_type == "Time reminder":
            alert_args += ["--at", alert_time, "--label", alert_label or "Time reminder"]
        else:
            ticker = nse_ticker(alert_symbol)
            if not ticker:
                st.warning("Enter an NSE ticker.")
                alert_args = None
            else:
                flag = {
                    "Price above": "--above", "Price below": "--below",
                    "Stop": "--stop", "Target": "--target", "Percent move": "--pct",
                }[alert_type]
                alert_args += ["--symbol", ticker, flag, str(alert_value)]
                if alert_type == "Percent move" and alert_direction != "Either":
                    alert_args += ["--direction", alert_direction.lower()]
        if alert_args:
            with st.spinner("Saving alert rule..."):
                code, stdout, stderr = run_skill("trader-alerts", "alerts.py", alert_args)
            show_skill_output(code, stdout, stderr)

    with st.form("quote_form"):
        quote_symbol_input = st.text_input("Save a delayed quote to trader/prices.json", placeholder="RELIANCE")
        quote_submit = st.form_submit_button("Fetch quote")
    if quote_submit:
        ticker = nse_ticker(quote_symbol_input)
        if not ticker:
            st.warning("Enter an NSE ticker.")
        else:
            with st.spinner(f"Fetching a delayed quote for {ticker}..."):
                code, stdout, stderr = run_skill(
                    "trader-alerts", "alerts.py", ["quote", ticker, "--json", "--write"], timeout=120)
            show_skill_output(code, stdout, stderr)

    st.divider()
    st.subheader("NSE Bulk & Block Deals")
    st.caption("Latest NSE snapshot only; it is not a complete historical archive or an insider-intent signal.")
    with st.form("deals_form"):
        deal_col, limit_col = st.columns([3, 1])
        with deal_col:
            deal_member = st.text_input("Counterparty or NSE ticker", placeholder="RELIANCE")
        with limit_col:
            deal_limit = st.number_input("Maximum rows", min_value=1, max_value=200, value=50, step=5)
        deal_kind = st.selectbox("Deal type", ["Both", "Bulk", "Block"])
        deal_since = st.text_input("On or after date (optional)", placeholder="YYYY-MM-DD")
        deal_offline = st.text_input("Offline snapshot path (optional)", placeholder="trader/state/nse-deals-cache.json")
        latest_deals = st.form_submit_button("Load latest deals", type="primary")
        new_deals = st.form_submit_button("Check and record unseen deals")

    if latest_deals or new_deals:
        deal_type = {"Both": "BK", "Bulk": "B", "Block": "K"}[deal_kind]
        if latest_deals:
            deal_args = ["index", "--json", "--limit", str(int(deal_limit)), "--type", deal_type]
        else:
            deal_args = ["new", "--json", "--read", "--type", deal_type]
        if deal_member.strip():
            deal_args += ["--member", deal_member.strip()]
            member_ticker = deal_member.strip().upper().removesuffix(".NS")
            if member_ticker.replace("-", "").isalnum() and " " not in member_ticker:
                deal_args += ["--tickers", nse_ticker(member_ticker)] if new_deals else []
        if deal_since.strip():
            deal_args += ["--since", deal_since.strip()]
        if deal_offline.strip():
            deal_args += ["--offline", deal_offline.strip()]
        with st.spinner("Loading the NSE deal snapshot..."):
            code, stdout, stderr = run_skill("trader-pelosi", "ptr.py", deal_args, timeout=150)
        payload = parse_json_output(stdout)
        if code not in (0, 1):
            show_skill_output(code, stdout, stderr)
        elif latest_deals:
            if stderr:
                st.warning(stderr)
            show_deals(payload if isinstance(payload, list) else [])
        elif isinstance(payload, dict):
            if stderr:
                st.warning(stderr)
            show_deals(payload.get("new") or [])
            for report in payload.get("reports") or []:
                with st.expander("Deal details: %s" % (report.get("filing", {}).get("doc_id", "NSE"))):
                    st.json(report.get("report") or report)
            st.caption("This check records seen deal IDs in trader/state/pelosi.json.")
        else:
            show_skill_output(code, stdout, stderr)

with timemachine_tab:
    st.subheader("Historical NSE Replay")
    st.caption(
        "Step through historical NSE bars with a simple simulated long trade. "
        "This is an educational replay, not a prediction or investment recommendation."
    )
    today = datetime.date.today()
    with st.form("timemachine_chart_form"):
        replay_symbol_input = st.text_input("NSE ticker", value="RELIANCE", placeholder="RELIANCE or RELIANCE.NS")
        date_col, end_col, interval_col = st.columns([2, 2, 1])
        with date_col:
            replay_start = st.date_input(
                "From",
                value=today - datetime.timedelta(days=7),
                max_value=today,
            )
        with end_col:
            replay_end = st.date_input("Through", value=today, max_value=today)
        with interval_col:
            replay_interval = st.selectbox("Bar interval", ["5m", "15m", "1h", "1d"], index=0)
        stop_pct = st.number_input("Simulated stop-loss (%)", min_value=0.1, value=1.0, step=0.1)
        target_pct = st.number_input("Simulated target (%)", min_value=0.1, value=2.0, step=0.1)
        replay_quantity = st.number_input("Shares", min_value=1, value=1, step=1)
        replay_chart_submit = st.form_submit_button("Load historical replay", type="primary")

    if replay_chart_submit:
        replay_ticker = nse_ticker(replay_symbol_input)
        if not replay_ticker:
            st.warning("Enter an NSE ticker.")
            st.session_state.pop("timemachine_chart_request", None)
        elif replay_start > replay_end:
            st.warning("The start date must be on or before the end date.")
            st.session_state.pop("timemachine_chart_request", None)
        else:
            st.session_state["timemachine_chart_request"] = {
                "ticker": replay_ticker,
                "start": replay_start,
                "end": replay_end,
                "interval": replay_interval,
                "stop_pct": float(stop_pct),
                "target_pct": float(target_pct),
                "quantity": int(replay_quantity),
            }

    chart_request = st.session_state.get("timemachine_chart_request")
    if chart_request:
        replay_frame = load_price_history(
            chart_request["ticker"],
            chart_request["start"],
            chart_request["end"],
            chart_request["interval"],
        )
        if replay_frame.empty:
            st.info(
                "No bars were returned for this ticker/date/interval. "
                "Yahoo Finance keeps intraday history for a limited time; try a shorter or more recent range."
            )
        else:
            entry_price = float(replay_frame["Close"].iloc[0])
            stop_price = entry_price * (1 - chart_request["stop_pct"] / 100)
            target_price = entry_price * (1 + chart_request["target_pct"] / 100)
            exit_index = len(replay_frame) - 1
            exit_price = float(replay_frame["Close"].iloc[-1])
            exit_reason = "Range end"
            for index in range(1, len(replay_frame)):
                bar = replay_frame.iloc[index]
                if float(bar["Low"]) <= stop_price:
                    exit_index = index
                    exit_price = stop_price
                    exit_reason = "Stop-Loss"
                    break
                if float(bar["High"]) >= target_price:
                    exit_index = index
                    exit_price = target_price
                    exit_reason = "Target"
                    break

            if len(replay_frame) > 1:
                step_count = st.slider(
                    "Replay bar",
                    min_value=1,
                    max_value=len(replay_frame),
                    value=min(25, len(replay_frame)),
                    key=(
                        f"timemachine_step_{chart_request['ticker']}_"
                        f"{chart_request['start']}_{chart_request['end']}_{chart_request['interval']}"
                    ),
                    help="Move one bar at a time to reveal the historical price action.",
                )
            else:
                step_count = 1
                st.caption("Only one bar is available in this range.")

            visible_frame = replay_frame.iloc[:step_count]
            exit_reached = step_count - 1 >= exit_index
            last_close = float(visible_frame["Close"].iloc[-1])
            pnl = (
                (exit_price - entry_price) * chart_request["quantity"]
                if exit_reached
                else (last_close - entry_price) * chart_request["quantity"]
            )
            metric_col, status_col, entry_col = st.columns(3)
            metric_col.metric(
                "Simulated P&L" if exit_reached else "Open P&L",
                f"₹{pnl:+,.2f}",
            )
            status_col.metric("Replay status", exit_reason if exit_reached else "Trade open")
            entry_col.metric("Entry", f"₹{entry_price:,.2f}")

            replay_markers = [
                {
                    "x": replay_frame.index[0],
                    "y": entry_price,
                    "label": "Entry",
                    "color": "#2962ff",
                    "symbol": "triangle-up",
                }
            ]
            if exit_reached:
                replay_markers.append({
                    "x": replay_frame.index[exit_index],
                    "y": exit_price,
                    "label": exit_reason,
                    "color": "#ef5350" if exit_reason == "Stop-Loss" else "#26a69a",
                    "symbol": "x",
                })
            chart_levels = [
                ("Entry", entry_price, "#2962ff"),
                ("Stop-Loss", stop_price, "#ef5350"),
                ("Target", target_price, "#26a69a"),
            ]
            replay_fig = build_candlestick_chart(
                visible_frame,
                f"{chart_request['ticker']} · {chart_request['interval']} replay",
                levels=chart_levels,
                markers=replay_markers,
                show_emas=False,
            )
            st.plotly_chart(
                replay_fig,
                width="stretch",
                config={"responsive": True, "displaylogo": False},
            )
            st.caption(
                f"Entry is the first bar close at ₹{entry_price:,.2f}; "
                f"the simulated long exits at its first stop/target touch, or at the range end. "
                f"Quantity: {chart_request['quantity']} share(s). "
                "If one bar touches both levels, the stop is counted first."
            )

    st.divider()
    st.subheader("Trade-plan probability analysis")
    st.caption("Optional trader-timemachine Monte Carlo analysis uses the saved trader-exit plan.")
    replay_plan_path = ROOT / "trader" / "plan.json"
    try:
        replay_plan = json.loads(replay_plan_path.read_text(encoding="utf-8")) if replay_plan_path.exists() else None
    except (OSError, json.JSONDecodeError):
        replay_plan = None
    if replay_plan:
        st.caption("Plan: %s · %s · entry ₹%s · stop ₹%s · target ₹%s" % (
            replay_plan.get("symbol", "—"), replay_plan.get("date", "—"), replay_plan.get("entry", "—"),
            replay_plan.get("stop", "—"), replay_plan.get("target", "—")))
    else:
        st.info("Create a plan in Quick Stock Analyzer before running the replay.")
    with st.form("timemachine_form"):
        replay_col, seed_col = st.columns([3, 1])
        with replay_col:
            replay_count = st.slider("Simulated sessions", min_value=1000, max_value=20000, value=10000, step=1000)
        with seed_col:
            replay_seed = st.number_input("Seed", min_value=0, value=19, step=1)
        replay_until_enabled = st.checkbox("Use a flat-by time", value=bool(replay_plan and replay_plan.get("flat_by")))
        replay_until = st.text_input("Flat-by (IST HH:MM)", value=(replay_plan or {}).get("flat_by", "15:25"))
        replay_submit = st.form_submit_button("Run time machine", type="primary", disabled=not bool(replay_plan))
    if replay_submit:
        replay_args = ["run", "--json", "--write", "--replays", str(int(replay_count)), "--seed", str(int(replay_seed))]
        if replay_until_enabled:
            replay_args += ["--until", replay_until]
        with st.spinner("Replaying the plan against recent NSE sessions..."):
            code, stdout, stderr = run_skill("trader-timemachine", "timemachine.py", replay_args, timeout=300)
        replay_result = parse_json_output(stdout)
        if code != 0 or not isinstance(replay_result, dict):
            show_skill_output(code, stdout, stderr)
        else:
            if stderr:
                st.warning(stderr)
            if replay_result.get("verdict"):
                st.markdown("**" + str(replay_result["verdict"]) + "**")
            for note in replay_result.get("verdict_notes") or []:
                st.write(note)
            st.json(replay_result)
            st.caption("Saved to trader/timemachine.json. Results are historical simulations, not forecasts.")
    if (ROOT / "trader" / "timemachine.json").exists():
        if st.button("Render saved fan chart", key="timemachine_chart"):
            with st.spinner("Rendering saved replay chart..."):
                code, stdout, stderr = run_skill(
                    "trader-timemachine", "timemachine.py", ["chart", "--from"], timeout=120)
            if code == 0 and stdout:
                st.code(stdout)
            else:
                show_skill_output(code, stdout, stderr)
