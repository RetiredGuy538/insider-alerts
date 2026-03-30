"""
Insider Trading Alert Bot
Polls FMP stable/insider-trading for new purchases >= $100,000
and sends Telegram notifications for any new entries since last run.
Only alerts on US-listed companies (NYSE, NASDAQ, AMEX) to match
the insider screener's universe.
"""

import os
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
FMP_API_KEY       = os.environ["FMP_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

MIN_VALUE         = 100_000          # alert threshold in dollars
SEEN_FILE         = Path("last_seen.json")
FMP_BASE          = "https://financialmodelingprep.com/stable"

# How far back to look on first run (days) — avoids flooding on initial setup
LOOKBACK_DAYS     = 1

# US exchanges to include — matches the insider screener exactly
US_EXCHANGES      = {"NYSE", "NASDAQ", "AMEX"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_seen() -> set:
    """Load the set of already-alerted transaction IDs from disk."""
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        return set(data.get("seen_ids", []))
    return set()


def save_seen(seen_ids: set):
    """Persist seen IDs back to disk so the next run skips them."""
    SEEN_FILE.write_text(json.dumps({"seen_ids": list(seen_ids)}, indent=2))


def fetch_us_symbols() -> set:
    """
    Build the set of US-listed stock symbols from the FMP company screener.
    Covers NYSE, NASDAQ, and AMEX; excludes ETFs and funds.
    Cached for the duration of this run only (not persisted to disk).
    """
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

    print(f"  US symbol universe: {len(symbols):,} stocks loaded")
    return symbols


def fetch_insider_purchases() -> list[dict]:
    """
    Fetch recent insider purchases from FMP.
    Uses transactionType=P-Purchase and pages through results
    until we hit entries older than LOOKBACK_DAYS.
    """
    cutoff = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
    results = []
    page = 0

    while True:
        url = (
            f"{FMP_BASE}/insider-trading/latest"
            f"?transactionType=P-Purchase"
            f"&page={page}"
            f"&limit=100"
            f"&apikey={FMP_API_KEY}"
        )
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            break

        for row in data:
            # Parse the filing date
            filing_date_str = row.get("filingDate") or row.get("transactionDate") or ""
            try:
                filing_date = datetime.strptime(filing_date_str[:10], "%Y-%m-%d")
            except ValueError:
                filing_date = datetime.utcnow()  # include if date is unparseable

            if filing_date < cutoff:
                # Remaining rows will be even older — stop paginating
                return results

            results.append(row)

        page += 1

    return results


def build_unique_id(row: dict) -> str:
    """Create a stable dedup key for a transaction row."""
    return "|".join([
        row.get("symbol", ""),
        row.get("reportingName", ""),
        row.get("transactionDate", ""),
        str(row.get("securitiesTransacted", "")),
        str(row.get("price", "")),
    ])


def format_currency(value) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "N/A"


def send_telegram(message: str):
    """Send a message to the configured Telegram chat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def build_alert_message(row: dict) -> str:
    """Format a single insider purchase into a Telegram message."""
    symbol       = row.get("symbol", "N/A")
    name         = row.get("reportingName", "N/A")
    title        = row.get("typeOfOwner", "")
    shares       = row.get("securitiesTransacted", "N/A")
    price        = row.get("price", "N/A")
    total_value  = row.get("value", None)
    trans_date   = row.get("transactionDate", "N/A")
    filing_date  = row.get("filingDate", "N/A")
    sec_link     = row.get("link", "")

    # Calculate total if not provided
    if total_value is None:
        try:
            total_value = float(shares) * float(price)
        except (TypeError, ValueError):
            total_value = None

    total_str = format_currency(total_value) if total_value else "N/A"
    price_str = format_currency(price) if price != "N/A" else "N/A"

    lines = [
        f"🟢 <b>Insider Purchase — ${symbol}</b>",
        f"",
        f"👤 <b>{name}</b>",
        f"🏷  {title}" if title else "",
        f"",
        f"📅 Transaction: {trans_date}",
        f"📋 Filed:       {filing_date}",
        f"",
        f"🔢 Shares:      {float(shares):,.0f}" if shares != "N/A" else "🔢 Shares: N/A",
        f"💵 Price:       {price_str}",
        f"💰 Total Value: <b>{total_str}</b>",
    ]

    if sec_link:
        lines += ["", f'📄 <a href="{sec_link}">SEC Filing</a>']

    return "\n".join(line for line in lines if line is not None)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.utcnow():%Y-%m-%d %H:%M} UTC] Checking FMP insider purchases...")

    seen_ids   = load_seen()
    us_symbols = fetch_us_symbols()          # <-- US universe filter
    purchases  = fetch_insider_purchases()
    print(f"  Fetched {len(purchases)} recent P-Purchase filings")

    new_alerts = []

    for row in purchases:
        uid = build_unique_id(row)

        if uid in seen_ids:
            continue  # already alerted

        # ── Only true open-market cash purchases (Form 4 type "P") ──
        # Excludes: M (option exercise), A (grant/award), C (conversion), G (gift)
        tt = str(row.get("transactionType") or "").upper().strip()
        if tt not in ("P", "P-PURCHASE"):
            seen_ids.add(uid)  # mark seen so we don't recheck it
            continue

        # ── US-only filter — skip foreign-listed companies (e.g. SE, BABA) ──
        symbol = row.get("symbol", "")
        if symbol not in us_symbols:
            seen_ids.add(uid)  # mark seen so we don't recheck it
            continue

        # Apply value threshold
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
            seen_ids.add(uid)  # mark seen so we don't recheck it
            continue

        new_alerts.append((uid, row))

    print(f"  {len(new_alerts)} new purchase(s) above ${MIN_VALUE:,} in US-listed stocks")

    for uid, row in new_alerts:
        message = build_alert_message(row)
        send_telegram(message)
        seen_ids.add(uid)
        print(f"  ✅ Alerted: {row.get('symbol')} — {row.get('reportingName')}")

    if not new_alerts:
        print("  No new alerts to send.")

    save_seen(seen_ids)
    print("  Done.")


if __name__ == "__main__":
    main()
