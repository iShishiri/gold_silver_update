"""Fetch today's official Nepal gold/silver rate (per tola) and send it on WhatsApp.

Source: FENEGOSIDA public feed (rates are posted ~10:30 AM Nepal time).
The script waits until the feed shows TODAY's date, so the price is never stale.

Environment variables
  RECIPIENTS          one "phone:apikey" per line (or comma separated), e.g.
                        +9779800000000:123456
                        +9779811111111:654321
  GREEN_API_ID        (optional) Green API idInstance (numbers only)
  GREEN_API_TOKEN     (optional) Green API apiTokenInstance
  GREEN_API_URL       (optional) your instance's apiUrl, default https://api.green-api.com
  GREEN_API_CHATS     (optional) one per line/comma: a phone number with country
                      code (e.g. 9779812345678) or a group id ending in @g.us
  TELEGRAM_BOT_TOKEN  (optional) Telegram bot token
  TELEGRAM_CHAT_IDS   (optional) comma/newline separated chat or channel ids
  TEST_MODE           (optional) "true" = send immediately, labelled [TEST]
  MAX_WAIT_MIN        (optional) minutes to wait for today's rate, default 60
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_URL = "https://api.fenegosida.org/api/website/v1/Dashboard/today"
NPT = timezone(timedelta(hours=5, minutes=45))
UA = {"User-Agent": "Mozilla/5.0 (gold-silver-alert)"}


def http_get(url, timeout=30, retries=1):
    """GET with retry on transient failures (5xx, 429, timeouts, connection errors).

    Keep retries=1 for anything with side effects (sending messages) so a retry can't duplicate it.
    """
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            transient = e.code >= 500 or e.code == 429
            if not transient or attempt == retries:
                raise
            reason = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == retries:
                raise
            reason = str(e)
        delay = 10 * attempt
        print(f"GET failed ({reason}), retry {attempt}/{retries - 1} in {delay}s...", flush=True)
        time.sleep(delay)


def fetch_rates():
    try:
        rows = json.loads(http_get(API_URL, retries=3))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Feed returned invalid JSON: {e}") from e
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Feed returned no rate rows: {str(rows)[:200]}")
    out = {}
    for row in rows:
        try:
            out.update(parse_row(row))
        except (KeyError, TypeError, ValueError) as e:
            raise RuntimeError(f"Unexpected feed row ({e!r}): {row}") from e
    if "gold" not in out or "silver" not in out:
        raise RuntimeError(f"Unexpected feed format: {rows}")
    return out


def parse_row(row):
    """Return {'gold'|'silver': {...}} for a per-tola row, or {} for any other row."""
    name = row["rateType"]
    if "१ तोला" not in name:
        return {}
    if "सुन" in name:
        key = "gold"
    elif "चाँदी" in name:
        key = "silver"
    else:
        return {}
    today = float(row["todayBaseRatePerGram"])
    if today <= 0:
        raise ValueError(f"non-positive rate {today}")
    return {key: {
        "today": today,
        "yesterday": float(row["yestardayBaseRatePerGram"]),
        "date": datetime.fromisoformat(row["todayDate"]).astimezone(NPT).date(),
    }}


def wait_for_today(max_wait_min):
    """Poll until the feed's date is today's Nepal date (or time runs out)."""
    deadline = time.time() + max_wait_min * 60
    while True:
        today = datetime.now(NPT).date()
        try:
            rates = fetch_rates()
        except Exception as e:  # feed down (e.g. HTTP 521) or malformed: keep retrying until the deadline
            if time.time() >= deadline:
                raise RuntimeError(f"Feed unavailable until the deadline: {e}") from e
            print(f"Feed error ({e}), retrying in 5 min...", flush=True)
            time.sleep(300)
            continue
        if rates["gold"]["date"] == today and rates["silver"]["date"] == today:
            return rates
        if time.time() >= deadline:
            raise RuntimeError(
                f"Feed still shows {rates['gold']['date']}, expected {today}. Not sending a stale price."
            )
        print("Today's rate not posted yet, retrying in 5 min...", flush=True)
        time.sleep(300)


def fmt_line(label, d):
    diff = d["today"] - d["yesterday"]
    pct = diff / d["yesterday"] * 100 if d["yesterday"] else 0
    arrow = "▲" if diff > 0 else "▼" if diff < 0 else "▬"
    change = "no change" if diff == 0 else f"{arrow} {abs(diff):,.0f} ({pct:+.2f}%)"
    return f"{label}: Rs {d['today']:,.0f} /tola  {change}"


def build_message(rates):
    date = rates["gold"]["date"].strftime("%d %b %Y")
    return "\n".join([
        f"Gold & Silver Rate - {date}",
        fmt_line("Gold (hallmark)", rates["gold"]),
        fmt_line("Silver", rates["silver"]),
        "Source: FENEGOSIDA",
    ])


def parse_recipients(raw):
    pairs = []
    for item in re.split(r"[\n,;]+", raw or ""):
        item = item.strip()
        if not item:
            continue
        phone, _, key = item.partition(":")
        phone, key = phone.strip(), key.strip()
        if not phone or not key:
            print(f"Skipping malformed recipient line: {item!r}")
            continue
        pairs.append((phone, key))
    return pairs


def send_whatsapp(phone, key, text):
    qs = urllib.parse.urlencode({"phone": phone, "text": text, "apikey": key})
    body = http_get("https://api.callmebot.com/whatsapp.php?" + qs, timeout=60)
    print(f"WhatsApp {phone[:6]}***: sent (response: {re.sub(r'<[^>]+>', ' ', body).strip()[:80]})")


def send_green_api(base_url, instance_id, token, chat, text):
    chat = chat.strip().lstrip("+")
    if "@" not in chat:
        chat += "@c.us"
    url = f"{base_url.rstrip('/')}/waInstance{instance_id}/sendMessage/{token}"
    payload = json.dumps({"chatId": chat, "message": text}).encode()
    req = urllib.request.Request(url, data=payload, headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode("utf-8")
    print(f"Green API {chat[:8]}***: sent ({body[:60]})")


def send_telegram(token, chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    print(f"Telegram {chat_id}: sent")


def main():
    test_mode = os.environ.get("TEST_MODE", "").strip().lower() == "true"
    try:
        if test_mode:
            rates = fetch_rates()  # no waiting: send whatever the feed has right now
            text = "[TEST - may be previous day's rate]\n" + build_message(rates)
        else:
            rates = wait_for_today(int(os.environ.get("MAX_WAIT_MIN", "60")))
            text = build_message(rates)
    except Exception as e:
        # Nothing was sent. Exit non-zero so the workflow's later scheduled attempt can retry.
        print(f"ERROR: could not get today's rate: {e}", file=sys.stderr)
        sys.exit(1)
    print(text, "\n")

    recipients = parse_recipients(os.environ.get("RECIPIENTS", ""))
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chats = [c.strip() for c in re.split(r"[\n,;]+", os.environ.get("TELEGRAM_CHAT_IDS", "")) if c.strip()]

    ga_id = os.environ.get("GREEN_API_ID", "").strip()
    ga_token = os.environ.get("GREEN_API_TOKEN", "").strip()
    ga_url = os.environ.get("GREEN_API_URL", "").strip() or "https://api.green-api.com"
    ga_chats = [c.strip() for c in re.split(r"[\n,;]+", os.environ.get("GREEN_API_CHATS", "")) if c.strip()]
    use_green = bool(ga_id and ga_token and ga_chats)

    if not recipients and not use_green and not (tg_token and tg_chats):
        print("No recipients configured - dry run only.")
        return

    failures = 0
    for phone, key in recipients:
        try:
            send_whatsapp(phone, key, text)
        except Exception as e:  # keep going so one bad key doesn't block the others
            failures += 1
            print(f"WhatsApp {phone[:6]}*** FAILED: {e}")
        time.sleep(2)
    if use_green:
        for chat in ga_chats:
            try:
                send_green_api(ga_url, ga_id, ga_token, chat, text)
            except Exception as e:
                failures += 1
                print(f"Green API {chat[:8]}*** FAILED: {e}")
            time.sleep(2)
    if tg_token:
        for chat in tg_chats:
            try:
                send_telegram(tg_token, chat, text)
            except Exception as e:
                failures += 1
                print(f"Telegram {chat} FAILED: {e}")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
