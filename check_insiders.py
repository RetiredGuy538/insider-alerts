"""
Insider Trading Alert Bot — EDGAR real-time edition (DEBUG VERSION)
"""

import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

FMP_API_KEY       = os.environ["FMP_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

MIN_VALUE         = 100_000
SEEN_FILE         = Path("last_seen.json")
ALERTS_FILE       = Path("alerts.json")
SYMBOLS_FILE      = Path("us_symbols_cache.json")
FMP_BASE          = "https://financialmodelingprep.com/stable"
EDGAR_FEED        = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom"
EDGAR_HEADERS     = {"User-Agent": "InsiderAlertBot/1.0 contact@example.com"}
DASHBOARD_DAYS    = 30
ET_ZONE           = ZoneInfo("America/New_York")


def now_utc():
    return datetime.now(timezone.utc)

def load_seen():
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        return set(data.get("seen_ids", []))
    return set()

def save_seen(seen_ids):
    SEEN_FILE.write_text(json.dumps({"seen_ids": list(seen_ids)}, indent=2))

def load_alerts():
    if ALERTS_FILE.exists():
        data = json.loads(ALERTS_FILE.read_text())
        return data.get("alerts", [])
    return []

def save_alerts(alerts):
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
    print("  Dashboard: " + str(len(filtered)) + " alerts saved to alerts.json")

def load_us_symbols():
    if SYMBOLS_FILE.exists():
        data = json.loads(SYMBOLS_FILE.read_text())
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01T00:00:00+00:00"))
        age_hours = (now_utc() - cached_at).total_seconds() / 3600
        if age_hours < 23:
            symbols = set(data.get("symbols", []))
            print("  US symbol cache: " + str(len(symbols)) + " stocks (age " + str(round(age_hours,1)) + "h)")
            return symbols
    print("  Fetching fresh US symbol universe from FMP...")
    symbols = set()
    url = (FMP_BASE + "/company-screener"
           + "?isActivelyTrading=true"
           + "&exchange=NYSE,NASDAQ,AMEX"
           + "&country=US"
           + "&limit=10000"
           + "&apikey=" + FMP_API_KEY)
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
    print("  US symbol universe: " + str(len(symbols)) + " stocks loaded and cached")
    return symbols

def fetch_edgar_form4_index():
    resp = requests.get(EDGAR_FEED, headers=EDGAR_HEADERS, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = []
    for entry in root.findall("atom:entry", ns):
        title   = entry.findtext("atom:title", default="", namespaces=ns)
        updated = entry.findtext("atom:updated", default="", namespaces=ns)
        link_el = entry.find("atom:link", ns)
        link    = link_el.get("href", "") if link_el is not None else ""
        accession = ""
        if "/Archives/edgar/data/" in link:
            parts = link.split("/")
            try:
                idx = parts.index("data")
                accession = parts[idx + 2]
            except (ValueError, IndexError):
                pass
        filing_dt_utc = None
        filing_dt_et_str = ""
        if updated:
            try:
                filing_dt_utc = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                filing_dt_et = filing_dt_utc.astimezone(ET_ZONE)
                filing_dt_et_str = filing_dt_et.strftime("%Y-%m-%d %H:%M ET")
            except ValueError:
                pass
        # Skip non-Form-4 filings (EDGAR type=4 prefix-matches 424B2 etc.)
        # True Form 4s have titles like "4 - CompanyName (CIK) (Filer)"
        import re as _re
        if not _re.match(r"^4\s*-", title):
            print("    Skipping non-Form-4: " + title[:60])
            continue
        entries.append({
            "title":         title,
            "accession":     accession,
            "filing_link":   link,
            "filing_dt_utc": filing_dt_utc,
            "filing_dt_et":  filing_dt_et_str,
            "updated_raw":   updated,
        })
    return entries

def fetch_form4_detail(filing_link):
    try:
        resp = requests.get(filing_link, headers=EDGAR_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print("    Could not fetch filing index: " + str(e))
        return []

    content = resp.text

    # ── DEBUG: print the raw filing index so we can see its structure ──

    import re as _re2
    xml_url = None

    # Look for the raw Form 4 XML data file — it lives in the filing root folder.
    # Exclude xsl/xslF345X06 paths (those are HTML renderings, not raw XML data).
    # The real data file typically matches the accession number pattern.
    matches = _re2.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', content, _re2.IGNORECASE)
    for match in matches:
        if "xsl" not in match.lower():
            xml_url = "https://www.sec.gov" + match
            break

    # Fallback: take first xml that isn't in an xsl folder
    if not xml_url and matches:
        xml_url = "https://www.sec.gov" + matches[0]

    print("    xml_url found: " + str(xml_url))

    if not xml_url:
        return []

    try:
        time.sleep(0.1)
        xml_resp = requests.get(xml_url, headers=EDGAR_HEADERS, timeout=20)
        xml_resp.raise_for_status()
        return parse_form4_xml(xml_resp.text, xml_url)
    except Exception as e:
        print("    Could not fetch/parse XML: " + str(e))
        return []

def parse_form4_xml(xml_text, xml_url):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print("    XML parse error: " + str(e))
        return []

    def find_text(el, *tags):
        for tag in tags:
            found = el.find(".//" + tag)
            if found is not None and found.text:
                return found.text.strip()
        return ""

    issuer_el = root.find(".//issuer")
    src_issuer = issuer_el if issuer_el is not None else root
    symbol     = find_text(src_issuer, "issuerTradingSymbol")
    company    = find_text(src_issuer, "issuerName")
    owner_el   = root.find(".//reportingOwner")
    src_owner  = owner_el if owner_el is not None else root
    insider    = find_text(src_owner, "rptOwnerName")
    title      = find_text(src_owner, "officerTitle")
    is10pct    = find_text(src_owner, "isTenPercentOwner")

    print("    Parsed: symbol=" + symbol + " company=" + company + " insider=" + insider)

    transactions = []
    for tx in root.findall(".//nonDerivativeTransaction"):
        tx_type    = find_text(tx, "transactionCode")
        aod        = find_text(tx, "transactionAcquiredDisposedCode")
        tx_date    = find_text(tx, "transactionDate", "value")
        shares_str = find_text(tx, "transactionShares", "value")
        price_str  = find_text(tx, "transactionPricePerShare", "value")

        print("    tx: code=" + tx_type + " aod=" + aod + " date=" + tx_date + " shares=" + shares_str + " price=" + price_str)

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

def send_telegram(message):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()

def build_alert_message(rec):
    shares_str = "{:,.0f}".format(rec["shares"]) if rec.get("shares") else "N/A"
    price_str  = "${:.2f}".format(rec["price"])   if rec.get("price")  else "N/A"
    value_str  = "${:,.0f}".format(rec["totalValue"]) if rec.get("totalValue") else "N/A"
    lines = [
        "🟢 <b>Insider Purchase — $" + rec["symbol"] + "</b>",
        "",
        "🏢 " + rec.get("companyName", ""),
        "<b>👤 " + rec.get("reportingName", "N/A") + "</b>",
        "🏷  " + rec["title"] if rec.get("title") else "",
        "",
        "📅 Transaction: " + rec.get("transactionDate", "N/A"),
        "📋 Filed:       " + rec.get("filingDatetime", "N/A"),
        "",
        "🔢 Shares:      " + shares_str,
        "💵 Price:       " + price_str,
        "💰 Total Value: <b>" + value_str + "</b>",
        "",
        '📄 <a href="' + rec["secLink"] + '">SEC Form 4 Filing</a>',
    ]
    return "\n".join(l for l in lines if l)

def main():
    print("[" + now_utc().strftime("%Y-%m-%d %H:%M") + " UTC] Checking EDGAR Form 4 feed...")

    seen_ids        = load_seen()
    existing_alerts = load_alerts()
    us_symbols      = load_us_symbols()

    print("  Fetching EDGAR real-time Form 4 feed...")
    try:
        feed_entries = fetch_edgar_form4_index()
    except Exception as e:
        print("  ERROR fetching EDGAR feed: " + str(e))
        return
    print("  Found " + str(len(feed_entries)) + " Form 4 filings in feed")

    # Process all filings

    new_alerts  = []
    new_records = []

    for entry in feed_entries:
        accession     = entry["accession"]
        filing_dt_et  = entry["filing_dt_et"]
        filing_dt_utc = entry["filing_dt_utc"]
        filing_link   = entry["filing_link"]

        if accession in seen_ids:
            continue

        seen_ids.add(accession)

        transactions = fetch_form4_detail(filing_link)
        if not transactions:
            continue

        for tx in transactions:
            symbol = tx.get("symbol", "")
            in_us  = symbol in us_symbols

            if symbol not in us_symbols:
                continue
            if tx.get("is10Pct"):
                continue
            if tx["totalValue"] < MIN_VALUE:
                continue

            uid = "|".join([symbol, tx.get("reportingName",""), tx.get("transactionDate",""), str(tx.get("shares","")), str(tx.get("price",""))])
            if uid in seen_ids:
                continue
            seen_ids.add(uid)

            rec = {**tx, "uid": uid, "filingDatetime": filing_dt_et, "filingDatetimeUTC": filing_dt_utc.isoformat() if filing_dt_utc else ""}
            new_alerts.append(rec)

    print("  " + str(len(new_alerts)) + " new purchase(s) above $" + str(MIN_VALUE) + " in US-listed stocks")

    for rec in new_alerts:
        message = build_alert_message(rec)
        send_telegram(message)
        print("  Alerted: " + rec["symbol"] + " — " + rec["reportingName"])
        new_records.append({
            "uid": rec["uid"], "symbol": rec["symbol"], "companyName": rec.get("companyName",""),
            "reportingName": rec.get("reportingName",""), "title": rec.get("title",""),
            "transactionDate": rec.get("transactionDate",""), "filingDatetime": rec.get("filingDatetime",""),
            "shares": rec.get("shares"), "price": rec.get("price"), "totalValue": rec.get("totalValue"),
            "secLink": rec.get("secLink",""),
        })

    if not new_alerts:
        print("  No new alerts to send.")

    save_alerts(new_records + existing_alerts)
    save_seen(seen_ids)
    print("  Done.")

if __name__ == "__main__":
    main()
