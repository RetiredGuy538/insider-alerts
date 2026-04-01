"""
One-time 30-day backfill script.
Pulls insider cash purchases from FMP for the past 30 days
and merges them into alerts.json for the dashboard.
Run once via GitHub Actions, then let EDGAR take over for live alerts.
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────────
FMP_API_KEY    = os.environ["FMP_API_KEY"]
ALERTS_FILE    = Path("alerts.json")
SYMBOLS_FILE   = Path("us_symbols_cache.json")
FMP_BASE       = "https://financialmodelingprep.com/stable"
MIN_VALUE      = 100_000
BACKFILL_DAYS  = 30
DASHBOARD_DAYS = 30
ET_ZONE        = ZoneInfo("America/New_York")


def now_utc():
    return datetime.now(timezone.utc)

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
    print("  Saved " + str(len(filtered)) + " total alerts to alerts.json")

def load_us_symbols():
    if SYMBOLS_FILE.exists():
        data = json.loads(SYMBOLS_FILE.read_text())
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01T00:00:00+00:00"))
        age_hours = (now_utc() - cached_at).total_seconds() / 3600
        if age_hours < 23:
            symbols = set(data.get("symbols", []))
            print("  US symbol cache: " + str(len(symbols)) + " stocks")
            return symbols
    print("  Fetching US symbol universe from FMP...")
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
    print("  US symbol universe: " + str(len(symbols)) + " stocks loaded")
    return symbols

def fetch_fmp_purchases(from_date, to_date):
    """Page through FMP insider feed for the date range."""
    results = []
    page = 0
    MAX_PAGES = 80  # 8,000 filings max — plenty for 30 days

    while page < MAX_PAGES:
        url = (FMP_BASE + "/insider-trading/latest"
               + "?transactionType=P-Purchase"
               + "&page=" + str(page)
               + "&limit=100"
               + "&apikey=" + FMP_API_KEY)
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print("  FMP fetch error page " + str(page) + ": " + str(e))
            break

        if not isinstance(data, list) or len(data) == 0:
            break

        found_old = False
        for row in data:
            tx_date = row.get("transactionDate") or row.get("filingDate") or ""
            if tx_date < from_date:
                found_old = True
                continue
            if tx_date > to_date:
                continue
            results.append(row)

        if found_old:
            break  # we've gone past our window

        page += 1
        if page % 10 == 0:
            print("  FMP page " + str(page) + " — " + str(len(results)) + " candidates so far...")

    return results

def build_uid(row):
    return "|".join([
        row.get("symbol", ""),
        row.get("reportingName", ""),
        row.get("transactionDate", ""),
        str(row.get("securitiesTransacted", "")),
        str(row.get("price", "")),
    ])

def main():
    print("[" + now_utc().strftime("%Y-%m-%d %H:%M") + " UTC] Starting 30-day backfill...")

    from_date = (now_utc() - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%d")
    to_date   = now_utc().strftime("%Y-%m-%d")
    print("  Date range: " + from_date + " to " + to_date)

    existing_alerts = load_alerts()
    existing_uids   = {a["uid"] for a in existing_alerts}
    us_symbols      = load_us_symbols()

    print("  Fetching FMP insider purchases...")
    raw = fetch_fmp_purchases(from_date, to_date)
    print("  Fetched " + str(len(raw)) + " raw P-Purchase filings from FMP")

    new_records = []
    skipped_type = 0
    skipped_us   = 0
    skipped_val  = 0
    skipped_dup  = 0

    for row in raw:
        # Type P only
        tt = str(row.get("transactionType") or "").upper().strip()
        if tt not in ("P", "P-PURCHASE"):
            skipped_type += 1
            continue

        # US-listed only
        symbol = row.get("symbol", "")
        if symbol not in us_symbols:
            skipped_us += 1
            continue

        # Skip 10% owners
        own = str(row.get("typeOfOwner") or "").lower()
        if "10%" in own or "beneficial owner only" in own:
            skipped_us += 1
            continue

        # Value threshold
        value = row.get("value")
        if value is None:
            try:
                value = float(row.get("securitiesTransacted", 0)) * float(row.get("price", 0))
            except (TypeError, ValueError):
                value = 0
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0

        if value < MIN_VALUE:
            skipped_val += 1
            continue

        uid = build_uid(row)
        if uid in existing_uids:
            skipped_dup += 1
            continue
        existing_uids.add(uid)

        # Use filing date as the datetime display (FMP doesn't have exact time)
        filing_date = row.get("filingDate", row.get("transactionDate", ""))

        shares = row.get("securitiesTransacted")
        price  = row.get("price")

        new_records.append({
            "uid":             uid,
            "symbol":          symbol,
            "companyName":     row.get("companyName", ""),
            "reportingName":   row.get("reportingName", ""),
            "title":           row.get("typeOfOwner", "") or row.get("officerTitle", ""),
            "transactionDate": row.get("transactionDate", ""),
            "filingDatetime":  filing_date,  # FMP only has date, not time
            "shares":          float(shares) if shares else None,
            "price":           float(price)  if price  else None,
            "totalValue":      round(value, 2),
            "secLink":         row.get("link", "") or row.get("secLink", ""),
        })

    print("  New records: " + str(len(new_records)))
    print("  Skipped — wrong type: " + str(skipped_type)
          + ", non-US: " + str(skipped_us)
          + ", below $100K: " + str(skipped_val)
          + ", duplicates: " + str(skipped_dup))

    save_alerts(new_records + existing_alerts)
    print("  Backfill complete.")

if __name__ == "__main__":
    main()
