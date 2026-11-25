"""NSE public equity quote lookup with a quiet yfinance fallback."""

import datetime
import http.cookiejar
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import yfinance as yf


NSE_HOME = "https://www.nseindia.com/"
NSE_QUOTE = "https://www.nseindia.com/api/quote-equity?symbol={}"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CACHE_SECONDS = 20
MIN_REQUEST_GAP = 1.0
SESSION_SECONDS = 15 * 60

_lock = threading.Lock()
_opener = None
_session_at = 0.0
_last_request_at = 0.0
_cache = {}


def normalize_ticker(value):
    ticker = re.sub(r"\s+", "", str(value or "")).upper()
    while ticker.endswith(".NS"):
        ticker = ticker[:-3]
    return ticker if ticker and re.fullmatch(r"[A-Z0-9&.-]{1,30}", ticker) else ""


def _headers(referer=NSE_HOME, accept="application/json, text/plain, */*"):
    return {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Origin": "https://www.nseindia.com",
        "Connection": "keep-alive",
    }


def _warm_session(timeout):
    global _opener, _session_at
    _opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    request = urllib.request.Request(
        NSE_HOME,
        headers=_headers(accept="text/html,application/xhtml+xml"),
    )
    with _opener.open(request, timeout=timeout) as response:
        response.read(1024)
    _session_at = time.monotonic()


def _nse_quote(symbol, timeout):
    global _last_request_at
    with _lock:
        now = time.monotonic()
        if _opener is None or now - _session_at >= SESSION_SECONDS:
            _warm_session(timeout)
        wait = MIN_REQUEST_GAP - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        url = NSE_QUOTE.format(urllib.parse.quote(symbol, safe=""))
        request = urllib.request.Request(url, headers=_headers())
        _last_request_at = time.monotonic()
        try:
            with _opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403):
                raise
            _warm_session(timeout)
            request = urllib.request.Request(url, headers=_headers())
            _last_request_at = time.monotonic()
            with _opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        return payload


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def parse_nse_quote(payload, symbol):
    price_info = payload.get("priceInfo") or {}
    day_range = price_info.get("intraDayHighLow") or {}
    trade_info = (payload.get("marketDeptOrderBook") or {}).get("tradeInfo") or {}
    security_dp = payload.get("securityWiseDP") or {}
    price = _number(price_info.get("lastPrice"))
    if price is None:
        return None
    previous_close = _number(price_info.get("previousClose"))
    change_pct = _number(price_info.get("pChange"))
    if change_pct is None and previous_close:
        change_pct = (price / previous_close - 1) * 100
    volume = _number(
        trade_info.get("totalTradedVolume")
        or security_dp.get("quantityTraded")
        or price_info.get("totalTradedVolume")
    )
    return {
        "symbol": symbol,
        "price": price,
        "day_high": _number(day_range.get("max") or price_info.get("dayHigh")),
        "day_low": _number(day_range.get("min") or price_info.get("dayLow")),
        "change_pct": change_pct,
        "volume": int(volume) if volume is not None else None,
        "source": "Live: NSE",
        "as_of": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
    }


def get_nse_quote(ticker, timeout=6):
    """Fetch one public NSE quote; return None on blocks, timeouts, or missing data."""
    symbol = normalize_ticker(ticker)
    if not symbol:
        return None
    now = time.monotonic()
    cached = _cache.get(symbol)
    if cached and now - cached[0] < CACHE_SECONDS:
        return dict(cached[1])
    try:
        quote = parse_nse_quote(_nse_quote(symbol, timeout), symbol)
    except (OSError, ValueError, TypeError, urllib.error.URLError, TimeoutError):
        quote = None
    except Exception:
        quote = None
    if quote:
        _cache[symbol] = (time.monotonic(), quote)
        return dict(quote)
    return None


def get_yahoo_quote(ticker):
    """Return a delayed daily quote from Yahoo Finance if it is available."""
    symbol = normalize_ticker(ticker)
    if not symbol:
        return None
    try:
        frame = yf.Ticker(symbol + ".NS").history(
            period="5d", interval="1d", auto_adjust=False, prepost=False
        )
        if frame.empty:
            return None
        if getattr(frame.columns, "nlevels", 1) > 1:
            frame.columns = frame.columns.get_level_values(0)
        frame = frame.dropna(subset=["Close"])
        if frame.empty:
            return None
        latest = frame.iloc[-1]
        price = _number(latest.get("Close"))
        if price is None:
            return None
        previous_close = _number(frame["Close"].iloc[-2]) if len(frame) > 1 else None
        return {
            "symbol": symbol,
            "price": price,
            "day_high": _number(latest.get("High")),
            "day_low": _number(latest.get("Low")),
            "change_pct": (
                (price / previous_close - 1) * 100 if previous_close else None
            ),
            "volume": int(_number(latest.get("Volume")) or 0),
            "source": "Fallback: Yahoo",
            "as_of": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        }
    except Exception:
        return None


def get_live_quote(ticker, timeout=6):
    """NSE first; fall back to Yahoo without surfacing NSE blocking errors."""
    quote = get_nse_quote(ticker, timeout=timeout)
    return quote if quote is not None else get_yahoo_quote(ticker)
