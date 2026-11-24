#!/usr/bin/env python3
"""NSE promoter/insider and bulk/block-deal tracker for trader-pelosi.

The existing index/new/fetch/parse commands are retained. The live data source
is NSE's latest bulk/block-deal snapshot; `--offline` accepts a saved JSON
snapshot. A last-successful snapshot is used if NSE blocks a request.
"""

import argparse
import datetime as _dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%d%s.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/%d/%s.pdf"
FD_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%d/%s.pdf"
NSE_DEALS_URL = "https://www.nseindia.com/api/snapshot-capital-market-largedeal?index=all"
NSE_DEALS_PAGE = "https://www.nseindia.com/market-data/large-deals"
NSE_CACHE = "trader/state/nse-deals-cache.json"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
LAST_INDEX_NOTE = None
FILING_TYPES = {
    "P": "Periodic Transaction Report", "O": "Annual report", "A": "Amendment",
    "C": "Candidate report", "X": "Extension", "T": "Termination report",
    "W": "Waiver", "D": "Disclosure (new member or candidate)", "H": "New filer report",
}
TXN_TYPES = {"P": "purchase", "S": "sale", "S (partial)": "partial sale", "E": "exchange"}
OWNERS = {"SP": "spouse", "JT": "joint", "DC": "dependent child", "": "self"}
LATE_DAYS = 45


class PtrError(Exception):
    pass


# ---------------------------------------------------------------- the index

def nse_ticker(ticker):
    ticker = str(ticker or "").strip().upper()
    return ticker if not ticker or ticker.endswith(".NS") else ticker + ".NS"

def iso(mdy):
    """'8/21/2026' or '08/21/2026' -> '2026-08-21' (two-digit years -> 20xx)."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", mdy or "")
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def download(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise PtrError("HTTP %s for %s" % (exc.code, url))
    except urllib.error.URLError as exc:
        raise PtrError("could not reach %s: %s" % (url, exc.reason))


def read_index_bytes(data):
    """Index XML bytes from the ZIP (or the XML itself)."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if not names:
                raise PtrError("no XML inside the ZIP (files: %s)" % ", ".join(z.namelist()))
            return z.read(names[0])
    return data


def parse_index(xml_bytes, year):
    """Every filing in a <YEAR>FD.xml as a dict, newest first."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise PtrError("index is not valid XML: %s" % exc)
    out = []
    for m in root.iter("Member"):
        f = {c.tag: (c.text or "").strip() for c in m}
        doc = f.get("DocID", "")
        kind = f.get("FilingType", "")
        y = int(f.get("Year") or year)
        url = (PTR_URL if kind == "P" else FD_URL) % (y, doc)
        name = " ".join(x for x in (f.get("Prefix"), f.get("First"), f.get("Last"),
                                    f.get("Suffix")) if x)
        out.append({"doc_id": doc, "type": kind, "type_name": FILING_TYPES.get(kind, kind),
                    "last": f.get("Last", ""), "first": f.get("First", ""), "name": name,
                    "district": f.get("StateDst", ""), "year": y,
                    "filed": iso(f.get("FilingDate", "")), "url": url})
    out.sort(key=lambda r: (r["filed"] or "", r["doc_id"]), reverse=True)
    return out


def nse_date(value):
    value = str(value or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_nse_snapshot(snapshot, year=None):
    """NSE bulk/block-deal snapshot -> rows compatible with the index commands."""
    if isinstance(snapshot, (bytes, str)):
        try:
            snapshot = json.loads(snapshot.decode("utf-8") if isinstance(snapshot, bytes) else snapshot)
        except (ValueError, UnicodeDecodeError) as exc:
            raise PtrError("NSE deal snapshot is not valid JSON: %s" % exc)
    if not isinstance(snapshot, dict):
        raise PtrError("NSE deal snapshot must be a JSON object")
    if not any(key in snapshot for key in ("BULK_DEALS_DATA", "BLOCK_DEALS_DATA")):
        raise PtrError("NSE deal snapshot has no bulk/block deal fields")
    out = []
    kinds = (("BULK_DEALS_DATA", "B", "Bulk deal"), ("BLOCK_DEALS_DATA", "K", "Block deal"))
    for key, code, label in kinds:
        deals = snapshot.get(key) or []
        if not isinstance(deals, list):
            raise PtrError("NSE field %s is not a list" % key)
        for deal in deals:
            if not isinstance(deal, dict):
                continue
            symbol = nse_ticker(deal.get("symbol"))
            client = " ".join(str(deal.get("clientName") or "").split())
            filed = nse_date(deal.get("date"))
            if not symbol or not client or not filed or (year and int(filed[:4]) != year):
                continue
            side = str(deal.get("buySell") or "").strip().upper()
            quantity = str(deal.get("qty") or "").replace(",", "").strip()
            price = str(deal.get("watp") or "").replace(",", "").strip()
            stable = "|".join((code, filed, symbol, client, side, quantity, price))
            doc_id = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:16]
            row = {
                "doc_id": doc_id, "type": code, "type_name": label,
                "last": client, "first": "", "name": client, "district": symbol,
                "year": int(filed[:4]), "filed": filed, "url": NSE_DEALS_PAGE,
                "deal": {"symbol": symbol, "company": str(deal.get("name") or "").strip(),
                         "client": client, "side": side, "quantity": quantity, "price": price,
                         "remarks": deal.get("remarks"), "amount_inr": None},
            }
            try:
                qty_value, price_value = float(quantity), float(price)
                row["deal"]["amount_inr"] = round(qty_value * price_value, 2)
            except ValueError:
                pass
            out.append(row)
    out.sort(key=lambda r: (r["filed"], r["doc_id"]), reverse=True)
    return out


def _nse_download():
    req = urllib.request.Request(NSE_DEALS_URL, headers={
        "User-Agent": UA, "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9", "Referer": NSE_DEALS_PAGE,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise PtrError("NSE deal feed answered HTTP %s" % exc.code)
    except urllib.error.URLError as exc:
        raise PtrError("could not reach NSE deal feed: %s" % exc.reason)
    except OSError as exc:
        raise PtrError("could not read NSE deal feed: %s" % exc)


def _write_nse_cache(snapshot, path=NSE_CACHE):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"saved_at": _dt.datetime.now(IST).isoformat(timespec="seconds"),
                   "snapshot": snapshot}, fh, indent=2)
        fh.write("\n")


def load_index(year, offline=None):
    global LAST_INDEX_NOTE
    LAST_INDEX_NOTE = None
    if offline:
        with open(offline, "rb") as fh:
            data = fh.read()
        try:
            snapshot = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PtrError("--offline expects a saved NSE JSON snapshot")
        if isinstance(snapshot, dict) and "snapshot" in snapshot:
            snapshot = snapshot["snapshot"]
        return parse_nse_snapshot(snapshot, year)
    try:
        snapshot = json.loads(_nse_download().decode("utf-8"))
        rows = parse_nse_snapshot(snapshot, year)
        _write_nse_cache(snapshot)
        return rows
    except (PtrError, ValueError, UnicodeDecodeError) as exc:
        if os.path.isfile(NSE_CACHE):
            try:
                with open(NSE_CACHE, encoding="utf-8") as fh:
                    cached = json.load(fh)
                snapshot = cached.get("snapshot", cached)
                rows = parse_nse_snapshot(snapshot, year)
            except (OSError, ValueError, PtrError) as cache_exc:
                raise PtrError("NSE feed failed (%s), and cached snapshot is unusable (%s)"
                               % (exc, cache_exc))
            saved_at = cached.get("saved_at", "unknown time")
            LAST_INDEX_NOTE = "NSE feed unavailable (%s); using cached snapshot saved %s." % (exc, saved_at)
            return rows
        raise PtrError("NSE feed unavailable (%s); no cached snapshot. Use --offline with a saved NSE JSON file."
                       % exc)


def member_match(row, member):
    """Case-insensitive match against the NSE deal counterparty name or symbol."""
    member = (member or "").strip().lower()
    if not member:
        return True
    return member in str(row.get("name", "")).lower() or member in str(row.get("district", "")).lower()


def select(rows, members=None, kind="P", since=None):
    out = []
    for r in rows:
        if kind and kind.upper() not in ("P", "ALL") and r["type"] not in kind.upper():
            continue
        if members and not any(member_match(r, m) for m in members):
            continue
        if since and (r["filed"] or "") < since:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- state

def load_state(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"seen": [], "last_check": None}


def save_state(path, state):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")


def new_since(rows, state, today, first_days=LATE_DAYS):
    """Rows not seen before. On the first run: only those filed in the last
    first_days days, so you are not flooded with the whole year."""
    seen = set(state.get("seen", []))
    if state.get("last_check") is None:
        cutoff = (today - _dt.timedelta(days=first_days)).isoformat()
        return [r for r in rows if (r["filed"] or "") >= cutoff]
    return [r for r in rows if r["doc_id"] not in seen]


# ---------------------------------------------------------------- parsing a PTR

TXN = re.compile(
    r"^\s*(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+(?P<type>S \(partial\)|P|S|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+(?P<amount>\S.*?)\s*$")
LABELS = [
    ("status", re.compile(r"^\s*F(?:ILING)?\s+S(?:TATUS)?\s*:\s*(.*)$", re.I)),
    ("description", re.compile(r"^\s*D(?:ESCRIPTION)?\s*:\s*(.*)$", re.I)),
    ("account", re.compile(r"^\s*S(?:UBHOLDING)?\s+O(?:F)?\s*:\s*(.*)$", re.I)),
    ("location", re.compile(r"^\s*L(?:OCATION)?\s*:\s*(.*)$", re.I)),
]
# Column headers repeat at the top of every page, sometimes in the middle of a
# row that continues on the next page: skip them without ending the row.
PAGE_HEADER = re.compile(r"^\s*(ID\s+Owner\s+Asset|Type\s+Date\b|Type$|Date$|Gains >$|\$200\?$)",
                         re.I)
END = re.compile(r"^\s*(\* For the complete|I CERTIFY|Digitally Signed|Filing ID #)", re.I)
AMOUNT = re.compile(r"\$([\d,]+)")
OPTION = re.compile(r"([\d,]+)\s+(call|put)\s+options?\b.*?strike price of \$([\d,.]+)"
                    r".*?expiration date of (\d{1,2}/\d{1,2}/\d{2,4})", re.I | re.S)
SHARES = re.compile(r"(?:purchased|sold|bought)\s+([\d,]+)\s+shares", re.I)


def _amount(text):
    nums = [int(x.replace(",", "")) for x in AMOUNT.findall(text or "")]
    low = nums[0] if nums else None
    high = nums[1] if len(nums) > 1 else None
    return low, high


def _finish(row, filed):
    asset = " ".join(row.pop("_asset").split())
    tick = re.findall(r"\(([A-Z][A-Z0-9.\-/]{0,9})\)", asset)
    code = re.findall(r"\[([A-Z]{2})\]", asset)
    row["ticker"] = tick[-1] if tick else None
    row["asset_type"] = code[-1] if code else None
    name = re.sub(r"\s*\[[A-Z]{2}\]", "", asset)
    if tick:
        name = re.sub(r"\s*\(%s\)" % re.escape(tick[-1]), "", name)
    row["asset"] = name.strip(" -")
    row["amount"] = " ".join(row["amount"].split())
    row["amount_low"], row["amount_high"] = _amount(row["amount"])
    row["description"] = " ".join(row.get("description", "").split()) or None
    opt = OPTION.search(row["description"] or "")
    if opt:
        row["option"] = {"contracts": int(opt.group(1).replace(",", "")),
                         "kind": opt.group(2).lower(),
                         "strike": float(opt.group(3).replace(",", "")),
                         "expires": iso(opt.group(4)),
                         "exercised": bool(re.match(r"\s*exercised\b", row["description"], re.I))}
    sh = SHARES.search(row["description"] or "")
    if sh:
        row["shares"] = int(sh.group(1).replace(",", ""))
    if filed and row["date"]:
        days = (_dt.date.fromisoformat(filed) - _dt.date.fromisoformat(row["date"])).days
        row["days_to_disclose"] = days
        row["late"] = days > LATE_DAYS
    return row


def parse_ptr_text(text, filed=None):
    """Rows from `pdftotext -layout` output of a House PTR.

    Returns {"name", "status", "district", "doc_id", "filed", "rows": [...]}.
    Each row: owner, owner_name, asset, ticker, asset_type, type, type_name,
    date, notified, amount, amount_low, amount_high, status, description,
    account, option {contracts, kind, strike, expires}, shares,
    days_to_disclose, late."""
    head = {"name": None, "status": None, "district": None, "doc_id": None, "filed": filed}
    rows, cur, mode = [], None, None
    amount_open = False
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        m = re.match(r"^Name:\s+(.*)$", s)
        if m and not head["name"]:
            head["name"] = m.group(1).strip()
            continue
        m = re.match(r"^Status:\s+(.*)$", s)
        if m and not head["status"]:
            head["status"] = m.group(1).strip()
            continue
        m = re.match(r"^State/District:\s*(\S+)", s)
        if m:
            head["district"] = m.group(1)
            continue
        m = re.search(r"Filing ID #(\d+)", s)
        if m:
            head["doc_id"] = m.group(1)
            continue
        m = re.match(r"^Digitally Signed:.*?,\s*(\d{2}/\d{2}/\d{4})", s)
        if m:
            head["filed"] = head["filed"] or iso(m.group(1))
            continue
        t = TXN.match(line)
        if t and iso(t.group("date")):
            if cur:
                rows.append(cur)
            owner = t.group("owner") or ""
            cur = {"owner": owner, "owner_name": OWNERS.get(owner, owner),
                   "_asset": t.group("asset"), "type": t.group("type"),
                   "type_name": TXN_TYPES.get(t.group("type"), t.group("type")),
                   "date": iso(t.group("date")), "notified": iso(t.group("notified")),
                   "amount": t.group("amount"), "status": None, "account": None}
            amount_open = cur["amount"].endswith("-")
            mode = "asset"
            continue
        if cur is None:
            continue
        if not s:
            if mode in ("description", "asset"):
                mode = "labels" if mode == "description" or "[" in cur["_asset"] else mode
            continue
        if PAGE_HEADER.match(s):
            continue
        if END.match(s):
            mode = "labels"
            continue
        label = next(((k, rx.match(line)) for k, rx in LABELS if rx.match(line)), None)
        if label:
            key, lm = label
            value = lm.group(1).strip()
            if key == "description":
                cur["description"] = value
                mode = "description"
            elif key in ("status", "account"):
                cur[key] = value
                mode = "labels"
            else:
                mode = "labels"
            continue
        if mode == "asset":
            parts = re.split(r"\s{2,}", s)
            if amount_open and re.fullmatch(r"\$[\d,]+", parts[-1]):
                cur["amount"] += " " + parts.pop()
                amount_open = False
            if parts and parts[0]:
                cur["_asset"] += " " + " ".join(parts)
        elif mode == "description":
            cur["description"] = cur.get("description", "") + " " + s
    if cur:
        rows.append(cur)
    head["rows"] = [_finish(r, head["filed"]) for r in rows]
    return head


def pdf_to_text(pdf_path):
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-layout", pdf_path, "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


# ---------------------------------------------------------------- output

def watchlist(args):
    tickers = {nse_ticker(t) for t in (args.tickers or [])}
    path = getattr(args, "portfolio", None)
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for p in json.load(fh).get("positions", []):
                if p.get("ticker"):
                    tickers.add(nse_ticker(p["ticker"]))
    return tickers


def nse_report(row):
    deal = row["deal"]
    amount = deal.get("amount_inr")
    amount_text = "₹{:,.2f}".format(amount) if amount is not None else "not reported"
    side = deal.get("side") or "unknown side"
    detail = "%s shares at ₹%s" % (deal.get("quantity") or "unknown quantity",
                                    deal.get("price") or "unknown price")
    if deal.get("remarks"):
        detail += "; " + str(deal["remarks"])
    transaction = {
        "owner": "", "owner_name": deal["client"], "asset": deal.get("company") or deal["symbol"],
        "ticker": deal["symbol"], "asset_type": "EQ", "type": side,
        "type_name": "%s %s" % (row["type_name"], side.lower()),
        "date": row["filed"], "notified": row["filed"], "amount": amount_text,
        "amount_low": amount, "amount_high": amount, "status": None,
        "description": detail, "account": None, "days_to_disclose": 0, "late": False,
    }
    return {"source": "NSE", "name": deal["client"], "status": transaction["type_name"],
            "district": deal["symbol"], "doc_id": row["doc_id"], "filed": row["filed"],
            "url": row["url"], "rows": [transaction]}


def nse_snapshot_report(rows, filed=None):
    transactions = []
    for row in rows:
        transactions.extend(nse_report(row)["rows"])
    date = filed or max((r["filed"] for r in rows), default=None)
    return {"source": "NSE", "name": "NSE bulk/block deals", "status": "deal snapshot",
            "district": "NSE", "doc_id": "snapshot", "filed": date,
            "url": NSE_DEALS_PAGE, "rows": transactions}


def describe(row):
    bits = ["%s %s" % (row["owner_name"], row["type_name"])]
    what = row["asset"]
    if row.get("ticker"):
        what += " (%s)" % row["ticker"]
    if row.get("option"):
        o = row["option"]
        what += ": %s%d %s options, ₹%g strike, expires %s" % (
            "exercised " if o.get("exercised") else "", o["contracts"], o["kind"],
            o["strike"], o["expires"])
    elif row.get("shares"):
        what += ": %s shares" % "{:,}".format(row["shares"])
    bits.append(what)
    bits.append(row["amount"])
    when = "traded %s" % row["date"]
    if row.get("days_to_disclose") is not None:
        when += ", disclosed %d days later%s" % (row["days_to_disclose"],
                                                   " (LATE: over 45)" if row.get("late") else "")
    bits.append(when)
    if row.get("account"):
        bits.append("account: %s" % row["account"])
    return ", ".join(bits)


def render_report(report, watch=()):
    is_nse = report.get("source") == "NSE"
    out = (["%s (%s), %s. NSE deal %s, disclosed %s." % (
        report.get("name") or "?", report.get("district") or "?", report.get("status") or "?",
        report.get("doc_id") or "?", report.get("filed") or "?")] if is_nse else
           ["%s (%s), %s. PTR %s, filed %s." % (
               report.get("name") or "?", report.get("district") or "?", report.get("status") or "?",
               report.get("doc_id") or "?", report.get("filed") or "?")])
    if report.get("url"):
        out.append("  Source: " + report["url"])
    for r in report["rows"]:
        flag = "  <- %s is on your list" % r["ticker"] if r.get("ticker") in watch else ""
        out.append("  - %s%s" % (describe(r), flag))
        if r.get("description") and not r.get("option") and not r.get("shares"):
            out.append("      %s" % r["description"])
    if not report["rows"]:
        out.append("  no NSE deal rows found in this snapshot" if is_nse else
                   "  no transaction rows found: read the PDF directly")
    if is_nse:
        out.append("NSE bulk/block-deal disclosure, not a promoter-insider filing or a recommendation. "
                   "Reported deals can be stale and do not establish current holdings or intent.")
    else:
        out.append("Public disclosure under the STOCK Act, filed up to 45 days after the trade. "
                   "Amounts are ranges. Not inside information.")
    return "\n".join(out) + "\n"


def render_index(rows):
    out = []
    for r in rows:
        deal = r.get("deal") or {}
        detail = "%s %s shares @ ₹%s" % (deal.get("side") or "?", deal.get("quantity") or "?",
                                          deal.get("price") or "?")
        out.append("%s  %-24s %-12s %-5s %-16s %s  %s" % (
            r["filed"], r["name"][:24], r["district"], r["type"], r["doc_id"], detail, r["url"]))
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------- CLI

def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    year = _dt.datetime.now(IST).year

    def common(p):
        p.add_argument("--year", type=int, default=year)
        p.add_argument("--member", action="append",
                       help="counterparty name or NSE ticker; repeat to match more")
        p.add_argument("--type", default="BK", help="B=bulk, K=block, BK=both (default)")
        p.add_argument("--since", help="only deal dates on or after YYYY-MM-DD")
        p.add_argument("--offline", metavar="FILE", help="saved NSE JSON snapshot")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("index", help="list NSE bulk and block deals")
    common(p)
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("new", help="deals not seen at the last check")
    common(p)
    p.add_argument("--state", default="trader/state/pelosi.json")
    p.add_argument("--today", help="YYYY-MM-DD, for reproducible runs")
    p.add_argument("--dry-run", action="store_true", help="do not update the state file")
    p.add_argument("--read", action="store_true", help="include full deal details from the snapshot")
    p.add_argument("--pdf-dir", default="trader/state/ptr-pdfs")
    p.add_argument("--tickers", nargs="*", help="flag trades in these tickers")
    p.add_argument("--portfolio", default="trader/portfolio.json")

    p = sub.add_parser("fetch", help="show one deal by its snapshot ID")
    p.add_argument("doc_id")
    p.add_argument("--year", type=int, default=year)
    p.add_argument("--filed", help="filing date YYYY-MM-DD if known")
    p.add_argument("--pdf-dir", default="trader/state/ptr-pdfs")
    p.add_argument("--tickers", nargs="*")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("parse", help="parse an NSE JSON snapshot or legacy PTR text")
    p.add_argument("text_file")
    p.add_argument("--filed", help="filing date YYYY-MM-DD (else read from the signature)")
    p.add_argument("--tickers", nargs="*")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--json", action="store_true")
    return ap


def _fetch_and_read(doc_id, year, pdf_dir, filed=None):
    os.makedirs(pdf_dir, exist_ok=True)
    path = os.path.join(pdf_dir, "%s.pdf" % doc_id)
    if not os.path.isfile(path):
        data = download(PTR_URL % (year, doc_id))
        if not data.startswith(b"%PDF"):
            raise PtrError("doc %s did not come back as a PDF" % doc_id)
        with open(path, "wb") as fh:
            fh.write(data)
    text = pdf_to_text(path)
    if text is None:
        return path, None
    report = parse_ptr_text(text, filed=filed)
    report["pdf"] = path
    report["url"] = PTR_URL % (year, doc_id)
    return path, report


def main(argv=None):
    args = _parser().parse_args(argv)
    if not args.cmd:
        _parser().print_help()
        return 2
    try:
        return _run(args)
    except (PtrError, OSError) as exc:
        print("ptr: %s" % exc, file=sys.stderr)
        return 2


def _run(args):
    if args.cmd == "parse":
        with open(args.text_file, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if isinstance(doc, dict) and "snapshot" in doc:
            doc = doc["snapshot"]
        if isinstance(doc, dict) and ("BULK_DEALS_DATA" in doc or "BLOCK_DEALS_DATA" in doc):
            report = nse_snapshot_report(parse_nse_snapshot(doc), args.filed)
        else:
            report = parse_ptr_text(text, filed=args.filed)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            sys.stdout.write(render_report(report, watchlist(args)))
        return 0 if report["rows"] else 1

    if args.cmd == "fetch":
        rows = load_index(args.year)
        row = next((r for r in rows if r["doc_id"] == args.doc_id), None)
        if LAST_INDEX_NOTE:
            print("note: " + LAST_INDEX_NOTE, file=sys.stderr)
        if row is None:
            raise PtrError("deal %s is not in the latest NSE snapshot or cache" % args.doc_id)
        report = nse_report(row)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            sys.stdout.write(render_report(report, watchlist(args)))
        return 0 if report["rows"] else 1

    rows = load_index(args.year, args.offline)
    if LAST_INDEX_NOTE:
        print("note: " + LAST_INDEX_NOTE, file=sys.stderr)
    picked = select(rows, args.member, args.type or None, args.since)

    if args.cmd == "index":
        picked = picked[:args.limit]
        if args.json:
            print(json.dumps(picked, indent=2))
        else:
            sys.stdout.write(render_index(picked))
            if not picked:
                print("No matching NSE deals for %d." % args.year)
        return 0 if picked else 1

    # new
    today = _dt.date.fromisoformat(args.today) if args.today else _dt.datetime.now(IST).date()
    state = load_state(args.state)
    fresh = new_since(picked, state, today)
    watch = watchlist(args)
    reports = []
    if args.read:
        for r in fresh:
            reports.append({"filing": r, "pdf": None, "report": nse_report(r)})
    if args.json:
        print(json.dumps({"new": fresh, "reports": reports}, indent=2))
    else:
        if not fresh:
            print("Nothing new since %s." % (state.get("last_check") or "the last check"))
        for r in fresh:
            print("NEW %s: %s (%s) disclosed %s, deal %s" % (
                r["type_name"], r["name"], r["district"], r["filed"], r["doc_id"]))
            print("    %s" % r["url"])
        for rep in reports:
            sys.stdout.write(render_report(rep["report"], watch))
    if not args.dry_run:
        seen = set(state.get("seen", []))
        seen.update(r["doc_id"] for r in picked)
        state["seen"] = sorted(seen)
        state["last_check"] = _dt.datetime.now(IST).replace(microsecond=0).isoformat()
        save_state(args.state, state)
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())
