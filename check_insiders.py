"""
Insider Trading Alert Bot — EDGAR real-time edition
Monitors SEC EDGAR's live Form 4 feed for open-market cash purchases (type P).
Filters for US-listed stocks (NYSE/NASDAQ/AMEX via daily FMP screener cache).
Sends Telegram alerts within minutes of SEC filing acceptance.
Writes alerts.json for the GitHub Pages dashboard.
"""

import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────────
FMP_API_KEY       = os.environ["FMP_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

MIN_VALUE         = 100_000          # alert threshold in dollars
SEEN_FILE         = Path("last_seen.json")
ALERTS_FILE       = Path("alerts.json")
SYMBOLS_FILE      = Path("us_symbols_cache.json")  # daily FMP cache
FMP_BASE          = "https://financialmodelingprep.com/stable"

# EDGAR real-time Form 4 feed — returns the 40 most recent Form 4 filings
EDGAR_FEED        = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_HEADERS     = {"User-Agent": "InsiderAlertBot/1.0 contact@example.com"}  # SEC requires a User-Agent

# How many days of alerts to keep in alerts.json
DASHBOARD_DAYS    = 30

ET_ZONE           = ZoneInfo("America/New_York")


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_et() -> datetime:
    return datetime.now(ET_ZONE)

def load_seen() -> set:
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        return set(data.get("seen_ids", []))
    return set()

def save_seen(seen_ids: set):
    SEEN_FILE.write_text(json.dumps({"seen_ids": list(seen_ids)}, indent=2))

def load_alerts() -> list:
    if ALERTS_FILE.exists():
        data = json.loads(ALERTS_FILE.read_text())
        return data.get("alerts", [])
    return []

def save_alerts(alerts: list):
    cutoff = (now_utc() - timedelta(days=DASHBOARD_DAYS)).strftime("%Y-%m-%d")
    seen_uids = set()
    filtered = []
    for a in alerts:
        if a["uid"] not in seen_uids and a.get("transactionDate", "9999") >= cutoff:
            seen_uids.add(a["uid"])
            filtered.append(a)
    filtered.sort(key=lambda x: (x.get("filingDatetime", ""), x.get("transactionDate", "")), reverse=True)
    ALERTS_FILE.write_text(json.dumps({
        "updated": now_utc().strftime("%Y-%m-%d %H:%M UTC"),
        "alerts":  filtered
    }, indent=2))
    print(f"  Dashboard: {len(filtered)} alerts saved to alerts.json")


# ── US Symbol Cache (refreshed once per day) ──────────────────────────────────

def load_us_symbols() -> set:
    """
    Return cached US symbol set if fresh (< 23 hours old).
    Otherwise fetch from FMP screener and cache to disk.
    """
    if SYMBOLS_FILE.exists():
        data = json.loads(SYMBOLS_FILE.read_text())
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01T00:00:00+00:00"))
        age_hours = (now_utc() - cached_at).total_seconds() / 3600
        if age_hours < 23:
            symbols = set(data.get("symbols", []))
            print(f"  US symbol cache: {len(symbols):,} stocks (age {age_hours:.1f}h)")
            return symbols

    # Fetch fresh from FMP
    print("  Fetching fresh US symbol universe from FMP...")
    symbols = set()
    url = (
        f"{FMP_BASE}/company-screener"
        f"?isActivelyTrading=true"
        f"&exchange=NYSE,NASDAQ,AMEX"
        f"&country=US"
        f"&limit=10000"
        f"&apikey={FMP_API_KEY}"
    )
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        for row in data:
            if not row.get("isEtf") and not row.get("isFund") and row.get("symbol"):
                symbols.add(row["symbol"])

    SYMBOLS_FILE.write_text(json.dumps({
        "cached_at": now_utc().isoformat(),
        "symbols":   list(symbols)
    }, indent=2))
    print(f"  US symbol universe: {len(symbols):,} stocks loaded and cached")
    return symbols


# ── EDGAR Feed ────────────────────────────────────────────────────────────────

def fetch_edgar_form4_index() -> list[dict]:
    """
    Fetch the EDGAR real-time Form 4 Atom feed.
    Returns a list of dicts with accession number, company name, CIK, filing datetime.
    """
    resp = requests.get(EDGAR_FEED, headers=EDGAR_HEADERS, timeout=30)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    entries = []
    for entry in root.findall("atom:entry", ns):
        title    = entry.findtext("atom:title", default="", namespaces=ns)
        updated  = entry.findtext("atom:updated", default="", namespaces=ns)
        link_el  = entry.find("atom:link", ns)
        link     = link_el.get("href", "") if link_el is not None else ""

        # Extract accession number from the link URL
        # e.g. https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/...
        accession = ""
        if "/Archives/edgar/data/" in link:
            parts = link.split("/")
            # accession is the folder name after the CIK
            try:
                idx = parts.index("data")
                accession = parts[idx + 2]  # folder after CIK
            except (ValueError, IndexError):
                pass

        # Parse filing datetime (UTC from EDGAR, convert to ET)
        filing_dt_utc = None
        filing_dt_et_str = ""
        if updated:
            try:
                filing_dt_utc = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                filing_dt_et = filing_dt_utc.astimezone(ET_ZONE)
                filing_dt_et_str = filing_dt_et.strftime("%Y-%m-%d %H:%M ET")
            except ValueError:
                pass

        entries.append({
            "title":           title,
            "accession":       accession,
            "filing_link":     link,
            "filing_dt_utc":   filing_dt_utc,
            "filing_dt_et":    filing_dt_et_str,
            "updated_raw":     updated,
        })

    return entries


def fetch_form4_detail(filing_link: str) -> list[dict]:
    """
    Fetch the filing index page to find the actual Form 4 XML document,
    then parse it for transaction details.
    Returns a list of transaction dicts (one filing can have multiple transactions).
    """
    # Get the filing index page
    try:
        resp = requests.get(filing_link, headers=EDGAR_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"    Could not fetch filing index: {e}")
        return []

    # Find the XML document link in the index
    xml_url = None
    content = resp.text
    for line in content.split("\n"):
        if ".xml" in line.lower() and "form4" not in line.lower() and "<a href" in line.lower():
            # Extract href
            start = line.lower().find('href="') + 6
            end   = line.find('"', start)
            if start > 5 and end > start:
                path = line[start:end]
                if path.endswith(".xml"):
                    xml_url = "https://www.sec.gov" + path if path.startswith("/") else path
                    break

    # Fallback: look for any .xml file in the index
    if not xml_url:
        import re
        matches = re.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', content, re.IGNORECASE)
        if matches:
            xml_url = "https://www.sec.gov" + matches[0]

    if not xml_url:
        return []

    # Fetch and parse the Form 4 XML
    try:
        time.sleep(0.1)  # be polite to EDGAR
        xml_resp = requests.get(xml_url, headers=EDGAR_HEADERS, timeout=20)
        xml_resp.raise_for_status()
        return parse_form4_xml(xml_resp.text, xml_url)
    except Exception as e:
        print(f"    Could not fetch/parse XML: {e}")
        return []


def parse_form4_xml(xml_text: str, xml_url: str) -> list[dict]:
    """
    Parse a Form 4 XML document and extract non-derivative transactions.
    Returns one dict per transaction row.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    def find_text(el, *tags):
        for tag in tags:
            found = el.find(".//" + tag)
            if found is not None and found.text:
                return found.text.strip()
        return ""

    # Issuer (company) info
    issuer_el  = root.find(".//issuer")
    symbol     = find_text(issuer_el or root, "issuerTradingSymbol") if issuer_el is not None else find_text(root, "issuerTradingSymbol")
    company    = find_text(issuer_el or root, "issuerName") if issuer_el is not None else find_text(root, "issuerName")

    # Reporting person info
    owner_el   = root.find(".//reportingOwner")
    insider    = find_text(owner_el or root, "rptOwnerName") if owner_el is not None else find_text(root, "rptOwnerName")
    title      = find_text(owner_el or root, "officerTitle") if owner_el is not None else find_text(root, "officerTitle")
    is10pct    = find_text(owner_el or root, "isTenPercentOwner") if owner_el is not None else ""

    transactions = []

    # Non-derivative transactions (table 1) — these are the cash purchases
    for tx in root.findall(".//nonDerivativeTransaction"):
        tx_type    = find_text(tx, "transactionCode")
        aod        = find_text(tx, "transactionAcquiredDisposedCode")
        tx_date    = find_text(tx, "transactionDate", "value")
        shares_str = find_text(tx, "transactionShares", "value")
        price_str  = find_text(tx, "transactionPricePerShare", "value")

        # Only type P (open-market cash purchase) acquisitions
        if tx_type.upper() != "P":
            continue
        if aod.upper() != "A":
            continue

        try:
            shares = float(shares_str) if shares_str else 0.0
            price  = float(price_str)  if price_str  else 0.0
            value  = shares * price
        except ValueError:
            continue

        transactions.append({
            "symbol":          symbol.upper() if symbol else "",
            "companyName":     company,
            "reportingName":   insider,
            "title":           title,
            "is10Pct":         is10pct == "1",
            "transactionDate": tx_date,
            "shares":          shares,
            "price":           price,
            "totalValue":      round(value, 2),
            "secLink":         xml_url,
        })

    return transactions


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def build_alert_message(rec: dict) -> str:
    shares_str = f"{rec['shares']:,.0f}" if rec.get("shares") else "N/A"
    price_str  = f"${rec['price']:.2f}"  if rec.get("price")  else "N/A"
    value_str  = f"${rec['totalValue']:,.0f}" if rec.get("totalValue") else "N/A"

    lines = [
        f"🟢 <b>Insider Purchase — ${rec['symbol']}</b>",
        f"",
        f"🏢 {rec.get('companyName', '')}",
        f"👤 <b>{rec.get('reportingName', 'N/A')}</b>",
        f"🏷  {rec['title']}" if rec.get("title") else "",
        f"",
        f"📅 Transaction: {rec.get('transactionDate', 'N/A')}",
        f"📋 Filed:       {rec.get('filingDatetime', 'N/A')}",
        f"",
        f"🔢 Shares:      {shares_str}",
        f"💵 Price:       {price_str}",
        f"💰 Total Value: <b>{value_str}</b>",
        f"",
        f'📄 <a href="{rec["secLink"]}">SEC Form 4 Filing</a>',
    ]
    return "\n".join(l for l in lines if l is not None)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{now_utc():%Y-%m-%d %H:%M} UTC] Checking EDGAR Form 4 feed...")

    seen_ids        = load_seen()
    existing_alerts = load_alerts()
    us_symbols      = load_us_symbols()

    # Fetch the live EDGAR feed
    print("  Fetching EDGAR real-time Form 4 feed...")
    try:
        feed_entries = fetch_edgar_form4_index()
    except Exception as e:
        print(f"  ERROR fetching EDGAR feed: {e}")
        return
    print(f"  Found {len(feed_entries)} Form 4 filings in feed")

    new_alerts  = []
    new_records = []

    for entry in feed_entries:
        accession      = entry["accession"]
        filing_dt_et   = entry["filing_dt_et"]
        filing_dt_utc  = entry["filing_dt_utc"]
        filing_link    = entry["filing_link"]

        # Skip if we've already processed this filing
        if accession in seen_ids:
            continue

        # Mark as seen regardless of outcome to avoid reprocessing
        seen_ids.add(accession)

        # Fetch and parse the Form 4 XML for transaction details
        transactions = fetch_form4_detail(filing_link)
        if not transactions:
            continue

        for tx in transactions:
            symbol = tx.get("symbol", "")

            # US-listed filter
            if symbol not in us_symbols:
                continue

            # Skip 10% owners (not officer/director conviction buys)
            if tx.get("is10Pct"):
                continue

            # Value threshold
            if tx["totalValue"] < MIN_VALUE:
                continue

            # Build a stable dedup UID
            uid = "|".join([
                symbol,
                tx.get("reportingName", ""),
                tx.get("transactionDate", ""),
                str(tx.get("shares", "")),
                str(tx.get("price", "")),
            ])

            if uid in seen_ids:
                continue
            seen_ids.add(uid)

            # Build full record
            rec = {
                **tx,
                "uid":             uid,
                "filingDatetime":  filing_dt_et,
                "filingDatetimeUTC": filing_dt_utc.isoformat() if filing_dt_utc else "",
            }

            new_alerts.append(rec)

    print(f"  {len(new_alerts)} new purchase(s) above ${MIN_VALUE:,} in US-listed stocks")

    for rec in new_alerts:
        message = build_alert_message(rec)
        send_telegram(message)
        print(f"  ✅ Alerted: {rec['symbol']} — {rec['reportingName']} — {rec['filingDatetime']}")

        # Build dashboard record
        new_records.append({
            "uid":             rec["uid"],
            "symbol":          rec["symbol"],
            "companyName":     rec.get("companyName", ""),
            "reportingName":   rec.get("reportingName", ""),
            "title":           rec.get("title", ""),
            "transactionDate": rec.get("transactionDate", ""),
            "filingDatetime":  rec.get("filingDatetime", ""),
            "shares":          rec.get("shares"),
            "price":           rec.get("price"),
            "totalValue":      rec.get("totalValue"),
            "secLink":         rec.get("secLink", ""),
        })

    if not new_alerts:
        print("  No new alerts to send.")

    save_alerts(new_records + existing_alerts)
    save_seen(seen_ids)
    print("  Done.")


if __name__ == "__main__":
    main()
