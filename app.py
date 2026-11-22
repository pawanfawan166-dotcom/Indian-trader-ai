import yfinance as yf
import pandas as pd
from datetime import datetime

NSE_INDEX_MAP = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
}

TOP_NSE_STOCKS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "LT.NS", "AXISBANK.NS",
    "BAJFINANCE.NS", "MARUTI.NS", "SUNPHARMA.NS", "TATAMOTORS.NS",
    "M&M.NS", "TITAN.NS", "KOTAKBANK.NS", "NTPC.NS", "ADANIENT.NS", "TATASTEEL.NS"
]

def to_nse(symbol: str) -> str:
    s = symbol.strip().upper()
    if s in NSE_INDEX_MAP:
        return NSE_INDEX_MAP[s]
    if not (s.endswith(".NS") or s.endswith(".BO") or s.startswith("^")):
        return f"{s}.NS"
    return s

# 1. TRADER-SCANNER (Indian NSE Top Movers & Volume Scanner)
def scan_indian_stocks() -> dict:
    """Scans top liquid NSE F&O stocks for intraday momentum, % change, and volume."""
    results = []
    for sym in TOP_NSE_STOCKS:
        try:
            df = yf.Ticker(sym).history(period="2d", interval="1d")
            if len(df) >= 2:
                prev_close = float(df["Close"].iloc[-2])
                curr_close = float(df["Close"].iloc[-1])
                pct_change = round(((curr_close - prev_close) / prev_close) * 100, 2)
                vol = int(df["Volume"].iloc[-1])
                results.append({
                    "symbol": sym.replace(".NS", ""),
                    "price_inr": round(curr_close, 2),
                    "change_pct": pct_change,
                    "volume": vol
                })
        except Exception:
            continue

    results.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return {
        "market": "NSE India",
        "top_movers": results[:8]
    }

# 2. TRADER-ENTRY & TRADER-EXIT (5m Intraday VWAP, EMA 9/21, RSI 14)
def analyze_entry_exit(symbol: str) -> dict:
    """Analyzes 5-minute intraday chart for an Indian NSE stock using VWAP, EMA 9/21, and RSI 14."""
    ticker = to_nse(symbol)
    df = yf.Ticker(ticker).history(period="5d", interval="5m")
    if df.empty or len(df) < 25:
        return {"error": f"No 5m intraday data found for {ticker}"}

    # Indicators
    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()

    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    df["RSI14"] = 100 - (100 / (1 + rs))

    # Intraday VWAP (latest day)
    latest_date = df.index[-1].date()
    today_df = df[df.index.date == latest_date].copy()
    if not today_df.empty and today_df["Volume"].sum() > 0:
        tp = (today_df["High"] + today_df["Low"] + today_df["Close"]) / 3
        vwap = float((tp * today_df["Volume"]).sum() / today_df["Volume"].sum())
        day_high = float(today_df["High"].max())
        day_low = float(today_df["Low"].min())
    else:
        vwap = float(df["Close"].iloc[-1])
        day_high = float(df["High"].tail(20).max())
        day_low = float(df["Low"].tail(20).min())

    last = df.iloc[-1]
    price = round(float(last["Close"]), 2)
    ema9 = round(float(last["EMA9"]), 2)
    ema21 = round(float(last["EMA21"]), 2)
    rsi = round(float(last["RSI14"]), 2)

    bias = "BULLISH" if (price > vwap and ema9 > ema21) else ("BEARISH" if (price < vwap and ema9 < ema21) else "SIDEWAYS")

    return {
        "symbol": ticker,
        "ltp_inr": price,
        "vwap": round(vwap, 2),
        "ema9": ema9,
        "ema21": ema21,
        "rsi14": rsi,
        "day_high": round(day_high, 2),
        "day_low": round(day_low, 2),
        "intraday_bias": bias
    }

# 3. TRADER-RISK & TRADER-BUDGET (INR Position Sizing & Risk Check)
def calculate_risk_and_budget(capital_inr: float, risk_percent: float, entry_price: float, stop_loss: float) -> dict:
    """Calculates exact NSE share quantity, max risk in INR, capital needed, and 1:2 / 1:3 targets."""
    risk_per_share = abs(entry_price - stop_loss)
    if risk_per_share <= 0:
        return {"error": "Entry price and stop loss cannot be the same."}

    max_risk_inr = capital_inr * (risk_percent / 100.0)
    qty = int(max_risk_inr // risk_per_share)
    capital_needed = round(qty * entry_price, 2)
    direction = "LONG" if entry_price > stop_loss else "SHORT"

    t1 = round(entry_price + (2 * risk_per_share), 2) if direction == "LONG" else round(entry_price - (2 * risk_per_share), 2)
    t2 = round(entry_price + (3 * risk_per_share), 2) if direction == "LONG" else round(entry_price - (3 * risk_per_share), 2)

    return {
        "direction": direction,
        "capital_inr": capital_inr,
        "max_risk_inr": round(max_risk_inr, 2),
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "recommended_qty": qty,
        "capital_required_inr": capital_needed,
        "target_1_2rr": t1,
        "target_1_3rr": t2
    }

# 4. TRADER-NEWS (Indian Stock News)
def get_indian_stock_news(symbol: str) -> dict:
    """Fetches latest news headlines for an Indian NSE stock."""
    ticker = to_nse(symbol)
    news_items = yf.Ticker(ticker).news or []
    headlines = []
    for item in news_items[:6]:
        content = item.get("content", item)
        title = content.get("title") or item.get("title", "No title")
        headlines.append(title)
    return {"symbol": ticker, "latest_news": headlines}

# 5. TRADER-ANALYST (Indian Fundamentals)
def get_stock_fundamentals(symbol: str) -> dict:
    """Fetches key fundamental metrics for an Indian NSE stock in INR."""
    ticker = to_nse(symbol)
    info = yf.Ticker(ticker).info or {}
    mcap = info.get("marketCap", 0)
    return {
        "symbol": ticker,
        "name": info.get("shortName", ticker),
        "sector": info.get("sector", "N/A"),
        "market_cap_cr": round(mcap / 1e7, 2) if mcap else "N/A",
        "pe_ratio": info.get("trailingPE", "N/A"),
        "52w_high": info.get("fiftyTwoWeekHigh", "N/A"),
        "52w_low": info.get("fiftyTwoWeekLow", "N/A"),
        "roe": info.get("returnOnEquity", "N/A")
    }

# 6. TRADER-TIMEMACHINE (Simple EMA 9/21 Backtest)
def run_timemachine_backtest(symbol: str, days: int = 30) -> dict:
    """Runs a quick EMA 9/21 crossover backtest on an Indian stock over recent days."""
    ticker = to_nse(symbol)
    df = yf.Ticker(ticker).history(period=f"{days}d", interval="15m")
    if df.empty or len(df) < 30:
        return {"error": f"Insufficient historical data for {ticker}"}

    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()
    df["Signal"] = (df["EMA9"] > df["EMA21"]).astype(int)
    df["Trade"] = df["Signal"].diff()

    buys = df[df["Trade"] == 1]["Close"].tolist()
    sells = df[df["Trade"] == -1]["Close"].tolist()
    trades = min(len(buys), len(sells))
    wins = sum(1 for i in range(trades) if sells[i] > buys[i])
    total_pts = round(sum(sells[i] - buys[i] for i in range(trades)), 2)

    return {
        "symbol": ticker,
        "timeframe": f"{days}d (15m candles)",
        "total_trades": trades,
        "winning_trades": wins,
        "win_rate_pct": round((wins / trades) * 100, 1) if trades > 0 else 0,
        "net_points_inr": total_pts
    }

# 7. TRADER-BROKER (Safe Paper Trade / Order Ticket Generator)
def create_paper_order_ticket(symbol: str, action: str, qty: int, entry_price: float, stop_loss: float, target: float) -> dict:
    """Generates a safe Paper Trade / Zerodha-style MIS Intraday order ticket."""
    return {
        "mode": "PAPER_TRADE_ONLY",
        "timestamp_ist": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exchange": "NSE",
        "symbol": to_nse(symbol).replace(".NS", ""),
        "product": "MIS (Intraday)",
        "action": action.upper(),
        "quantity": qty,
        "order_type": "LIMIT",
        "entry_price_inr": entry_price,
        "stop_loss_inr": stop_loss,
        "target_inr": target
    }
