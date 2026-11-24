#!/usr/bin/env python3
"""Earnings reader for U.S. SEC and Indian NSE listed companies.

Reads reported quarter facts from SEC company facts for U.S. tickers and from
official NSE financial-result XBRL filings for tickers ending in .NS. It lays
out revenue, gross, operating and net margins, net income, diluted EPS, and
growth against the same quarter a year earlier. SEC tickers also include
links to the latest earnings press release and 10-Q or 10-K.

    python3 facts.py AAPL RELIANCE.NS
    python3 facts.py AAPL KO --quarters 8
    python3 facts.py AAPL --json
    python3 facts.py RELIANCE.NS --metrics trader/metrics.json

Sources (U.S. tickers: SEC; a User-Agent with your name and email is required,
from --user-agent, SEC_USER_AGENT or sec_user_agent in trader/config.json):
  https://www.sec.gov/files/company_tickers.json            ticker -> CIK
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
  https://data.sec.gov/submissions/CIK##########.json        filings list
  https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/index.json

Sources (NSE tickers ending in .NS):
  https://www.nseindia.com/api/integrated-filing-results
  https://www.nseindia.com/api/corporates-financial-results

How the quarters are built:
  - A quarter is a reported value covering about three months (80 to 100
    days). When a period was reported more than once, the latest filing wins.
  - SEC Q4 is the 10-K year minus the three 10-Qs. NSE Q4 is derived from the
    annual result minus the nine-month cumulative result when a direct quarter
    is not available. Derived EPS is approximate.
  - Revenue is read from reported XBRL concepts; gross and operating margins
    are blank where the filing does not report a comparable concept.

--metrics FILE merges one row per ticker into a JSON file (TTM revenue and
EPS, margins, growth). Offline: --offline DIR supports SEC data only.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import base64
import datetime as _dt
import gzip
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK%s.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/%d/%s/"
PLACEHOLDER = "you@example.com"
UA_HELP = ("SEC requires a User-Agent with your name and email. Set \"sec_user_agent\": "
           "\"Your Name you@example.com\" in trader/config.json (your real name and email), "
           "or export SEC_USER_AGENT, or pass --user-agent.")
NSE_API_URL = "https://www.nseindia.com/api/integrated-filing-results"
NSE_OLD_API_URL = "https://www.nseindia.com/api/corporates-financial-results"
NSE_HOME = "https://www.nseindia.com/"
NSE_ENV = ("ANGELONE_API_KEY", "ANGELONE_CLIENT_CODE", "ANGELONE_PASSWORD",
           "ANGELONE_TOTP_SECRET", "ANGELONE_CLIENT_PUBLIC_IP")
NSE_MARKET = {"timezone": "IST", "open": "09:15", "close": "15:30"}
SMARTAPI_ROOT = "https://apiconnect.angelone.in"
SMARTAPI_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenuesNetOfInterestExpense", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
}
FLOWS = ("revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income")
NSE_CONCEPTS = {
    "revenue": ["RevenueFromOperations", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "Revenues"],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss", "ProfitFromOperations"],
    "net_income": ["ProfitLossForPeriod", "ProfitLossForPeriodFromContinuingOperations",
                   "ProfitLoss", "NetIncomeLoss"],
    "eps_diluted": ["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "DilutedEarningsLossPerShareFromContinuingOperations",
                    "EarningsPerShareDiluted", "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "BasicEarningsLossPerShareFromContinuingOperations"],
}


class FactsError(Exception):
    pass


def user_agent(cli=None, config="trader/config.json"):
    ua = (cli or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua and config and os.path.isfile(config):
        try:
            with open(config, encoding="utf-8") as fh:
                ua = str(json.load(fh).get("sec_user_agent") or "").strip()
        except (OSError, ValueError):
            ua = ""
    if not ua or "@" not in ua or PLACEHOLDER in ua:
        return None
    return ua


class Fetcher(object):
    def __init__(self, ua=None, offline=None, pause=0.15):
        self.ua, self.offline, self.pause, self._last = ua, offline, pause, 0.0

    def json(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise FactsError("offline: no file %s" % path)
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        if not self.ua:
            raise FactsError(UA_HELP)
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": self.ua,
                                                   "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FactsError("SEC has no data at %s (no XBRL filings for this CIK?)" % url)
            raise FactsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise FactsError("could not reach %s: %s" % (url, exc.reason))
        return json.loads(data.decode("utf-8"))


class NSEFetcher(object):
    """Fetch NSE API records and their linked filing XBRL using a cookie session."""

    def __init__(self, pause=0.2):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies))
        self.pause, self._last, self._warmed = pause, 0.0, False

    def _open(self, url, referer):
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={
            "User-Agent": SMARTAPI_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
        })
        try:
            return self.opener.open(req, timeout=25)
        except urllib.error.HTTPError as exc:
            raise FactsError("NSE returned HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise FactsError("could not reach NSE at %s: %s" % (url, exc.reason))

    def _warm(self):
        if self._warmed:
            return
        try:
            with self._open(NSE_HOME, NSE_HOME) as response:
                response.read()
        except FactsError as exc:
            # NSE can forbid the homepage while still serving API requests.
            if "HTTP 403" not in str(exc):
                raise
        self._warmed = True

    def json(self, url):
        self._warm()
        with self._open(url, NSE_HOME + "companies-listing/corporate-filings-financial-results") as response:
            try:
                return json.loads(response.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FactsError("NSE returned invalid JSON for %s: %s" % (url, exc))

    def text(self, url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in (
                "nsearchives.nseindia.in", "archives.nseindia.com"):
            raise FactsError("refusing non-official NSE XBRL URL: %s" % url)
        self._warm()
        with self._open(url, NSE_HOME + "companies-listing/corporate-filings-financial-results") as response:
            try:
                return response.read().decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise FactsError("NSE filing is not UTF-8 XML at %s: %s" % (url, exc))


# ---------------------------------------------------------------- facts

def _date(s):
    return _dt.date.fromisoformat(s)


def facts_for(companyfacts, concept, unit_hint=None):
    """Raw facts for one us-gaap concept (USD, or USD/shares for EPS)."""
    node = companyfacts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not node:
        return []
    units = node.get("units", {})
    unit = unit_hint if unit_hint in units else next(iter(units), None)
    return units.get(unit, []) if unit else []


def periods(facts):
    """{(start, end): fact} for duration facts from 10-Q/10-K.

    The value comes from the latest filing (restatements win). The fiscal
    label (fy, fp) comes from the first filing that reported the period: a
    later 10-Q that repeats it as a comparison carries its own fy and fp."""
    latest, first = {}, {}
    for f in facts:
        if "start" not in f or f.get("form", "").split("/")[0] not in ("10-Q", "10-K", "20-F", "40-F"):
            continue
        key = (f["start"], f["end"])
        stamp = (f.get("filed", ""), f.get("accn", ""))
        if key not in latest or stamp > (latest[key].get("filed", ""), latest[key].get("accn", "")):
            latest[key] = f
        if key not in first or stamp < (first[key].get("filed", ""), first[key].get("accn", "")):
            first[key] = f
    out = {}
    for key, f in latest.items():
        g = dict(f)
        g["fy"], g["fp"] = first[key].get("fy"), first[key].get("fp")
        g["first_filed"] = first[key].get("filed")
        out[key] = g
    return out


def quarterly(facts, derive_q4=True):
    """{end: {"value", "start", "end", "derived", "fy", "fp", "form"}} three-month values."""
    per = periods(facts)
    q = {}
    years = []
    for (start, end), f in per.items():
        days = (_date(end) - _date(start)).days
        if 80 <= days <= 100:
            q[end] = {"value": f["val"], "start": start, "end": end, "derived": False,
                      "fy": f.get("fy"), "fp": f.get("fp"), "form": f.get("form"),
                      "filed": f.get("filed")}
        elif 350 <= days <= 380:
            years.append((start, end, f))
    if derive_q4:
        for start, end, f in years:
            if end in q:
                continue
            inside = [v for v in q.values() if v["start"] >= start and v["end"] < end]
            inside.sort(key=lambda v: v["end"])
            if len(inside) != 3:
                continue
            last_q_start = _date(inside[-1]["end"]) + _dt.timedelta(days=1)
            q[end] = {"value": f["val"] - sum(v["value"] for v in inside),
                      "start": last_q_start.isoformat(), "end": end, "derived": True,
                      "fy": f.get("fy"), "fp": "Q4", "form": f.get("form"),
                      "filed": f.get("filed")}
    return q


def series(companyfacts, key, derive_q4=True):
    """Quarterly series for a metric, trying its concepts in order and filling gaps."""
    unit = "USD/shares" if key == "eps_diluted" else "USD"
    merged, used = {}, []
    for concept in CONCEPTS[key]:
        q = quarterly(facts_for(companyfacts, concept, unit), derive_q4)
        if not q:
            continue
        added = False
        for end, v in q.items():
            if end not in merged:
                merged[end] = dict(v, concept=concept)
                added = True
        if added:
            used.append(concept)
    return merged, used


def _yoy_match(series_, end, days=365, slack=10):
    target = _date(end) - _dt.timedelta(days=days)
    best = None
    for e in series_:
        gap = abs((_date(e) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, e)
    return series_[best[1]] if best else None


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def analyse(companyfacts, quarters=6, name=None):
    """Every number in the report, as a dict."""
    s = {}
    concepts = {}
    for key in CONCEPTS:
        s[key], concepts[key] = series(companyfacts, key, derive_q4=True)
    rev = s["revenue"]
    if not rev and not s["net_income"]:
        raise FactsError("no quarterly revenue or net income in the XBRL facts")
    ends = sorted(set(rev) | set(s["net_income"]), reverse=True)[:quarters]
    rows = []
    for end in ends:
        r = rev.get(end, {}).get("value")
        gp = s["gross_profit"].get(end, {}).get("value")
        if gp is None and r is not None and end in s["cost_of_revenue"]:
            gp = r - s["cost_of_revenue"][end]["value"]
        oi = s["operating_income"].get(end, {}).get("value")
        ni = s["net_income"].get(end, {}).get("value")
        eps = s["eps_diluted"].get(end, {}).get("value")
        prior = {k: _yoy_match(s[k], end) for k in ("revenue", "net_income", "eps_diluted")}
        base = rev.get(end) or s["net_income"].get(end) or {}
        rows.append({
            "end": end, "start": base.get("start"),
            "fiscal": "%s %s" % (base.get("fp") or "", base.get("fy") or ""),
            "derived": any(s[k].get(end, {}).get("derived") for k in ("revenue", "net_income")),
            "revenue": r, "gross_profit": gp, "operating_income": oi, "net_income": ni,
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gp, r), "operating_margin": margin(oi, r),
            "net_margin": margin(ni, r),
            "revenue_yoy": pct(r, (prior["revenue"] or {}).get("value")),
            "net_income_yoy": pct(ni, (prior["net_income"] or {}).get("value")),
            "eps_yoy": pct(eps, (prior["eps_diluted"] or {}).get("value")),
            "filed": base.get("filed"), "form": base.get("form"),
        })
    ttm = {}
    for key in ("revenue", "net_income", "eps_diluted"):
        last4 = sorted(s[key])[-4:]
        if len(last4) == 4 and (_date(last4[-1]) - _date(last4[0])).days < 300:
            ttm[key] = sum(s[key][e]["value"] for e in last4)
    if "eps_diluted" in ttm:
        ttm["eps_diluted"] = round(ttm["eps_diluted"], 2)
    return {"entity": name or companyfacts.get("entityName"),
            "cik": str(companyfacts.get("cik", "")).zfill(10),
            "currency": "USD", "currency_symbol": "$",
            "source": "SEC XBRL company facts",
            "rows": rows, "ttm": ttm, "concepts": concepts}


def _nse_date(value):
    value = str(value or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return _date(value[:10]).isoformat()
    except ValueError:
        return None


def _nse_records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        records = payload.get("data") or payload.get("results") or []
        return records if isinstance(records, list) else []
    return []


def _nse_basis(value):
    value = str(value or "").strip().lower()
    if value in ("consolidated", "consolidated financials", "true", "1"):
        return "Consolidated"
    if value in ("standalone", "non-consolidated", "non consolidated", "false", "0"):
        return "Standalone"
    return "Unknown"


def _nse_stamp(value):
    value = str(value or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(value, fmt).timestamp()
        except ValueError:
            pass
    return 0


def nse_filings(symbol, fetcher, quarters=6):
    """Fetch recent financial-result XBRL filings from NSE's official APIs."""
    params = {"index": "equities", "symbol": symbol,
              "type": "Integrated Filing- Financials", "size": "500"}
    urls = [
        ("integrated", NSE_API_URL + "?" + urllib.parse.urlencode(params)),
        ("quarterly", NSE_OLD_API_URL + "?" + urllib.parse.urlencode(
            {"index": "equities", "symbol": symbol, "period": "Quarterly"})),
    ]
    records, errors = [], []
    for source, url in urls:
        try:
            payload = fetcher.json(url)
        except FactsError as exc:
            errors.append((source, exc))
            continue
        for item in _nse_records(payload):
            end = _nse_date(item.get("qe_Date") or item.get("toDate") or item.get("to_date"))
            xbrl = item.get("xbrl") or item.get("XBRL")
            if not end or not xbrl:
                continue
            records.append({
                "end": end,
                "basis": _nse_basis(item.get("consolidated")),
                "url": str(xbrl),
                "filed": (item.get("broadcast_Date") or item.get("broadcastDate")
                          or item.get("filingDate") or item.get("filing_date") or ""),
                "company": (item.get("companyName") or item.get("company_name")
                            or item.get("symbol") or symbol),
                "financial_year": item.get("financialYear") or item.get("financial_year") or "",
                "relating_to": (item.get("relatingTo") or item.get("type_Sub")
                                or item.get("period") or ""),
                "source": source,
            })
    if not records:
        if errors:
            raise FactsError("; ".join("%s feed: %s" % error for error in errors))
        raise FactsError("NSE returned no XBRL financial-result filings for %s" % symbol)

    latest = {}
    for filing in records:
        key = (filing["end"], filing["basis"])
        if key not in latest or _nse_stamp(filing["filed"]) >= _nse_stamp(latest[key]["filed"]):
            latest[key] = filing
    records = list(latest.values())
    bases = {r["basis"] for r in records}
    selected_basis = ("Consolidated" if "Consolidated" in bases else
                      "Standalone" if "Standalone" in bases else "Unknown")
    records = [r for r in records if r["basis"] == selected_basis]
    records.sort(key=lambda r: (r["end"], _nse_stamp(r["filed"])), reverse=True)
    records = records[:max(quarters + 6, 12)]
    if not records:
        raise FactsError("NSE returned no usable financial-result filings for %s" % symbol)
    source_names = {"integrated": "Integrated filing", "quarterly": "quarterly results"}
    warnings = ["%s feed was unavailable: %s" % (source_names[source], error)
                for source, error in errors]
    return records, selected_basis, warnings


def _xml_local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _nse_fiscal(end, filing):
    end_date = _date(end)
    if end == filing.get("end"):
        label = str(filing.get("relating_to") or "").lower()
        for word, q in (("first", "Q1"), ("second", "Q2"),
                        ("third", "Q3"), ("fourth", "Q4")):
            if word in label:
                fy = re.search(r"(\d{4})\s*$", str(filing.get("financial_year") or ""))
                if fy:
                    return q, fy.group(1)
                break
    quarter = {6: "Q1", 9: "Q2", 12: "Q3", 3: "Q4"}.get(end_date.month)
    fiscal_year = end_date.year + (1 if end_date.month > 3 else 0)
    return quarter or "Q%d" % ((end_date.month - 1) // 3 + 1), str(fiscal_year)


def parse_nse_xbrl(xml_text, filing):
    """Extract dimension-free duration facts and their INR units from an NSE filing."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FactsError("invalid XBRL XML at %s: %s" % (filing["url"], exc))

    contexts = {}
    units = {}
    for node in root.iter():
        local = _xml_local(node.tag)
        if local == "context":
            dates = {}
            dimensional = False
            for child in node.iter():
                child_name = _xml_local(child.tag)
                if child_name in ("startdate", "enddate"):
                    dates[child_name] = _nse_date(child.text)
                elif child_name in ("explicitmember", "typedmember"):
                    dimensional = True
            if not dimensional and dates.get("startdate") and dates.get("enddate"):
                contexts[node.attrib.get("id", "")] = (
                    dates["startdate"], dates["enddate"])
        elif local == "unit":
            measures = [child.text.strip() for child in node.iter()
                        if _xml_local(child.tag) == "measure" and child.text]
            if measures:
                unit_id = node.attrib.get("id", "")
                if "pershare" in unit_id.lower() or (
                        len(measures) == 2 and measures[1].split(":")[-1].lower() == "shares"):
                    units[unit_id] = "INRPerShare"
                else:
                    units[unit_id] = measures[-1].split(":")[-1]

    facts = {}
    for node in root.iter():
        context_id = node.attrib.get("contextRef") or node.attrib.get("contextref")
        unit_id = node.attrib.get("unitRef") or node.attrib.get("unitref")
        context = contexts.get(context_id or "")
        unit = units.get(unit_id or "")
        concept = node.attrib.get("name") or node.tag.rsplit("}", 1)[-1]
        text = "".join(node.itertext()).strip()
        if not context or not unit or not text:
            continue
        if unit.lower() not in ("inr", "inrpershare"):
            continue
        try:
            value = float(text.replace(",", ""))
        except ValueError:
            continue
        scale = node.attrib.get("scale")
        if scale:
            try:
                value *= 10 ** int(scale)
            except ValueError:
                raise FactsError("invalid XBRL scale for %s in %s" % (concept, filing["url"]))
        if node.attrib.get("sign") == "-":
            value = -abs(value)
        start, end = context
        fp, fy = _nse_fiscal(end, filing)
        fact = {"start": start, "end": end, "val": value, "unit": unit,
                "filed": filing.get("filed", ""), "accn": filing["url"],
                "form": "10-K" if (_date(end) - _date(start)).days >= 350 else "10-Q",
                "fp": fp, "fy": fy, "url": filing["url"]}
        facts.setdefault(concept, []).append(fact)
    return facts


def nse_quarterly(facts, derive_q4=True):
    """Return quarter values, deriving Q4 from annual less YTD when necessary."""
    by_period = {}
    for fact in facts:
        days = (_date(fact["end"]) - _date(fact["start"])).days
        if not 80 <= days <= 380:
            continue
        key = (fact["start"], fact["end"])
        prior = by_period.get(key)
        if prior is None or (_nse_stamp(fact.get("filed")) >=
                             _nse_stamp(prior.get("filed"))):
            by_period[key] = fact

    quarters, annual, ytd = {}, [], []
    for (start, end), fact in by_period.items():
        days = (_date(end) - _date(start)).days
        if 80 <= days <= 100:
            quarters[end] = dict(fact, derived=False)
        elif 250 <= days <= 290:
            ytd.append(fact)
        elif 350 <= days <= 380:
            annual.append(fact)

    if derive_q4:
        for year in annual:
            end, start = year["end"], year["start"]
            if end in quarters:
                continue
            prior_ytd = [f for f in ytd if f["start"] == start and f["end"] < end]
            if prior_ytd:
                base = max(prior_ytd, key=lambda f: f["end"])
                if (_date(end) - _date(base["end"])).days <= 120:
                    value = year["val"] - base["val"]
                else:
                    base = None
                    value = None
            else:
                base, value = None, None
            if base is None:
                inside = [f for f in quarters.values()
                          if f["start"] >= start and f["end"] < end]
                inside.sort(key=lambda f: f["end"])
                if len(inside) == 3:
                    value = year["val"] - sum(f["val"] for f in inside)
            if value is not None:
                fp, fy = _nse_fiscal(end, year)
                quarter_start = (_date(base["end"]) + _dt.timedelta(days=1)).isoformat() \
                    if base else (_date(inside[-1]["end"]) + _dt.timedelta(days=1)).isoformat()
                quarters[end] = dict(year, val=value, start=quarter_start,
                    form="10-K", fp=fp, fy=fy, derived=True)
    return {end: {"value": f["val"], "start": f["start"], "end": end,
                  "derived": f["derived"], "fy": f.get("fy"), "fp": f.get("fp"),
                  "form": f.get("form"), "filed": f.get("filed"), "url": f.get("url")}
            for end, f in quarters.items()}


def nse_series(all_facts, key):
    unit = "INRPerShare" if key == "eps_diluted" else "INR"
    merged, used = {}, []
    for concept in NSE_CONCEPTS[key]:
        facts = [f for f in all_facts.get(concept, []) if f["unit"] == unit]
        q = nse_quarterly(facts)
        added = False
        for end, value in q.items():
            if end not in merged:
                merged[end] = dict(value, concept=concept)
                added = True
        if added:
            used.append(concept)
    return merged, used


def analyse_nse(symbol, quarters, filings, basis, fetcher, warnings=None,
                quote=None, quote_note=None):
    all_facts = {}
    for filing in filings:
        parsed = parse_nse_xbrl(fetcher.text(filing["url"]), filing)
        for concept, facts in parsed.items():
            all_facts.setdefault(concept, []).extend(facts)
    if not all_facts:
        raise FactsError("NSE filings contained no supported INR XBRL facts for %s" % symbol)

    series_, concepts = {}, {}
    for key in NSE_CONCEPTS:
        series_[key], concepts[key] = nse_series(all_facts, key)
    rev, ni = series_["revenue"], series_["net_income"]
    if not rev and not ni:
        raise FactsError("no quarterly revenue or net income in NSE XBRL for %s" % symbol)
    ends = sorted(set(rev) | set(ni), reverse=True)[:quarters]
    rows = []
    for end in ends:
        r = rev.get(end, {}).get("value")
        gp = series_["gross_profit"].get(end, {}).get("value")
        if gp is None and r is not None and end in series_["cost_of_revenue"]:
            gp = r - series_["cost_of_revenue"][end]["value"]
        oi = series_["operating_income"].get(end, {}).get("value")
        net = ni.get(end, {}).get("value")
        eps = series_["eps_diluted"].get(end, {}).get("value")
        prior = {k: _yoy_match(series_[k], end)
                 for k in ("revenue", "net_income", "eps_diluted")}
        base = rev.get(end) or ni.get(end) or {}
        rows.append({
            "end": end, "start": base.get("start"),
            "fiscal": "%s %s" % (base.get("fp") or "", base.get("fy") or ""),
            "derived": any(series_[k].get(end, {}).get("derived")
                           for k in ("revenue", "net_income")),
            "revenue": r, "gross_profit": gp, "operating_income": oi,
            "net_income": net, "eps_diluted": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gp, r), "operating_margin": margin(oi, r),
            "net_margin": margin(net, r),
            "revenue_yoy": pct(r, (prior["revenue"] or {}).get("value")),
            "net_income_yoy": pct(net, (prior["net_income"] or {}).get("value")),
            "eps_yoy": pct(eps, (prior["eps_diluted"] or {}).get("value")),
            "filed": base.get("filed"), "form": base.get("form"), "url": base.get("url"),
        })
    ttm = {}
    for key in ("revenue", "net_income", "eps_diluted"):
        last4 = sorted(series_[key])[-4:]
        if len(last4) == 4 and (_date(last4[-1]) - _date(last4[0])).days < 300:
            ttm[key] = sum(series_[key][end]["value"] for end in last4)
    if "eps_diluted" in ttm:
        ttm["eps_diluted"] = round(ttm["eps_diluted"], 2)

    latest = filings[0]
    result = {
        "entity": latest.get("company") or symbol, "cik": None,
        "currency": "INR", "currency_symbol": "₹",
        "source": "official NSE XBRL financial filings",
        "market_hours": dict(NSE_MARKET),
        "basis": basis, "rows": rows, "ttm": ttm, "concepts": concepts,
        "filing": {"filed": latest.get("filed"), "period_end": latest["end"],
                   "basis": basis, "url": latest["url"]},
        "warnings": warnings or [], "quote": quote, "quote_note": quote_note,
    }
    return result


def _totp(secret, timestamp=None):
    secret = re.sub(r"\s+", "", secret).upper()
    try:
        key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    except (ValueError, base64.binascii.Error) as exc:
        raise FactsError("ANGELONE_TOTP_SECRET must be a Base32 secret: %s" % exc)
    counter = int((timestamp if timestamp is not None else time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return "%06d" % code


def _smartapi_post(path, payload, headers):
    req = urllib.request.Request(
        SMARTAPI_ROOT + path, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FactsError("Angel One SmartAPI returned HTTP %s for a read-only quote request"
                         % exc.code)
    except urllib.error.URLError as exc:
        raise FactsError("could not reach Angel One SmartAPI: %s" % exc.reason)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactsError("Angel One SmartAPI returned invalid JSON: %s" % exc)
    if not isinstance(data, dict) or data.get("status") is not True:
        message = data.get("message", "request failed") if isinstance(data, dict) else "invalid response"
        raise FactsError("Angel One SmartAPI request failed: %s" % message)
    return data.get("data")


def smartapi_quote(ticker):
    missing = [name for name in NSE_ENV if not os.environ.get(name)]
    if missing:
        if len(missing) == len(NSE_ENV):
            return None, ("SmartAPI quote not fetched; set " + ", ".join(NSE_ENV) +
                          " in the local environment to enable it.")
        raise FactsError("SmartAPI quote configuration is incomplete; missing " +
                         ", ".join(missing))

    local_ip = os.environ.get("ANGELONE_CLIENT_LOCAL_IP")
    if not local_ip:
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            local_ip = "127.0.0.1"
    mac = os.environ.get("ANGELONE_MAC_ADDRESS")
    if not mac:
        mac = ":".join("%02x" % (uuid.getnode() >> shift & 0xff)
                       for shift in range(40, -1, -8))
    api_key = os.environ["ANGELONE_API_KEY"]
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "X-PrivateKey": api_key, "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": local_ip,
        "X-ClientPublicIP": os.environ["ANGELONE_CLIENT_PUBLIC_IP"],
        "X-MACAddress": mac,
    }
    login = _smartapi_post(
        "/rest/auth/angelbroking/user/v1/loginByPassword",
        {"clientcode": os.environ["ANGELONE_CLIENT_CODE"],
         "password": os.environ["ANGELONE_PASSWORD"],
         "totp": _totp(os.environ["ANGELONE_TOTP_SECRET"])}, headers)
    token = (login or {}).get("jwtToken")
    if not token:
        raise FactsError("Angel One SmartAPI login returned no access token")
    auth_headers = dict(headers, Authorization="Bearer " + token)
    symbol = ticker[:-3]
    searched = _smartapi_post(
        "/rest/secure/angelbroking/order/v1/searchScrip",
        {"exchange": "NSE", "searchscrip": symbol}, auth_headers)
    candidates = searched if isinstance(searched, list) else []
    scrip = next((item for item in candidates
                  if item.get("tradingsymbol") == symbol + "-EQ"), None)
    if not scrip or not scrip.get("symboltoken"):
        raise FactsError("Angel One SmartAPI found no NSE cash-market scrip for %s" % symbol)
    quoted = _smartapi_post(
        "/rest/secure/angelbroking/market/v1/quote",
        {"mode": "LTP", "exchangeTokens": {"NSE": [str(scrip["symboltoken"])]}},
        auth_headers)
    fetched = (quoted or {}).get("fetched", []) if isinstance(quoted, dict) else []
    row = next((item for item in fetched
                if item.get("tradingSymbol") == symbol + "-EQ"), None)
    if not row or row.get("ltp") is None:
        raise FactsError("Angel One SmartAPI returned no LTP for %s" % symbol)
    try:
        price = float(row["ltp"])
    except (TypeError, ValueError):
        raise FactsError("Angel One SmartAPI returned a non-numeric LTP for %s" % symbol)
    return {"price": price, "currency": "INR",
            "as_of": row.get("exchangeFeedTime") or row.get("exchFeedTime"),
            "source": "Angel One SmartAPI"}, None


def stale_note(result, filings):
    """A note when SEC's facts lag a report that is already filed."""
    rep = (filings or {}).get("report")
    if not rep or not result["rows"] or not rep.get("period"):
        return None
    latest = result["rows"][0]["end"]
    if rep["period"] > latest and (_date(rep["period"]) - _date(latest)).days > 20:
        return ("SEC's XBRL facts stop at %s, but a %s for the period ending %s was filed %s. "
                "Read it directly: %s" % (latest, rep["form"], rep["period"], rep["filed"],
                                         rep["url"]))
    return None


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    symbol = result.get("currency_symbol", "$")
    out = []

    def money(v):
        if v is None:
            return "n/a"
        return (symbol + "%.2fB" % (v / 1e9) if abs(v) >= 1e9
                else symbol + "%.1fM" % (v / 1e6))

    derived_label = (" (derived from the annual filing)" if result.get("currency") == "INR"
                     else " (derived from the 10-K)")
    label = "Quarter ending %s%s" % (cur["end"], derived_label if cur["derived"] else "")
    line = "%s: revenue %s" % (label, money(cur["revenue"]))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    line += ", net income %s" % money(cur["net_income"])
    if cur["net_income_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_income_yoy"]
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS %s%.2f" % (symbol, cur["eps_diluted"])
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur["net_income"] is not None and cur["operating_income"] is not None \
            and cur["net_income"] > cur["operating_income"]:
        out.append("Net income is above operating income: it includes gains outside the "
                   "business (investments, tax items). Check the 10-Q before reading it as growth.")
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append("Revenue %+.1f%% vs the quarter before (many businesses are seasonal: "
                   "compare with a year earlier first)." % pct(cur["revenue"], prev["revenue"]))
    return out


# ---------------------------------------------------------------- filings

EXHIBIT_RANK = [r"ex(?:hibit)?[-_]?99[-_.]?0?1(?!\d)", r"ex(?:hibit)?[-_]?99",
                r"earnings|release|press", r"ex(?:hibit)?[-_]?\d"]


def exhibit_name(names, primary=None):
    """The press release among a filing's files (EX-99.1 by its usual names)."""
    docs = [n for n in names if n.lower().endswith((".htm", ".html", ".txt", ".pdf"))
            and n != primary and not re.match(r"^(R\d+\.htm|\d{10}-\d{2}-\d{6})", n)]
    for rx in EXHIBIT_RANK:
        hits = sorted(n for n in docs if re.search(rx, n, re.I))
        if hits:
            return hits[0]
    return None


def latest_filings(submissions, cik, fetcher=None):
    """Newest 10-Q/10-K and newest earnings 8-K (item 2.02) with its EX-99 exhibit."""
    rec = submissions.get("filings", {}).get("recent", {})
    out = {"report": None, "earnings_8k": None}
    base_cik = int(cik)
    for i, form in enumerate(rec.get("form", [])):
        acc = rec["accessionNumber"][i]
        folder = ARCHIVE_URL % (base_cik, acc.replace("-", ""))
        if out["report"] is None and form in ("10-Q", "10-K", "20-F", "40-F"):
            out["report"] = {"form": form, "filed": rec["filingDate"][i],
                             "period": rec["reportDate"][i], "accession": acc,
                             "url": folder + rec["primaryDocument"][i]}
        if out["earnings_8k"] is None and form == "8-K" and "2.02" in (rec.get("items", [""] * (i + 1))[i] or ""):
            e = {"form": form, "filed": rec["filingDate"][i], "accession": acc,
                 "url": folder + rec["primaryDocument"][i], "exhibit": None,
                 "index": folder + "%s-index.htm" % acc}
            if fetcher is not None:
                try:
                    idx = fetcher.json(folder + "index.json", os.path.join(acc.replace("-", ""), "index.json"))
                    names = [it["name"] for it in idx.get("directory", {}).get("item", [])]
                    pick = exhibit_name(names, rec["primaryDocument"][i])
                    if pick:
                        e["exhibit"] = folder + pick
                except FactsError:
                    pass
            out["earnings_8k"] = e
        if out["report"] and out["earnings_8k"]:
            break
    return out


# ---------------------------------------------------------------- output

def _m(v):
    if v is None:
        return "-"
    return "{:,.0f}".format(v / 1e6)


def _p(v):
    return "-" if v is None else "%+.1f%%" % v if v < 0 or v > 0 else "0.0%"


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def render(ticker, result, filings):
    symbol = result.get("currency_symbol", "$")
    if result.get("currency") == "INR":
        out = ["%s (%s), INR (₹) millions except EPS (₹/share). Source: %s; %s basis." % (
            result["entity"], ticker, result["source"], result["basis"]), ""]
    else:
        out = ["%s (%s), CIK %s. US$ millions except EPS. Source: %s." % (
            result["entity"], ticker, result["cik"], result["source"]), ""]
    out.append("%-11s %-9s %10s %8s %7s %7s %7s %10s %8s %7s" % (
        "quarter end", "fiscal", "revenue", "rev yoy", "gross", "oper", "net",
        "net inc", "EPS dil", "EPS yoy"))
    for r in result["rows"]:
        out.append("%-11s %-9s %10s %8s %7s %7s %7s %10s %8s %7s%s" % (
            r["end"], r["fiscal"].strip()[:9], _m(r["revenue"]), _p(r["revenue_yoy"]),
            _pm(r["gross_margin"]), _pm(r["operating_margin"]), _pm(r["net_margin"]),
            _m(r["net_income"]), "-" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"],
            _p(r["eps_yoy"]), "  derived" if r["derived"] else ""))
    t = result["ttm"]
    if t:
        out.append("")
        out.append("Last 4 quarters: revenue %s, net income %s, diluted EPS %s." % (
            symbol + _m(t.get("revenue")) + "M" if t.get("revenue") is not None else "-",
            symbol + _m(t.get("net_income")) + "M" if t.get("net_income") is not None else "-",
            symbol + "%.2f" % t["eps_diluted"] if t.get("eps_diluted") is not None else "-"))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    if result.get("currency") == "INR":
        filing = result.get("filing")
        hours = result.get("market_hours", NSE_MARKET)
        out.append("NSE cash session: %s-%s %s (exchange holidays apply)." % (
            hours["open"], hours["close"], hours["timezone"]))
        if filing:
            out.append("Latest NSE financial result: period %s, broadcast %s  %s" % (
                filing["period_end"], filing.get("filed") or "date not supplied",
                filing["url"]))
        if result.get("quote"):
            quote = result["quote"]
            out.append("Angel One SmartAPI quote: ₹%.2f%s." % (
                quote["price"], " at " + quote["as_of"] if quote.get("as_of") else ""))
        if result.get("quote_note"):
            out.append(result["quote_note"])
        for warning in result.get("warnings", []):
            out.append("NOTE: " + warning)
    elif filings:
        out.append("")
        rep, e8 = filings.get("report"), filings.get("earnings_8k")
        if rep:
            out.append("Latest %s: period %s, filed %s  %s" % (rep["form"], rep["period"],
                                                              rep["filed"], rep["url"]))
        if e8:
            out.append("Latest earnings 8-K (item 2.02): filed %s  %s" % (
                e8["filed"], e8["exhibit"] or e8["index"]))
    note = stale_note(result, filings) if result.get("currency") != "INR" else None
    if note:
        out.append("")
        out.append("NOTE: " + note)
    if any(r["derived"] for r in result["rows"]):
        out.append("")
        if result.get("currency") == "INR":
            out.append("Derived quarters subtract the nine-month cumulative filing from annual "
                       "results; derived EPS is approximate.")
        else:
            out.append("Derived quarters are the 10-K year minus the three 10-Qs; derived EPS is "
                       "approximate.")
    return "\n".join(out) + "\n"


def metrics_row(ticker, result):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"ticker": ticker, "name": result["entity"], "cik": result["cik"],
            "currency": result.get("currency", "USD"),
            "quote": result.get("quote"),
            "period_end": cur.get("end"), "revenue_ttm": t.get("revenue"),
            "net_income_ttm": t.get("net_income"), "eps_ttm": t.get("eps_diluted"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "net_income_growth_yoy": cur.get("net_income_yoy"),
            "gross_margin": cur.get("gross_margin"), "operating_margin": cur.get("operating_margin"),
            "net_margin": cur.get("net_margin"), "source": result.get("source")}


def clean_title(title):
    return re.sub(r"\s*/[A-Z]{2,5}/?\s*$", "", title or "").strip() or None


def ticker_ciks(fetcher):
    """ticker -> (10-digit CIK, company name) from company_tickers.json."""
    data = fetcher.json(TICKERS_URL, "company_tickers.json")
    out = {}
    for v in data.values():
        out.setdefault(v["ticker"].upper().replace("-", "."),
                       (str(v["cik_str"]).zfill(10), clean_title(v.get("title"))))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tickers", nargs="*", help="tickers, e.g. AAPL KO RELIANCE.NS")
    ap.add_argument("--cik", help="use this CIK instead of a ticker lookup (one company)")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--no-filings", action="store_true",
                    help="skip the SEC 8-K and 10-Q links")
    ap.add_argument("--no-quote", action="store_true",
                    help="skip the SmartAPI quote for NSE tickers")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per ticker into this JSON")
    ap.add_argument("--user-agent")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.tickers and not args.cik:
        ap.print_help()
        return 2
    if args.quarters < 1:
        ap.error("--quarters must be at least 1")
    wanted = args.tickers or [args.cik]
    use_sec = bool(args.cik) or any(not t.upper().endswith(".NS") for t in wanted)
    if args.offline and any(t.upper().endswith(".NS") for t in wanted) and not args.cik:
        print("facts: NSE filings require a live connection; --offline supports SEC data only",
              file=sys.stderr)
        return 2
    ua = user_agent(args.user_agent, args.config)
    if use_sec and not ua and not args.offline:
        print("facts: " + UA_HELP, file=sys.stderr)
        return 2
    fetcher = Fetcher(ua, offline=args.offline)
    nse_fetcher = NSEFetcher() if any(
        t.upper().endswith(".NS") for t in wanted) and not args.cik else None
    results, rows, failed = [], [], 0
    ciks = {}
    if use_sec and not args.cik:
        try:
            ciks = ticker_ciks(fetcher)
        except FactsError as exc:
            print("facts: %s" % exc, file=sys.stderr)
            return 2

    for t in wanted:
        tick = t.upper()
        try:
            if tick.endswith(".NS") and not args.cik:
                symbol = tick[:-3]
                filing_records, basis, warnings = nse_filings(
                    symbol, nse_fetcher, quarters=args.quarters)
                quote, quote_note = (None, "SmartAPI quote skipped with --no-quote.") \
                    if args.no_quote else smartapi_quote(tick)
                result = analyse_nse(
                    symbol, args.quarters, filing_records, basis, nse_fetcher,
                    warnings=warnings, quote=quote, quote_note=quote_note)
                filings = {"source": "NSE", "latest": result["filing"]}
            else:
                if args.cik:
                    cik, name = str(args.cik).zfill(10), None
                else:
                    cik, name = ciks.get(tick.replace("-", "."), (None, None))
                if not cik:
                    raise FactsError(
                        "no SEC CIK for %s (is it a US filer? ETFs and foreign filers "
                        "may not have XBRL facts; use the .NS suffix for NSE stocks)" % tick)
                cf = fetcher.json(FACTS_URL % cik, "companyfacts-CIK%s.json" % cik)
                result = analyse(cf, quarters=args.quarters, name=name)
                filings = None
                if not args.no_filings:
                    sub = fetcher.json(SUBMISSIONS_URL % cik, "submissions-CIK%s.json" % cik)
                    filings = latest_filings(sub, cik, fetcher)
        except FactsError as exc:
            print("facts: %s: %s" % (tick, exc), file=sys.stderr)
            failed += 1
            continue
        note = stale_note(result, filings) if result.get("currency") != "INR" else None
        results.append({"ticker": tick, "result": result, "filings": filings,
                        "changes": changes(result), "note": note})
        rows.append(metrics_row(tick, result))
        if not args.json:
            sys.stdout.write(render(tick, result, filings))
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps(results, indent=2))
    if args.metrics and rows:
        existing = {}
        if os.path.isfile(args.metrics):
            with open(args.metrics, encoding="utf-8") as fh:
                existing = {r["ticker"]: r for r in json.load(fh).get("companies", [])}
        for r in rows:
            existing[r["ticker"]] = r
        folder = os.path.dirname(args.metrics)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump({"as_of": _dt.date.today().isoformat(),
                       "companies": sorted(existing.values(), key=lambda r: r["ticker"])},
                      fh, indent=2)
            fh.write("\n")
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
