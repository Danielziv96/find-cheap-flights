"""
Flight price monitor v2: TLV -> Greek Islands
- Real flight data via fast-flights (Google Flights)
- Alerts via Telegram when price < $230/person round trip
- Outbound: departs TLV before 12:00
- Return:   departs island at 17:00 or later
- Never sends the same deal twice
"""

import os
import sys
import json
import re
import hashlib
import time
import requests
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv
from fast_flights import FlightData, Passengers, get_flights

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PRICE_LIMIT_USD  = 230   # per person, round trip
SENT_DB          = Path(__file__).parent / "sent_deals.json"

ISLANDS = {
    "JTR": "Santorini",
    "RHO": "Rhodes",
    "JMK": "Mykonos",
    "HER": "Crete (Heraklion)",
    "CHQ": "Crete (Chania)",
    "CFU": "Corfu",
    "KGS": "Kos",
    "ZTH": "Zakynthos",
    "MJT": "Lesbos",
    "EFL": "Kefalonia",
}


def get_trip_dates() -> tuple[str, str]:
    today = date.today()
    if today <= date(2026, 5, 27):
        return "2026-05-28", "2026-05-31"
    return "2026-06-11", "2026-06-14"


def fetch_ils_rate() -> float:
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
        return float(r.json()["rates"]["ILS"])
    except Exception:
        return 3.67


def parse_price_usd(price_str: str, ils_rate: float) -> float | None:
    """Convert '4483' (ILS) or '$230' to USD per-person (divide by 2 for 2 adults)."""
    digits = re.sub(r"[^\d.]", "", price_str)
    if not digits:
        return None
    total = float(digits)
    # fast-flights returns ILS when running from Israel
    total_usd = total / ils_rate
    return total_usd / 2   # per person


def dep_to_minutes(dep_str: str) -> int | None:
    """'5:40 AM on Thu, May 28'  ->  340 (minutes from midnight)"""
    m = re.match(r"(\d+):(\d+)\s+(AM|PM)", dep_str.strip())
    if not m:
        return None
    h, mn, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
    if ampm == "PM" and h != 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    return h * 60 + mn


def parse_duration_minutes(duration_str: str) -> int | None:
    """'3h 45m', '4 hr 20 min', '2h' -> total minutes"""
    if not duration_str:
        return None
    h = re.search(r"(\d+)\s*hr?", duration_str, re.IGNORECASE)
    m = re.search(r"(\d+)\s*m(?:in)?", duration_str, re.IGNORECASE)
    hours = int(h.group(1)) if h else 0
    mins  = int(m.group(1)) if m else 0
    total = hours * 60 + mins
    return total if total > 0 else None


def send_telegram(text: str):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    ).raise_for_status()


def deal_hash(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def load_sent() -> set:
    try:
        return set(json.loads(SENT_DB.read_text()))
    except Exception:
        return set()


def save_sent(sent: set):
    SENT_DB.write_text(json.dumps(sorted(sent), indent=2))


def gflights_url(dep_date: str, ret_date: str, dest: str) -> str:
    # t:f triggered explore/flexible mode; removing it opens a proper search
    return (
        f"https://www.google.com/travel/flights"
        f"?hl=en#flt=TLV.{dest}.{dep_date}*{dest}.TLV.{ret_date}"
        f";c:USD;e:1;sd:1"
    )


def search_island(code: str, dep_date: str, ret_date: str) -> list[dict]:
    result = get_flights(
        flight_data=[
            FlightData(date=dep_date, from_airport="TLV", to_airport=code),
            FlightData(date=ret_date, from_airport=code,  to_airport="TLV"),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=2),
    )
    return result.flights


def run():
    dep_date, ret_date = get_trip_dates()
    ils_rate = fetch_ils_rate()

    print(f"\n{'='*60}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Scanning TLV -> Greek Islands")
    print(f"  Dates: {dep_date} / {ret_date}  |  Limit: ${PRICE_LIMIT_USD}/person  |  ILS rate: {ils_rate:.2f}")
    print(f"{'='*60}")

    sent     = load_sent()
    new_deals = 0

    for code, name in ISLANDS.items():
        print(f"\n  [{code}] {name} ...")
        try:
            flights = search_island(code, dep_date, ret_date)
        except Exception as e:
            print(f"    [warn] search failed: {e}")
            continue

        seen_this_run: set = set()

        for fl in flights:
            # Skip explore-mode rows with no airline or departure time
            if not fl.name or not fl.departure:
                continue

            # --- Price filter ---
            price_pp = parse_price_usd(fl.price, ils_rate)
            if price_pp is None or price_pp > PRICE_LIMIT_USD:
                continue

            # --- Outbound time filter: depart TLV before 12:00 ---
            out_min = dep_to_minutes(fl.departure)
            if out_min is None or out_min >= 12 * 60:
                continue

            # --- Duration filter: outbound leg < 5 hours ---
            dur_min = parse_duration_minutes(fl.duration)
            if dur_min is not None and dur_min >= 5 * 60:
                continue

            # Dedup within this run (library returns duplicates)
            fkey = (code, fl.name, fl.departure, fl.price)
            if fkey in seen_this_run:
                continue
            seen_this_run.add(fkey)

            # Dedup across runs
            did = deal_hash(code, fl.name, fl.departure, fl.price)
            if did in sent:
                print(f"    [skip] already sent: {fl.name} {fl.departure} {fl.price}")
                continue

            link = gflights_url(dep_date, ret_date, code)

            msg = (
                f"<b>Flight Deal Found!</b>\n\n"
                f"<b>Route:</b> TLV -> {name} ({code}) -> TLV\n"
                f"<b>Dates:</b> {dep_date} to {ret_date}\n"
                f"<b>Airline:</b> {fl.name}\n"
                f"<b>Outbound departs:</b> {fl.departure}\n"
                f"<b>Outbound arrives:</b> {fl.arrival}\n"
                f"<b>Outbound duration:</b> {fl.duration}  |  Stops: {fl.stops}\n"
                f"<b>Price:</b> ${price_pp:.0f}/person (round trip)\n"
                f"<b>Note:</b> Verify return departs island at 17:00 or later\n\n"
                f'<a href="{link}">Open prefilled search on Google Flights</a>'
            )

            try:
                send_telegram(msg)
                sent.add(did)
                save_sent(sent)
                new_deals += 1
                print(f"    [DEAL] {fl.name} dep={fl.departure} ${price_pp:.0f}/person -> sent")
            except Exception as e:
                print(f"    [error] Telegram failed: {e}")

        time.sleep(2)

    print(f"\nDone. {new_deals} new deal(s) sent. DB: {len(sent)} entries.")


if __name__ == "__main__":
    run()
