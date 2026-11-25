import datetime
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots
from nse_live import get_live_quote
from orchestrator import SKILLS as AUTOPILOT_SKILLS, ensure_portfolio, run_autopilot

ROOT = Path(__file__).resolve().parent
SKILLS = ROOT / "skills"
WATCHLIST = ROOT / "trader" / "watchlist.json"

st.set_page_config(page_title="Indian NSE Trading Desk", page_icon="₹", layout="wide")
try:
    ensure_portfolio(ROOT)
except (OSError, ValueError) as exc:
    st.error(f"Could not initialize trader/portfolio.json: {exc}")

st.markdown("""
<style>
.block-container {padding-top: 1.5rem; padding-bottom: 2rem; max-width: 1180px;}
[data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.18); border-radius: 8px; padding: 10px; min-width: 0;}
[data-testid="stMetricValue"] {font-size: clamp(.9rem, 2vw, 1.5rem) !important; white-space: nowrap;}
[data-testid="stDataFrame"] {border: 1px solid rgba(128,128,128,.18); border-radius: 8px;}
.stButton > button {width: 100%; border-radius: 6px; min-height: 42px;}
.stTabs [data-baseweb="tab-list"] {gap: .25rem; flex-wrap: wrap;}
.stTabs [data-baseweb="tab"] {white-space: normal;}
@media (max-width: 640px) {
  .block-container {padding-left: .75rem; padding-right: .75rem;}
  h1 {font-size: 1.7rem !important;}
  .stTabs [data-baseweb="tab"] {padding: .4rem .55rem; font-size: .85rem;}
  [data-testid="stMetric"] {padding: 6px;}
  [data-testid="stMetricValue"] {font-size: .88rem !important;}
}
</style>
""", unsafe_allow_html=True)

st.title("Indian NSE Trading Desk")
st.caption("NSE · INR (₹) · IST 9:15 AM–3:30 PM")

with st.sidebar.expander("Optional Broker Settings", expanded=False):
    broker_mode = st.selectbox(
        "Broker mode",
        ["Off / Paper Mode", "Angel One (optional)", "DhanHQ (optional)"],
        key="optional_broker_mode",
    )
    st.caption(
        "Free NSE/Yahoo quotes and paper proposals do not need broker credentials. "
        "Values stay in this Streamlit session; they are not written to files or used by the pipeline."
    )
    if broker_mode == "Angel One (optional)":
        st.text_input("Angel One API key", type="password", key="optional_angel_api_key")
        st.text_input("Angel One client code", key="optional_angel_client_code")
        st.text_input("Angel One PIN", type="password", key="optional_angel_pin")
        st.text_input("Angel One TOTP secret", type="password", key="optional_angel_totp")
    elif broker_mode == "DhanHQ (optional)":
        st.text_input("DhanHQ client ID", key="optional_dhan_client_id")
        st.text_input("DhanHQ access token", type="password", key="optional_dhan_access_token")


def nse_ticker(value: str) -> str:
    ticker = re.sub(r"\s+", "", str(value or "")).upper()
    if not ticker:
        return ""
    while ticker.endswith(".NS"):
        ticker = ticker[:-3]
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


def _scanner_rows_to_frame(rows, company_names, include_status=False):
    output = []
    for stock in rows:
        setups = stock.get("setups") or stock.get("setup") or []
        if isinstance(setups, str):
            setups = [setups]
        setup_status = ", ".join(setups)
        if include_status and not setup_status:
            setup_status = "Scanned · no matching setup"
        symbol = str(stock.get("symbol", ""))
        average_volume = stock.get(
            "average_daily_volume", stock.get("avg_daily_volume")
        )
        liquid = stock.get("liquid")
        liquid_label = (
            "Yes" if liquid is True else
            "No" if liquid is False else
            "—"
        )
        output.append({
            "Symbol": symbol,
            "Company": company_names.get(nse_ticker(symbol), ""),
            "Price (₹)": stock.get("price"),
            "RSI": stock.get("rsi"),
            "Setup / Status": setup_status,
            "Volume": stock.get("volume"),
            "Avg Daily Volume": average_volume,
            "Liquid": liquid_label,
            "Close Date": stock.get("close_date", ""),
        })
    return pd.DataFrame(output)


def _all_scanned_rows(doc):
    rows = doc.get("stocks") or doc.get("hits") or []
    if not doc.get("stocks"):
        return rows

    def numeric_value(value):
        if value is None:
            return float("nan")
        return pd.to_numeric(str(value).replace(",", ""), errors="coerce")

    mode = str(doc.get("mode") or "").lower()
    filters = doc.get("filters") or {}
    try:
        minimum_price = float(filters.get("min_price", 0) or 0)
        minimum_volume = float(filters.get("min_volume", 0) or 0)
    except (TypeError, ValueError):
        minimum_price = minimum_volume = 0

    if mode == "daily":
        return [
            row for row in rows
            if row.get("liquid") is True
            and numeric_value(row.get("data_bars")) >= 30
            and numeric_value(row.get("price")) >= minimum_price
            and numeric_value(row.get("volume")) >= minimum_volume
        ]

    if mode == "orb":
        try:
            cutoff = datetime.datetime.strptime(
                str(doc.get("as_of", "")).removesuffix(" IST"),
                "%Y-%m-%d %H:%M",
            )
            session_open = datetime.datetime.combine(
                cutoff.date(), datetime.time(9, 15)
            )
            session_close = datetime.datetime.combine(
                cutoff.date(), datetime.time(15, 30)
            )
            cutoff = min(cutoff, session_close)
            expected_bars = int(
                (cutoff - session_open).total_seconds() // (5 * 60)
            )
        except ValueError:
            expected_bars = None
        if expected_bars is not None:
            return [
                row for row in rows
                if row.get("liquid") is True
                and numeric_value(row.get("data_bars")) == expected_bars
                and numeric_value(row.get("price")) >= minimum_price
                and numeric_value(row.get("volume")) >= minimum_volume
            ]
    return rows


def load_watchlist():
    if not WATCHLIST.exists():
        return {}, pd.DataFrame(), pd.DataFrame()
    try:
        doc = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": str(exc)}, pd.DataFrame(), pd.DataFrame()

    company_names = {}
    try:
        for line in (ROOT / "trader" / "universe.txt").read_text(encoding="utf-8").splitlines():
            symbol, separator, name = line.partition("|")
            if separator:
                company_names[nse_ticker(symbol)] = name.strip()
    except OSError:
        pass
    hits_frame = _scanner_rows_to_frame(doc.get("hits") or [], company_names)
    scanned_frame = _scanner_rows_to_frame(
        _all_scanned_rows(doc), company_names, include_status=True
    )
    return doc, hits_frame, scanned_frame


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


@st.cache_data(ttl=60, show_spinner=False)
def load_live_daily_history(ticker: str):
    """Fetch a recent daily window for the scanner's latest quote and setup metrics."""
    frame = yf.Ticker(ticker).history(
        period="3mo",
        interval="1d",
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


@st.cache_data(ttl=20, show_spinner=False)
def load_live_quote(ticker: str):
    return get_live_quote(ticker)


def chart_x_values(frame: pd.DataFrame, interval: str):
    """Use ordered category positions so sessions have no overnight/weekend gaps."""
    intraday = interval.lower() != "1d"
    labels = []
    for value in frame.index:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("Asia/Kolkata")
        labels.append(stamp.strftime("%d %b %H:%M" if intraday else "%d %b %Y"))
    return labels


def build_candlestick_chart(
    frame: pd.DataFrame,
    title: str,
    levels: list[tuple[str, float, str]] | None = None,
    markers: list[dict] | None = None,
    show_emas: bool = True,
    interval: str = "1d",
):
    x_values = chart_x_values(frame, interval)
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[0.76, 0.24],
    )
    fig.add_trace(
        go.Candlestick(
            x=x_values,
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
                    x=x_values,
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
            x=x_values,
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
            line_dash="dot",
            line_color=color,
            line_width=1,
            row=1,
            col=1,
        )
        fig.add_annotation(
            x=1.015,
            y=price,
            xref="paper",
            yref="y",
            text=f"{label} ₹{price:,.2f}",
            showarrow=False,
            xanchor="left",
            yanchor="middle",
            bgcolor=color,
            bordercolor=color,
            borderpad=4,
            font={"color": "#ffffff", "size": 10},
        )
    for marker in markers or []:
        fig.add_trace(
            go.Scatter(
                x=[chart_x_values(pd.DataFrame(index=[marker["x"]]), interval)[0]],
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
        title={"text": title, "x": 0.01, "xanchor": "left", "y": 0.99},
        template="plotly_dark",
        paper_bgcolor="#0b0e11",
        plot_bgcolor="#0b0e11",
        font={"color": "#d1d4dc", "family": "Inter, sans-serif"},
        height=590,
        margin={"l": 12, "r": 132, "t": 70, "b": 82},
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "top", "y": -0.12, "x": 0},
        xaxis_rangeslider_visible=False,
    )
    fig.update_yaxes(
        title_text="Price (INR)", tickprefix="₹", tickformat=",.2f",
        gridcolor="#20252b", zerolinecolor="#20252b",
        showspikes=True, spikemode="across", spikesnap="cursor",
        spikecolor="#75808a", spikethickness=1, row=1, col=1,
    )
    fig.update_yaxes(
        title_text="Volume", tickformat="~s", gridcolor="#20252b",
        zerolinecolor="#20252b", row=2, col=1,
    )
    fig.update_xaxes(
        type="category", categoryorder="array", categoryarray=x_values,
        showgrid=True, gridcolor="#20252b",
        showspikes=True, spikemode="across", spikesnap="cursor",
        spikecolor="#75808a", spikethickness=1,
    )
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
    fig = build_candlestick_chart(
        frame, title, levels=levels, markers=markers, show_emas=show_emas, interval=interval,
    )
    st.plotly_chart(fig, width="stretch", config={"responsive": True, "displaylogo": False})
    st.caption("Yahoo Finance data is unofficial and may be delayed.")
    return frame


@st.cache_data(ttl=900, show_spinner=False)
def yahoo_fundamentals_fallback(ticker: str, quarter_count: int):
    """Best-effort NSE fundamentals and quarterly income statement from Yahoo."""
    stock = yf.Ticker(ticker)
    warnings = []
    try:
        info = stock.get_info() or {}
        if not isinstance(info, dict):
            info = {}
    except Exception as exc:
        info = {}
        warnings.append(f"Yahoo fundamentals unavailable: {exc}")

    income = pd.DataFrame()
    for attribute in ("quarterly_income_stmt", "quarterly_financials"):
        try:
            candidate = getattr(stock, attribute)
        except Exception as exc:
            warnings.append(f"Yahoo {attribute.replace('_', ' ')} unavailable: {exc}")
            continue
        if isinstance(candidate, pd.DataFrame) and not candidate.empty:
            income = candidate
            break

    def statement_row(aliases):
        labels = {str(label).strip().lower(): label for label in income.index}
        for alias in aliases:
            if alias in labels:
                return labels[alias]
        for label in income.index:
            normalized = str(label).strip().lower()
            if any(alias in normalized for alias in aliases):
                return label
        return None

    revenue_row = statement_row(("total revenue", "operating revenue", "revenue"))
    profit_row = statement_row((
        "net income", "net income common stockholders", "net income from continuing operation",
    ))
    if revenue_row is None:
        warnings.append("Yahoo quarterly statement has no revenue row.")
    if profit_row is None:
        warnings.append("Yahoo quarterly statement has no net income row.")

    quarters = []
    for period in sorted(income.columns, key=lambda value: pd.Timestamp(value), reverse=True):
        def statement_value(row):
            if row is None:
                return None
            value = pd.to_numeric(income.at[row, period], errors="coerce")
            return None if pd.isna(value) else float(value) / 10_000_000

        stamp = pd.Timestamp(period)
        quarters.append({
            "Quarter End": stamp.strftime("%Y-%m-%d"),
            "Revenue (₹ Cr)": statement_value(revenue_row),
            "Net Profit (₹ Cr)": statement_value(profit_row),
        })
        if len(quarters) >= quarter_count:
            break

    def numeric_info(*keys):
        for key in keys:
            try:
                value = float(info.get(key))
            except (TypeError, ValueError):
                continue
            if pd.notna(value):
                return value
        return None

    market_cap = numeric_info("marketCap")
    roe = numeric_info("returnOnEquity")
    return {
        "entity": info.get("shortName") or info.get("longName") or ticker,
        "source": "Yahoo Finance fallback · unofficial",
        "market_cap_cr": market_cap / 10_000_000 if market_cap is not None else None,
        "pe": numeric_info("trailingPE"),
        "roe_pct": roe * 100 if roe is not None else None,
        "quarters": quarters,
        "warnings": warnings,
    }


def show_yahoo_fundamentals(fundamentals: dict):
    st.caption(f"{fundamentals['entity']} · {fundamentals['source']} · values in INR")
    market_col, pe_col, roe_col = st.columns(3)
    market_cap = fundamentals.get("market_cap_cr")
    pe = fundamentals.get("pe")
    roe = fundamentals.get("roe_pct")
    market_col.metric("Market Cap (₹ Cr)", "—" if market_cap is None else f"₹{market_cap:,.0f}")
    pe_col.metric("Trailing P/E", "—" if pe is None else f"{pe:,.2f}")
    roe_col.metric("ROE", "—" if roe is None else f"{roe:.1f}%")

    quarters = fundamentals.get("quarters") or []
    if quarters:
        quarter_frame = pd.DataFrame(quarters)
        st.dataframe(
            quarter_frame,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Revenue (₹ Cr)": st.column_config.NumberColumn(format="₹%.2f"),
                "Net Profit (₹ Cr)": st.column_config.NumberColumn(format="₹%.2f"),
            },
        )
        chart_frame = quarter_frame.set_index("Quarter End").sort_index()
        figure = go.Figure()
        figure.add_bar(
            x=chart_frame.index, y=chart_frame["Revenue (₹ Cr)"],
            name="Revenue", marker_color="#26a69a",
        )
        figure.add_bar(
            x=chart_frame.index, y=chart_frame["Net Profit (₹ Cr)"],
            name="Net Profit", marker_color="#2962ff",
        )
        figure.update_layout(
            title={"text": "Quarterly Revenue & Net Profit", "x": 0.02, "xanchor": "left"},
            template="plotly_dark", paper_bgcolor="#0b0e11", plot_bgcolor="#0b0e11",
            font={"color": "#d1d4dc"}, barmode="group", height=360,
            margin={"l": 12, "r": 12, "t": 60, "b": 12},
            yaxis={"title": "₹ Crore", "gridcolor": "#20252b"},
            xaxis={"type": "category", "categoryorder": "array", "categoryarray": list(chart_frame.index)},
            legend={"orientation": "h", "y": 1.02, "x": 0},
        )
        st.plotly_chart(figure, width="stretch", config={"responsive": True, "displaylogo": False})
    else:
        st.info("Yahoo Finance did not return quarterly income statement rows for this ticker.")

    for warning in fundamentals.get("warnings") or []:
        st.caption(warning)


if st.session_state.pop("_navigate_to_analyzer", False):
    deep_analyze_ticker = st.session_state.pop("_deep_analyze_ticker", None)
    if deep_analyze_ticker:
        st.session_state["analyzer_ticker_input"] = deep_analyze_ticker
        st.session_state["analyzer_run_requested"] = True
        st.session_state["trading_tabs"] = "Quick Stock Analyzer"

autopilot_tab, scanner_tab, analyzer_tab, analyst_tab, budget_broker_tab, alerts_deals_tab, timemachine_tab = st.tabs(
    [
        "11-Skill Auto-Pilot",
        "NSE Scanner",
        "Quick Stock Analyzer",
        "Analyst",
        "Budget & Broker",
        "Alerts & Deals",
        "Time Machine",
    ],
    key="trading_tabs",
    on_change="rerun",
)

with autopilot_tab:
    st.subheader("Strict NSE trade qualification")
    st.caption(
        "Runs the 11 local trader skills in order. Replay qualification requires at least 40% green paths, "
        "positive expected value, and R:R of at least 1.5. Tickets are paper proposals only; short ideas "
        "do not authorize live short selling."
    )
    autopilot_mode = st.selectbox(
        "Auto-Pilot mode",
        ["F&O Intraday (5m/15m)", "Cash Swing (Daily)"],
        index=0,
        key="autopilot_mode",
    )
    mode_key = "fo_intraday" if autopilot_mode.startswith("F&O") else "cash_swing"
    if st.button("Run 11-Skill Auto-Pilot", type="primary", key="autopilot_run"):
        progress_box = st.empty()
        progress_log = []

        def update_autopilot(step, skill, message):
            progress_log[:] = [
                line for line in progress_log
                if not line.startswith(f"{step}/11 · {skill} · ")
            ]
            progress_log.append(f"{step}/11 · {skill} · {message}")
            st.session_state["autopilot_trace"] = list(progress_log)
            progress_box.markdown("\n\n".join(f"- {line}" for line in progress_log[-11:]))

        with st.spinner("Executing qualification pipeline; no trades are sent to a broker..."):
            try:
                st.session_state["autopilot_result"] = run_autopilot(
                    ROOT, update_autopilot, mode=mode_key,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                st.session_state["autopilot_result"] = {
                    "error": str(exc),
                    "mode": mode_key,
                    "qualified": [],
                    "verification": [
                        {
                            "skill": skill,
                            "status": "FAIL" if index == 0 else "WARN",
                            "summary": str(exc) if index == 0 else "Not run because the pipeline stopped.",
                            "output": str(exc) if index == 0 else "Not run because the pipeline stopped.",
                        }
                        for index, skill in enumerate(AUTOPILOT_SKILLS)
                    ],
                    "market": {"mood": "Caution", "reason": "Pipeline error; no trades qualified."},
                }

    autopilot_result = st.session_state.get("autopilot_result")
    has_autopilot_result = bool(autopilot_result)
    if not autopilot_result:
        autopilot_result = {
            "market": {"mood": "Caution", "reason": "Run Auto-Pilot to evaluate current F&O candidates."},
            "qualified": [],
            "candidates": [],
        }
    if autopilot_result:
        mood = autopilot_result.get("market") or {}
        mood_label = mood.get("mood", "Caution")
        mood_color = {"Bullish": "🟢", "Caution": "🟠", "Bearish": "🔴"}.get(mood_label, "🟠")
        st.markdown(f"### {mood_color} Market Mood: **{mood_label}**")
        st.caption(mood.get("reason", "Market regime data unavailable."))
        qualified = autopilot_result.get("qualified") or []
        candidates = autopilot_result.get("candidates") or []

        funnel_columns = [
            "Symbol", "Direction", "Entry", "SL", "Target", "Target 1", "Target 2", "R:R",
            "Time Machine Win %", "Expected Value (R)", "Confidence Score",
            "Passed/Rejected Reason",
        ]
        funnel_frame = pd.DataFrame(candidates).reindex(columns=funnel_columns)
        st.markdown("#### Top 10 Candidates Funnel Table")
        st.dataframe(
            funnel_frame,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Entry": st.column_config.NumberColumn(format="₹%.2f"),
                "SL": st.column_config.NumberColumn(format="₹%.2f"),
                "Target": st.column_config.NumberColumn(format="₹%.2f"),
                "Target 1": st.column_config.NumberColumn(format="₹%.2f"),
                "Target 2": st.column_config.NumberColumn(format="₹%.2f"),
                "R:R": st.column_config.NumberColumn(format="%.2f"),
                "Time Machine Win %": st.column_config.NumberColumn(format="%.1f%%"),
                "Expected Value (R)": st.column_config.NumberColumn(format="%+.3fR"),
                "Confidence Score": st.column_config.NumberColumn(format="%.1f"),
                "Passed/Rejected Reason": st.column_config.TextColumn(width="large"),
            },
        )
        if autopilot_result.get("error"):
            st.error(autopilot_result["error"])

        if qualified:
            best = qualified[0]
            best_plan = best["plan"]
            with st.container(border=True):
                mode_title = "F&O Intraday" if autopilot_result.get("mode") == "fo_intraday" else "Cash Swing"
                st.markdown(f"#### Best {mode_title} Setup · {best['symbol']}")
                st.caption("Highest-ranked rules-qualified paper proposal. This is not a recommendation.")
                summary_cols = st.columns(7)
                summary_cols[0].metric("Direction", str(best.get("_funnel", {}).get("Direction", "LONG")))
                summary_cols[1].metric("Entry", f"₹{best_plan['entry']:,.2f}")
                summary_cols[2].metric("Stop-Loss", f"₹{best_plan['stop']:,.2f}")
                summary_cols[3].metric("Target 1", f"₹{best_plan.get('target1', best_plan['target']):,.2f}")
                summary_cols[4].metric("Target 2", f"₹{best_plan.get('target2', best_plan['target']):,.2f}")
                summary_cols[5].metric(
                    "R:R",
                    f"{best_plan['reward_risk']:.2f}",
                )
                summary_cols[6].metric(
                    "Qty · Confidence",
                    f"{best['sizing']['qty']:,} · {best.get('_funnel', {}).get('Confidence Score', '—')}",
                )

        trace = st.session_state.get("autopilot_trace") or []
        if trace:
            with st.expander("11-Skill Handoff Trace", expanded=True):
                for line in trace:
                    st.markdown(f"- {line}")

        if not qualified:
            if has_autopilot_result:
                st.warning("No Qualified Trade Found Today (Capital Protected)")
        else:
            for trade in qualified:
                plan = trade["plan"]
                st.markdown(f"### Best Trade · {trade['symbol']}")
                st.caption("Rules-qualified paper proposal; not a recommendation.")
                quote = trade.get("quote") or {}
                p1, p2, p3, p4, p5, p6, p7, p8 = st.columns(8)
                p1.metric("Entry", f"₹{plan['entry']:,.2f}")
                p2.metric("SL", f"₹{plan['stop']:,.2f}")
                p3.metric("Target", f"₹{plan['target']:,.2f}")
                p4.metric("Qty", f"{trade['sizing']['qty']:,}")
                p5.metric("Risk", f"₹{trade['sizing']['max_loss']:,.2f}")
                p6.metric("R:R", f"{plan['reward_risk']:.2f}")
                p7.metric("LTP", "—" if quote.get("price") is None else f"₹{quote['price']:,.2f}")
                p8.metric(
                    "Day change",
                    "—" if quote.get("change_pct") is None else f"{quote['change_pct']:+.2f}%",
                )
                if quote:
                    st.caption(f"● {quote['source']} · quote volume {quote.get('volume') or '—'}")

                chart_days = datetime.date.today() - datetime.timedelta(days=60)
                chart_frame = load_price_history(
                    trade["symbol"], chart_days, datetime.date.today(), "5m")
                if not chart_frame.empty:
                    candle_fig = build_candlestick_chart(
                        chart_frame,
                        f"{trade['symbol']} · 5-minute candles",
                        levels=[
                            ("Entry", plan["entry"], "#2962ff"),
                            ("Stop-Loss", plan["stop"], "#ef5350"),
                            ("Target", plan["target"], "#26a69a"),
                        ],
                        interval="5m",
                    )
                    st.plotly_chart(
                        candle_fig, width="stretch",
                        config={"responsive": True, "displaylogo": False},
                    )
                else:
                    st.info(f"No candlestick bars returned for {trade['symbol']}.")

                mc = trade.get("timemachine", {}).get("mc") or {}
                if trade.get("negative_news"):
                    st.warning("Negative-news keywords matched a headline; inspect the linked source before acting.")
                if mc.get("bands"):
                    fan = go.Figure()
                    band_times = mc.get("band_times") or list(range(len(mc["bands"])))
                    for column, label, color in ((4, "90% band", "#607d8b"), (3, "Middle half", "#2962ff")):
                        fan.add_trace(go.Scatter(
                            x=band_times, y=[row[column] for row in mc["bands"]],
                            mode="lines", name=label, line={"color": color, "width": 1},
                        ))
                    fan.add_trace(go.Scatter(
                        x=band_times, y=[row[2] for row in mc["bands"]],
                        mode="lines", name="Median", line={"color": "#26a69a", "width": 2},
                    ))
                    fan.update_layout(
                        title=f"{trade['symbol']} · 500-path Monte Carlo fan",
                        template="plotly_dark", paper_bgcolor="#0b0e11",
                        plot_bgcolor="#0b0e11", font={"color": "#d1d4dc"},
                        yaxis={"title": "Price (INR)", "tickprefix": "₹"},
                    )
                    st.plotly_chart(
                        fan, width="stretch",
                        config={"responsive": True, "displaylogo": False},
                    )
                histogram = mc.get("hist_R") or mc.get("hist") or {}
                if histogram.get("bins"):
                    left, right = histogram.get("lo", 0), histogram.get("hi", 0)
                    if mc.get("hist_R"):
                        risk_per_share = abs(plan["entry"] - plan["stop"])
                        pnl_values = [
                            (left + (right - left) * (index + 0.5) / len(histogram["bins"]))
                            * risk_per_share * trade["sizing"]["qty"]
                            for index in range(len(histogram["bins"]))
                        ]
                    else:
                        pnl_values = [
                            ((left + (right - left) * index / len(histogram["bins"]) - plan["entry"])
                             * (1 if plan.get("side", "buy") == "buy" else -1))
                            * trade["sizing"]["qty"]
                            for index in range(len(histogram["bins"]))
                        ]
                    pnl_fig = go.Figure(go.Bar(x=pnl_values, y=histogram["bins"], name="P&L distribution"))
                    pnl_fig.update_layout(
                        title="Monte Carlo P&L distribution (₹)",
                        template="plotly_dark", paper_bgcolor="#0b0e11",
                        plot_bgcolor="#0b0e11", font={"color": "#d1d4dc"},
                        xaxis={"title": "Simulated P&L (₹)"},
                        yaxis={"title": "Path count"},
                    )
                    st.plotly_chart(
                        pnl_fig, width="stretch",
                        config={"responsive": True, "displaylogo": False},
                    )
                st.caption(
                    f"Monte Carlo green-path probability: {mc.get('p_green', 0):.1%} · "
                    f"Expected value: {mc.get('avg_R', 0):+.3f}R · "
                    f"Institutional-deal conviction points: {trade.get('conviction_score', 0)}"
                )
                if st.button(
                    f"Load {trade['symbol']} in Quick Stock Analyzer",
                    key=f"autopilot_analyze_{trade['symbol']}",
                ):
                    st.session_state["analyzer_ticker_input"] = trade["symbol"]
                    st.session_state["analyzer_run_requested"] = True
                    st.info("Ticker loaded. Open the Quick Stock Analyzer tab to see its fresh analysis.")

        verification = autopilot_result.get("verification") or []
        if verification:
            with st.expander("11-Skill Verification Table", expanded=False):
                table_rows = [{
                    "Skill": row["skill"],
                    "Status": row["status"],
                    "Real output": row["summary"],
                } for row in verification]
                st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
                for row in verification:
                    with st.expander(f"{row['skill']} · {row['status']}"):
                        st.code(row.get("output") or row["summary"], language="text")

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

    doc, hits_frame, scanned_frame = load_watchlist()
    if isinstance(doc, dict) and doc.get("error"):
        st.error(f"Could not read watchlist: {doc['error']}")
    elif scanned_frame.empty:
        st.info("No watchlist data yet. Run a scanner mode above.")
    else:
        selected_view = st.radio(
            "Scanner view",
            ["Hits Only (matched setups)", "All Scanned Stocks"],
            horizontal=True,
            key="scanner_table_view",
        )
        is_hits_view = selected_view == "Hits Only (matched setups)"
        frame = hits_frame if is_hits_view else scanned_frame
        search = st.text_input("Search symbols or setup", key="watchlist_search")
        filtered = frame
        if search:
            needle = search.casefold()
            mask = filtered.astype(str).apply(lambda column: column.str.casefold().str.contains(needle, regex=False)).any(axis=1)
            filtered = filtered[mask]

        filtered_symbols = "\x1f".join(
            nse_ticker(str(symbol)) for symbol in filtered.get("Symbol", [])
        )
        table_key = "scanner_watchlist_" + hashlib.sha1(
            filtered_symbols.encode("utf-8")
        ).hexdigest()[:12]

        meta1, meta2, meta3 = st.columns(3)
        meta1.metric("Mode", str(doc.get("mode", "—")).upper())
        meta2.metric("As of", str(doc.get("as_of", "—")))
        with meta3:
            with st.popover(f"Watchlist: {len(scanned_frame)} Stocks"):
                popup_filter = st.text_input(
                    "Filter stocks",
                    placeholder="Company or ticker",
                    key="watchlist_popup_filter",
                )
                popup_rows = scanned_frame
                if popup_filter:
                    popup_needle = popup_filter.casefold()
                    popup_mask = popup_rows[["Symbol", "Company"]].astype(str).apply(
                        lambda column: column.str.casefold().str.contains(
                            popup_needle, regex=False
                        )
                    ).any(axis=1)
                    popup_rows = popup_rows[popup_mask]
                st.caption(f"{len(popup_rows)} of {len(frame)} stocks")
                for start in range(0, len(popup_rows), 3):
                    button_columns = st.columns(3)
                    for column, (row_index, row) in zip(
                        button_columns,
                        popup_rows.iloc[start : start + 3].iterrows(),
                    ):
                        symbol = nse_ticker(str(row["Symbol"]))
                        company_value = row.get("Company")
                        company = (
                            str(company_value).strip()
                            if pd.notna(company_value)
                            else ""
                        )
                        button_label = f"{company} · {symbol}" if company else symbol
                        with column:
                            if st.button(
                                button_label,
                                key=f"watchlist_pick_{row_index}_{symbol}",
                                help=f"Show {symbol} chart",
                            ):
                                st.session_state["selected_scanner_symbol"] = symbol
                                st.session_state.pop(table_key, None)

        st.caption(
            f"Showing {len(filtered):,} of {len(frame):,} "
            f"{'matched setup hits' if is_hits_view else 'scanned stocks'}."
        )
        table_event = st.dataframe(
            format_watchlist(filtered),
            use_container_width=True,
            height=560,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key=table_key,
            column_config={
                "Price (₹)": st.column_config.NumberColumn(format="₹%.2f"),
                "RSI": st.column_config.NumberColumn(format="%.1f"),
                "Volume": st.column_config.NumberColumn(format="%,d"),
                "Avg Daily Volume": st.column_config.NumberColumn(format="%,d"),
            },
        )
        selected_rows = table_event.selection.rows
        if selected_rows:
            selected = filtered.iloc[selected_rows[0]]
            st.session_state["selected_scanner_symbol"] = nse_ticker(
                str(selected["Symbol"])
            )

        chart_ticker = st.session_state.get("selected_scanner_symbol")
        if chart_ticker:
            selected_rows = scanned_frame[
                scanned_frame["Symbol"].map(
                    lambda symbol: nse_ticker(str(symbol))
                ) == chart_ticker
            ]
            selected = selected_rows.iloc[0] if not selected_rows.empty else {}
            st.markdown(f"#### {chart_ticker} · Live scanner detail")
            quote = load_live_quote(chart_ticker)
            if quote:
                quote_cols = st.columns(5)
                quote_cols[0].metric("LTP", f"₹{quote['price']:,.2f}")
                quote_cols[1].metric(
                    "Day High",
                    "—" if quote.get("day_high") is None else f"₹{quote['day_high']:,.2f}",
                )
                quote_cols[2].metric(
                    "Day Low",
                    "—" if quote.get("day_low") is None else f"₹{quote['day_low']:,.2f}",
                )
                quote_cols[3].metric(
                    "Change",
                    "—" if quote.get("change_pct") is None else f"{quote['change_pct']:+.2f}%",
                )
                quote_cols[4].metric(
                    "Volume",
                    "—" if quote.get("volume") is None else f"{quote['volume']:,}",
                )
                st.caption(f"● {quote['source']} · {quote.get('as_of', 'time unavailable')}")
            else:
                st.caption("Live quote unavailable from NSE and Yahoo.")

            live_frame = load_live_daily_history(chart_ticker)
            if not live_frame.empty:
                latest = live_frame.iloc[-1]
                average_volume = float(live_frame["Volume"].tail(20).mean())
                q1, q2, q3 = st.columns(3)
                q1.metric("Latest close", f"₹{float(latest['Close']):,.2f}")
                q2.metric("20-session avg volume", f"{average_volume:,.0f}")
                q3.metric("Scanner setup", str(selected.get("Setup / Status") or "—"))
                st.caption(
                    f"Watchlist setup: {selected.get('Setup / Status') or '—'} · "
                    f"Scanner RSI: {selected.get('RSI') if pd.notna(selected.get('RSI')) else '—'} · "
                    f"Watchlist volume: {selected.get('Volume') if pd.notna(selected.get('Volume')) else '—'}"
                )
            else:
                st.warning(f"No current Yahoo Finance setup data returned for {chart_ticker}.")

            chart_column, action_column = st.columns([5, 1])
            with chart_column:
                show_stock_chart(
                    chart_ticker,
                    datetime.date.today() - datetime.timedelta(days=365),
                    datetime.date.today(),
                    "1d",
                    f"{chart_ticker} · Daily Candles",
                )
            with action_column:
                st.markdown("#### Deep Analyze")
                if st.button(
                    "Open full analysis",
                    key=f"scanner_analyze_{chart_ticker}",
                    use_container_width=True,
                ):
                    st.session_state["_deep_analyze_ticker"] = chart_ticker
                    st.session_state["_navigate_to_analyzer"] = True
        elif search and filtered.empty:
            st.info("No watchlist rows match this search.")

with analyzer_tab:
    col_input, col_timeframe, col_action = st.columns([3, 1, 1])
    with col_input:
        ticker_input = st.text_input(
            "NSE ticker",
            placeholder="RELIANCE or RELIANCE.NS",
            key="analyzer_ticker_input",
        )
    with col_timeframe:
        analyzer_timeframe = st.selectbox("Chart timeframe", ["5m", "15m", "1h", "1D"], index=3)
    with col_action:
        analyze = st.button("Analyze", type="primary", use_container_width=True)
    if st.session_state.pop("analyzer_run_requested", False):
        analyze = True

    if analyze:
        ticker = nse_ticker(ticker_input)
        if not ticker:
            st.warning("Enter an NSE ticker.")
        else:
            st.subheader(ticker)
            quote = get_live_quote(ticker)
            if quote:
                quote_cols = st.columns(5)
                quote_cols[0].metric("LTP", f"₹{quote['price']:,.2f}")
                quote_cols[1].metric(
                    "Day High", "—" if quote.get("day_high") is None else f"₹{quote['day_high']:,.2f}")
                quote_cols[2].metric(
                    "Day Low", "—" if quote.get("day_low") is None else f"₹{quote['day_low']:,.2f}")
                quote_cols[3].metric(
                    "Change", "—" if quote.get("change_pct") is None else f"{quote['change_pct']:+.2f}%")
                quote_cols[4].metric(
                    "Volume", "—" if quote.get("volume") is None else f"{quote['volume']:,}")
                st.caption(f"● {quote['source']} · {quote.get('as_of', 'time unavailable')}")
            else:
                st.caption("Quote unavailable from NSE and Yahoo.")
            entry_doc = None
            with st.spinner(f"Running a fresh trader-entry check for {ticker}..."):
                entry_code, entry_stdout, entry_stderr = run_skill(
                    "trader-entry", "entry.py", ["check", ticker, "--json"])
            parsed_entry = parse_json_output(entry_stdout)
            if (
                entry_code in (0, 1)
                and isinstance(parsed_entry, dict)
                and nse_ticker(parsed_entry.get("symbol", "")) == ticker
                and not parsed_entry.get("fallback_session")
            ):
                entry_doc = parsed_entry

            st.markdown("**Fresh Entry Check**")
            if entry_doc is None:
                st.error(entry_stderr or entry_stdout or f"No fresh entry data returned for {ticker}.")
            else:
                status = str(entry_doc.get("status", "UNKNOWN")).upper()
                st.metric("Status", status)
                if entry_doc.get("reason"):
                    st.caption(entry_doc["reason"])

            entry_price = None
            or_high = or_low = None
            if entry_doc and entry_doc.get("status") in ("triggered", "waiting"):
                trigger = entry_doc.get("trigger") or {}
                opening_range = entry_doc.get("opening_range") or {}
                entry_price = trigger.get("price") or entry_doc.get("level")
                or_high = opening_range.get("high")
                or_low = opening_range.get("low")

            exit_doc = None
            if entry_doc is not None and entry_price is not None:
                exit_args = [
                    "plan", "--symbol", ticker, "--entry", str(entry_price),
                    "--date", str(entry_doc.get("date") or datetime.date.today()),
                    "--json",
                ]
                if or_high is not None and or_low is not None:
                    exit_args += [
                        "--or-high", str(or_high), "--or-low", str(or_low), "--setup", "orb",
                    ]
                else:
                    exit_args += ["--setup", "level"]
                if entry_doc.get("time"):
                    exit_args += ["--time", str(entry_doc["time"])]
                with st.spinner(f"Calculating a fresh trader-exit plan for {ticker}..."):
                    exit_code, exit_stdout, exit_stderr = run_skill(
                        "trader-exit", "exit.py", exit_args)
                parsed_exit = parse_json_output(exit_stdout)
                if (
                    exit_code == 0
                    and isinstance(parsed_exit, dict)
                    and nse_ticker(parsed_exit.get("symbol", "")) == ticker
                ):
                    exit_doc = parsed_exit
                else:
                    st.error(exit_stderr or exit_stdout or "Fresh exit plan is unavailable.")
            elif entry_doc is not None:
                st.info("No current entry level or trigger was returned; no exit plan was calculated.")
            else:
                st.info("Entry data failed for this request; no prior entry or exit plan is reused.")

            st.markdown("**Fresh Exit Plan**")
            if exit_doc:
                m1, m2, m3 = st.columns(3)
                m1.metric("Entry", f"₹{float(exit_doc['entry']):,.2f}")
                m2.metric("Stop", f"₹{float(exit_doc['stop']):,.2f}")
                m3.metric("Target", f"₹{float(exit_doc['target']):,.2f}")
            else:
                st.caption("No plan values are displayed because this ticker has no fresh valid plan.")

            chart_levels = []
            if exit_doc:
                chart_levels = [
                    ("Entry", float(exit_doc["entry"]), "#2962ff"),
                    ("Stop-Loss", float(exit_doc["stop"]), "#ef5350"),
                    ("Target", float(exit_doc["target"]), "#26a69a"),
                ]
            st.markdown("#### Price Chart")
            chart_interval = {"5m": "5m", "15m": "15m", "1h": "1h", "1D": "1d"}[analyzer_timeframe]
            history_days = {"5m": 7, "15m": 60, "1h": 365, "1d": 365}[chart_interval]
            show_stock_chart(
                ticker,
                datetime.date.today() - datetime.timedelta(days=history_days),
                datetime.date.today(),
                chart_interval,
                f"{ticker} · {'Daily' if analyzer_timeframe == '1D' else analyzer_timeframe}",
                levels=chart_levels,
            )

            risk_col, news_col = st.columns(2)
            with risk_col:
                st.markdown("**Fresh Risk Check**")
                if not exit_doc:
                    st.info("Risk was not run: this ticker has no fresh exit plan to size.")
                else:
                    with tempfile.TemporaryDirectory(prefix="nse-analyzer-") as temp_dir:
                        temp_plan = Path(temp_dir) / "plan.json"
                        temp_orders = Path(temp_dir) / "orders.json"
                        temp_plan.write_text(json.dumps(exit_doc), encoding="utf-8")
                        try:
                            portfolio = json.loads(
                                (ROOT / "trader" / "portfolio.json").read_text(encoding="utf-8"))
                            cash = float(portfolio.get("cash", 100000))
                        except (OSError, ValueError, TypeError):
                            cash = 100000.0
                        with st.spinner(f"Running fresh sizing and trader-risk validation for {ticker}..."):
                            size_code, size_stdout, size_stderr = run_skill(
                                "trader-budget", "size.py",
                                [
                                    "--plan", str(temp_plan), "--portfolio", "trader/portfolio.json",
                                    "--budget", str(cash), "--write", str(temp_orders), "--json",
                                ],
                            )
                            size_doc = parse_json_output(size_stdout)
                            if size_code != 0 or not isinstance(size_doc, dict) or not size_doc.get("orders"):
                                st.error(size_stderr or size_stdout or "Fresh position sizing returned no order.")
                            else:
                                risk_code, risk_stdout, risk_stderr = run_skill(
                                    "trader-risk", "check.py",
                                    [
                                        "--orders", str(temp_orders), "--portfolio", "trader/portfolio.json",
                                        "--json", "--no-write",
                                    ],
                                )
                                risk_doc = parse_json_output(risk_stdout)
                                if risk_code not in (0, 1) or not isinstance(risk_doc, dict):
                                    st.error(risk_stderr or risk_stdout or "Fresh risk check failed.")
                                else:
                                    results = risk_doc.get("results") or []
                                    for verdict in results:
                                        verdict_label = verdict.get("verdict", "UNKNOWN")
                                        label = f"{ticker} · {verdict_label}"
                                        st.success(label) if verdict_label == "PASS" else st.error(label)
                                        for reason in verdict.get("reasons") or []:
                                            st.caption(reason)
                                    if not results:
                                        st.json(risk_doc)

            with news_col:
                st.markdown("**Recent News**")
                with st.spinner(f"Running trader-news for {ticker}..."):
                    news_code, news_stdout, news_stderr = run_skill(
                        "trader-news", "news.py",
                        ["--tickers", ticker, "--json", "--no-sec", "--no-moves", "--dry-run", "--all", "--hours", "48"],
                        timeout=150,
                    )
                    news_doc = parse_json_output(news_stdout)
                    if news_code not in (0, 1) or not isinstance(news_doc, dict):
                        st.warning(news_stderr or news_stdout or f"No news data available (exit {news_code})")
                    else:
                        items = news_doc.get("items") or []
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
                            st.info("No exact ticker/company-match headlines found.")
                        if news_doc.get("notes"):
                            for note in news_doc["notes"]:
                                st.caption(note)

with analyst_tab:
    st.subheader("NSE Fundamentals & Filings")
    st.caption("Official NSE filings when available; any Yahoo Finance fallback is labeled. Analysis only, not a recommendation.")
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
            has_nse_quarterly_income = False
            if isinstance(reports, list):
                for report in reports:
                    rows = (report.get("result") or {}).get("rows") or []
                    if rows and rows[0].get("revenue") is not None and rows[0].get("net_income") is not None:
                        has_nse_quarterly_income = True
                        break
            if ticker.endswith(".NS") and not has_nse_quarterly_income:
                if stderr or stdout:
                    st.warning(stderr or stdout)
                with st.spinner(f"Falling back to Yahoo Finance fundamentals for {ticker}..."):
                    fallback = yahoo_fundamentals_fallback(ticker, int(analyst_quarters))
                show_yahoo_fundamentals(fallback)
            elif code not in (0, 1):
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
