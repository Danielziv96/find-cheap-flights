"""
Flight price monitor v3: TLV -> Greek Islands
- Playwright headless Chromium scrapes real Google Flights results
- Sends specific booking links (tfs= URL, flight pre-selected) via Telegram
- Alerts when price < $230/person round trip
- Outbound: departs TLV before 12:00, duration < 5h
- Return:   departs island at 17:00 or later
- Never sends the same deal twice
"""

import os
import sys
import json
import re
import hashlib
import time
import random
import base64
import requests
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PRICE_LIMIT_USD  = 230
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

_STEALTH_JS = """
  Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
  Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});
  Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
  window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}};
"""


# ---------------------------------------------------------------------------
# Protobuf helpers for building a Google Flights search URL
# ---------------------------------------------------------------------------

def _varint(n: int) -> bytes:
    parts = []
    while n > 127:
        parts.append((n & 0x7F) | 0x80)
        n >>= 7
    parts.append(n)
    return bytes(parts)


def _fb(field_num: int, data: bytes) -> bytes:
    tag = (field_num << 3) | 2
    return _varint(tag) + _varint(len(data)) + data


def _leg(dep_date: str, frm: str, to: str) -> bytes:
    inner = _fb(2, dep_date.encode()) + _fb(13, _fb(2, frm.encode())) + _fb(14, _fb(2, to.encode()))
    return _fb(3, inner)


def build_search_url(dep_date: str, ret_date: str, dest: str) -> str:
    suffix = bytes.fromhex("420201014801980101")  # 2 adults, economy
    proto  = _leg(dep_date, "TLV", dest) + _leg(ret_date, dest, "TLV") + suffix
    tfs    = base64.urlsafe_b64encode(proto).decode().rstrip("=")
    return f"https://www.google.com/travel/flights?tfs={tfs}&hl=en&curr=USD"


def gflights_url(dep_date: str, ret_date: str, dest: str) -> str:
    """Fallback generic search link used when tfs= capture fails."""
    return (
        f"https://www.google.com/travel/flights"
        f"?hl=en#flt=TLV.{dest}.{dep_date}*{dest}.TLV.{ret_date}"
        f";c:USD;e:1;sd:1"
    )


# ---------------------------------------------------------------------------
# Date / price helpers
# ---------------------------------------------------------------------------

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


def parse_price_usd(fl: dict, ils_rate: float) -> float | None:
    """Handle both USD (GitHub Actions US IP) and ILS (local dev) prices."""
    if fl.get("price_usd") is not None:
        return fl["price_usd"] / 2  # per person (total is for 2 adults)
    if fl.get("price_ils"):
        digits = re.sub(r"[^\d.]", "", fl["price_ils"])
        if digits:
            return float(digits) / ils_rate / 2
    return None


def dep_to_minutes(dep_str: str) -> int | None:
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
    if not duration_str:
        return None
    h = re.search(r"(\d+)\s*hr?", duration_str, re.IGNORECASE)
    m = re.search(r"(\d+)\s*m(?:in)?", duration_str, re.IGNORECASE)
    hours = int(h.group(1)) if h else 0
    mins  = int(m.group(1)) if m else 0
    total = hours * 60 + mins
    return total if total > 0 else None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text: str):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    ).raise_for_status()


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deal_hash(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def load_sent() -> set:
    try:
        return set(json.loads(SENT_DB.read_text()))
    except Exception:
        return set()


def save_sent(sent: set):
    SENT_DB.write_text(json.dumps(sorted(sent), indent=2))


# ---------------------------------------------------------------------------
# Playwright scraping
# ---------------------------------------------------------------------------

def parse_flight_label(label: str) -> dict | None:
    """Extract structured flight data from a Google Flights result aria-label."""
    airline = re.match(r'^([^.]+)\.', label)
    dep     = re.search(r'at (\d+:\d+\s+(?:AM|PM))\s+on', label)
    arr     = re.search(r'arrives\s+(?:at\s+\S+\s+Airport\s+)?at\s+(\d+:\d+\s+(?:AM|PM))', label, re.I)
    dur     = re.search(r'Total duration\s+([\d\s\w]+?)\.', label)
    stops   = re.search(r'\.\s+(Nonstop|\d+\s+stops?)\s*\.', label)
    price_u = re.search(r'From\s+([\d,]+)\s+US dollars', label)
    price_i = re.search(r'From\s+([\d,]+)\s+Israeli shekel', label)
    if not (airline and dep):
        return None
    return {
        "name":      airline.group(1).strip(),
        "departure": dep.group(1),
        "arrival":   arr.group(1) if arr else "",
        "duration":  dur.group(1).strip() if dur else "",
        "stops":     stops.group(1) if stops else "",
        "price_usd": float(price_u.group(1).replace(",", "")) if price_u else None,
        "price_ils": price_i.group(1).replace(",", "") if price_i else None,
    }


def search_island_pw(page, code: str, dep_date: str, ret_date: str) -> list[dict]:
    url = build_search_url(dep_date, ret_date, code)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except PlaywrightTimeoutError:
        raise Exception("page load timeout")

    # Handle Google consent screen (first run from a new IP)
    if "consent.google.com" in page.url:
        try:
            page.click("button:has-text('Accept all')", timeout=6_000)
            page.wait_for_load_state("domcontentloaded")
        except PlaywrightTimeoutError:
            pass

    if "unusual traffic" in page.content().lower():
        raise Exception("bot detection triggered")

    try:
        page.wait_for_selector("ul[role='list'] li[aria-label]", timeout=45_000)
    except PlaywrightTimeoutError:
        return []

    time.sleep(random.uniform(1.5, 3.0))

    cards = page.evaluate(
        "() => [...document.querySelectorAll(\"ul[role='list'] li[aria-label]\")]"
        ".map((li, i) => ({idx: i, label: li.getAttribute('aria-label') || ''}))"
    )

    flights = []
    for c in cards:
        fl = parse_flight_label(c["label"])
        if fl:
            fl["_idx"] = c["idx"]
            flights.append(fl)
    return flights


def capture_booking_url(page, card_idx: int, dep_date: str, ret_date: str, dest: str) -> str | None:
    """Click a flight card and return its tfs= booking URL, or None on failure."""
    try:
        cards = page.query_selector_all("ul[role='list'] li[aria-label]")
        if card_idx >= len(cards):
            return None
        card = cards[card_idx]
        card.scroll_into_view_if_needed()
        card.click()
        time.sleep(1.0)

        # Best case: a tfs= anchor appeared without full navigation
        link = page.query_selector("a[href*='tfs=']")
        if link:
            href = link.get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://www.google.com" + href
            if "tfs=" in href:
                return href

        # Fallback: find and click the "Select" button, capture resulting URL
        select = (
            page.query_selector("button:has-text('Select this flight')") or
            page.query_selector("[aria-label*='Select departure']") or
            page.query_selector("button:has-text('Select')")
        )
        if select:
            with page.expect_navigation(url="**/travel/flights**tfs=**", timeout=12_000):
                select.click()
            booking_url = page.url
            # Return to search results for the next check
            page.goto(build_search_url(dep_date, ret_date, dest),
                      wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_selector("ul[role='list'] li[aria-label]", timeout=30_000)
            except PlaywrightTimeoutError:
                pass
            return booking_url

    except Exception as e:
        print(f"    [warn] tfs= capture failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------

def run():
    dep_date, ret_date = get_trip_dates()
    ils_rate = fetch_ils_rate()

    print(f"\n{'='*60}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Scanning TLV -> Greek Islands")
    print(f"  Dates: {dep_date} / {ret_date}  |  Limit: ${PRICE_LIMIT_USD}/person  |  ILS rate: {ils_rate:.2f}")
    print(f"{'='*60}")

    sent      = load_sent()
    new_deals = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--no-first-run",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = context.new_page()
        page.add_init_script(_STEALTH_JS)

        for code, name in ISLANDS.items():
            print(f"\n  [{code}] {name} ...")
            try:
                flights = search_island_pw(page, code, dep_date, ret_date)
            except Exception as e:
                print(f"    [warn] search failed: {e}")
                time.sleep(random.uniform(3, 6))
                continue

            print(f"    found {len(flights)} result(s)")
            seen_this_run: set = set()

            for fl in flights:
                if not fl["name"] or not fl["departure"]:
                    continue

                # --- Price filter ---
                price_pp = parse_price_usd(fl, ils_rate)
                if price_pp is None or price_pp > PRICE_LIMIT_USD:
                    continue

                # --- Outbound time filter: depart TLV before 12:00 ---
                out_min = dep_to_minutes(fl["departure"])
                if out_min is None or out_min >= 12 * 60:
                    continue

                # --- Duration filter: outbound < 5 hours ---
                dur_min = parse_duration_minutes(fl["duration"])
                if dur_min is not None and dur_min >= 5 * 60:
                    continue

                # --- Dedup within run ---
                fkey = (code, fl["name"], fl["departure"], fl.get("price_usd") or fl.get("price_ils"))
                if fkey in seen_this_run:
                    continue
                seen_this_run.add(fkey)

                # --- Dedup across runs ---
                did = deal_hash(code, fl["name"], fl["departure"], price_pp)
                if did in sent:
                    print(f"    [skip] already sent: {fl['name']} {fl['departure']} ${price_pp:.0f}")
                    continue

                # --- Capture specific booking URL ---
                booking_url = capture_booking_url(page, fl["_idx"], dep_date, ret_date, code)
                link = booking_url or gflights_url(dep_date, ret_date, code)
                link_label = "Open this flight on Google Flights" if booking_url else "Search this route on Google Flights"

                msg = (
                    f"<b>Flight Deal Found!</b>\n\n"
                    f"<b>Route:</b> TLV -> {name} ({code}) -> TLV\n"
                    f"<b>Dates:</b> {dep_date} to {ret_date}\n"
                    f"<b>Airline:</b> {fl['name']}\n"
                    f"<b>Outbound departs:</b> {fl['departure']}\n"
                    f"<b>Outbound arrives:</b> {fl['arrival']}\n"
                    f"<b>Outbound duration:</b> {fl['duration']}  |  Stops: {fl['stops']}\n"
                    f"<b>Price:</b> ${price_pp:.0f}/person (round trip)\n"
                    f"<b>Note:</b> Verify return departs island at 17:00 or later\n\n"
                    f'<a href="{link}">{link_label}</a>'
                )

                try:
                    send_telegram(msg)
                    sent.add(did)
                    save_sent(sent)
                    new_deals += 1
                    url_type = "tfs=" if booking_url else "generic"
                    print(f"    [DEAL] {fl['name']} dep={fl['departure']} ${price_pp:.0f}/person -> sent ({url_type})")
                except Exception as e:
                    print(f"    [error] Telegram failed: {e}")

            # Delay between islands to avoid burst pattern
            time.sleep(random.uniform(4, 9))

        context.close()
        browser.close()

    print(f"\nDone. {new_deals} new deal(s) sent. DB: {len(sent)} entries.")


if __name__ == "__main__":
    run()
