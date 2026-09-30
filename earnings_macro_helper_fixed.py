import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
EARNINGS_FILE = ROOT / "earnings.json"
MACRO_FILE = ROOT / "macro.json"
PUSH_STATE_FILE = ROOT / "push_state.json"

SITE_EARNINGS_URL = "https://morris199786.github.io/us-stock-watch/?page=earnings"
FF_THIS_WEEK_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT_WEEK_JSON = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"


def _load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fetch_text(url, timeout=20, headers=None):
    h = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    if headers:
        h.update(headers)

    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _send_telegram(title, message, url=None):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()

    if not token or not chat_id:
        return False, "missing_telegram_secrets"

    text = f"{title}\n{message}".strip()
    if url:
        text += f"\n{url}"

    try:
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": text[:3900],
                "disable_web_page_preview": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "us-stock-watch/1.0",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=12) as r:
            ok = 200 <= getattr(r, "status", 200) < 300

        return ok, "ok" if ok else "http_error"

    except Exception as e:
        return False, type(e).__name__


def _clean_text(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _norm_title(value):
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").lower(),
    ).strip()


def _num_token(value):
    s = str(value or "").strip()
    s = re.sub(r"(?i)image.*$", "", s).strip()
    s = s.replace(" ", "")

    if re.fullmatch(
        r"[-+]?\d[\d,.]*(?:\|[-+]?\d[\d,.]*)?(?:[KMBT])?%?",
        s,
        flags=re.I,
    ):
        return s
    return ""


def _ff_json_rows():
    rows = []

    for url in (FF_THIS_WEEK_JSON, FF_NEXT_WEEK_JSON):
        try:
            raw = _fetch_text(
                url,
                timeout=15,
                headers={
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": "https://www.forexfactory.com/calendar",
                },
            )
            data = json.loads(raw)
            if isinstance(data, list):
                rows.extend(data)
        except Exception as e:
            print("FF JSON fetch failed:", type(e).__name__, url)

    return rows


def _ff_json_lookup(title, event_dt_utc):
    wanted = _norm_title(title)

    for row in _ff_json_rows():
        if _norm_title(row.get("title")) != wanted:
            continue

        try:
            raw = str(row.get("date") or "").strip()
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
            dt = dt.astimezone(timezone.utc)
        except Exception:
            dt = None

        if dt and abs((dt - event_dt_utc).total_seconds()) > 6 * 3600:
            continue

        actual = str(row.get("actual") or "").strip()
        forecast = str(row.get("forecast") or "").strip()
        previous = str(row.get("previous") or "").strip()

        if actual:
            return {
                "actual": actual,
                "forecast": forecast,
                "previous": previous,
                "source": "ff_json",
            }

    return None


class _FFCalendarParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_row = False
        self.in_td = False
        self.row = {}
        self.rows = []
        self.current_classes = set()
        self.buf = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())

        if tag == "tr" and any("calendar__row" in c for c in classes):
            self.in_row = True
            self.row = {}

        if self.in_row and tag == "td":
            self.in_td = True
            self.current_classes = classes
            self.buf = []

    def handle_data(self, data):
        if self.in_row and self.in_td:
            self.buf.append(data)

    def handle_endtag(self, tag):
        if self.in_row and self.in_td and tag == "td":
            text = re.sub(r"\s+", " ", " ".join(self.buf)).strip()

            for cls in self.current_classes:
                if cls.startswith("calendar__"):
                    self.row[cls] = text

            self.in_td = False
            self.current_classes = set()
            self.buf = []

        if self.in_row and tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.in_row = False
            self.row = {}


def _row_value(row, suffix):
    for k, v in row.items():
        if k == suffix or k.endswith(suffix):
            return _clean_text(v)
    return ""


def _parse_direct_html_for_title(html_text, title):
    if not html_text:
        return None

    parser = _FFCalendarParser()

    try:
        parser.feed(html_text)
    except Exception:
        return None

    wanted = _norm_title(title)

    for row in parser.rows:
        row_title = _row_value(row, "calendar__event")

        if _norm_title(row_title) != wanted:
            continue

        actual = _row_value(row, "calendar__actual")
        forecast = _row_value(row, "calendar__forecast")
        previous = _row_value(row, "calendar__previous")

        if actual:
            return {
                "actual": actual,
                "forecast": forecast,
                "previous": previous,
                "source": "ff_direct_html",
            }

    return None


def _extract_event_id(text, title):
    if not text:
        return ""

    pos = text.lower().find(str(title).lower())
    if pos < 0:
        return ""

    window = text[max(0, pos - 600):pos + 900]

    m = re.search(r"[?&]event=(\d+)", window, flags=re.I)
    return m.group(1) if m else ""


def _extract_values_near_title(text, title):
    """
    Flexible text parser for Jina/markdown output.
    It does NOT assume one event equals one physical line.
    It searches after the exact title and accepts Actual only if
    at least 3 numeric fields are present: Actual, Forecast, Previous.
    """
    if not text:
        return None

    low = text.lower()
    needle = str(title or "").lower()
    pos = low.find(needle)

    if pos < 0:
        return None

    window = text[pos + len(title):pos + len(title) + 700]

    # Stop before the next obvious currency-event row when possible.
    stop = re.search(
        r"\n\s*(?:\d{1,2}:\d{2}(?:am|pm)?\s*)?\|\s*(?:USD|EUR|GBP|JPY|CAD|AUD|NZD|CHF|CNY)\s*\|",
        window,
        flags=re.I,
    )
    if stop:
        window = window[:stop.start()]

    # Remove markdown images/links noise but keep values.
    window = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", window)
    window = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", window)
    window = window.replace("\n", " | ")

    raw_parts = [x.strip() for x in window.split("|") if x.strip()]
    nums = []

    for part in raw_parts:
        val = _num_token(part)
        if val:
            nums.append(val)

    # Critical safeguard:
    # 2 values means Forecast + Previous and Actual is still blank.
    # Only 3+ values means Actual is present.
    if len(nums) < 3:
        return None

    return {
        "actual": nums[0],
        "forecast": nums[1],
        "previous": nums[2],
        "source": "ff_jina_text",
    }


def _ff_day_direct_text(day_et):
    token = day_et.strftime("%b%d.%Y").lower()
    urls = [
        f"https://www.forexfactory.com/calendar?day={token}",
        f"https://www.forexfactory.com/calendar?day={token}&embed=true",
    ]

    for url in urls:
        try:
            raw = _fetch_text(
                url,
                timeout=18,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": "https://www.forexfactory.com/calendar",
                    "Cookie": "fftimezone=America/New_York",
                },
            )
            if len(raw) > 5000:
                return raw
        except Exception as e:
            print("FF direct day fetch failed:", type(e).__name__)

    return ""


def _ff_jina_text(url):
    targets = [
        "https://r.jina.ai/https://" + url.removeprefix("https://"),
        "https://r.jina.ai/http://" + url.removeprefix("https://"),
    ]

    for target in targets:
        try:
            raw = _fetch_text(
                target,
                timeout=25,
                headers={"Accept": "text/plain,text/markdown,*/*"},
            )
            if len(raw) > 800:
                return raw
        except Exception as e:
            print("FF Jina fetch failed:", type(e).__name__)

    return ""


def _ff_historical_lookup(title, event_dt_utc):
    et = ZoneInfo("America/New_York")
    day_et = event_dt_utc.astimezone(et).date()
    token = day_et.strftime("%b%d.%Y").lower()
    day_url = f"https://www.forexfactory.com/calendar?day={token}"

    # 1) Live weekly JSON first
    hit = _ff_json_lookup(title, event_dt_utc)
    if hit:
        return hit

    # 2) Direct HTML day page
    html_text = _ff_day_direct_text(day_et)
    hit = _parse_direct_html_for_title(html_text, title)
    if hit:
        return hit

    # 3) Jina-rendered day page, exact-title window parsing
    day_text = _ff_jina_text(day_url)
    hit = _extract_values_near_title(day_text, title)
    if hit:
        return hit

    # 4) Event-specific page.
    # This solves FF stale day-page snapshots where the day page still has
    # blank Actual but ?event=NNNNNN already contains the released value.
    event_id = _extract_event_id(day_text, title)

    if not event_id and html_text:
        event_id = _extract_event_id(html_text, title)

    if event_id:
        event_url = f"{day_url}&event={event_id}"
        event_text = _ff_jina_text(event_url)
        hit = _extract_values_near_title(event_text, title)

        if hit:
            hit["source"] = "ff_event_page"
            return hit

        try:
            event_html = _fetch_text(
                event_url,
                timeout=18,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": day_url,
                },
            )
        except Exception:
            event_html = ""

        hit = _parse_direct_html_for_title(event_html, title)
        if hit:
            hit["source"] = "ff_event_page_direct"
            return hit

    print(
        "FF historical lookup failed:",
        title,
        day_et.isoformat(),
        "event_id=",
        event_id or "none",
    )
    return None


def backfill_recent_macro_actuals(days=7):
    macro = _load_json(MACRO_FILE, {})
    events = macro.get("events") or []

    if not events:
        print("macro.json has no events")
        return False

    now_utc = datetime.now(timezone.utc)
    changed = False
    candidates = 0
    filled = 0

    for event in events:
        if not isinstance(event, dict):
            continue

        if str(event.get("actual") or "").strip():
            continue

        title = str(event.get("title") or "").strip()
        if not title:
            continue

        low = title.lower()
        if any(
            key in low
            for key in (
                "speaks",
                "speech",
                "press conference",
                "testifies",
            )
        ):
            continue

        try:
            dt = datetime.fromisoformat(
                str(event.get("eventTimeUtc") or "").replace("Z", "+00:00")
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        except Exception:
            continue

        if dt > now_utc + timedelta(minutes=2):
            continue

        if dt < now_utc - timedelta(days=days):
            continue

        candidates += 1
        hit = _ff_historical_lookup(title, dt)

        if not hit:
            continue

        actual = str(hit.get("actual") or "").strip()
        if not actual:
            continue

        before = (
            event.get("actual"),
            event.get("forecast"),
            event.get("previous"),
        )

        event["actual"] = actual

        if hit.get("forecast"):
            event["forecast"] = hit["forecast"]

        if hit.get("previous"):
            event["previous"] = hit["previous"]

        event["actualBackfillSource"] = hit.get("source") or "Forex Factory"

        after = (
            event.get("actual"),
            event.get("forecast"),
            event.get("previous"),
        )

        if after != before:
            changed = True
            filled += 1
            print(
                "macro backfilled:",
                title,
                "| actual:",
                event.get("actual"),
                "| forecast:",
                event.get("forecast"),
                "| previous:",
                event.get("previous"),
                "| source:",
                event.get("actualBackfillSource"),
            )

    print(
        "Macro backfill summary:",
        "candidates=",
        candidates,
        "filled=",
        filled,
    )

    if changed:
        macro["updatedAt"] = datetime.now(
            ZoneInfo("Asia/Taipei")
        ).strftime("%Y-%m-%d %H:%M 台灣時間")
        _save_json(MACRO_FILE, macro)

    return changed


def send_next_day_earnings_reminder():
    tw = ZoneInfo("Asia/Taipei")
    et = ZoneInfo("America/New_York")
    now_tw = datetime.now(timezone.utc).astimezone(tw)

    # 財報提醒固定台灣時間 08:30
    # GitHub Actions 可能延遲數分鐘，因此 08:30～08:59 都可觸發
    if not (
        now_tw.hour == 8
        and now_tw.minute >= 30
    ):
        print("Not earnings reminder time")
        return False

    earnings = _load_json(EARNINGS_FILE, {})
    upcoming = earnings.get("upcoming") or []
    target_tw_date = now_tw.date() + timedelta(days=1)

    state = _load_json(PUSH_STATE_FILE, {})
    sent_days = set(state.get("earningsReminderDays") or [])
    target_key = target_tw_date.isoformat()

    if target_key in sent_days:
        print("Earnings reminder already sent:", target_key)
        return False

    pre = []
    post = []

    for x in upcoming:
        try:
            dt = datetime.fromisoformat(
                str(x.get("date") or "").replace("Z", "+00:00")
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        except Exception:
            continue

        dt_tw = dt.astimezone(tw)

        if dt_tw.date() != target_tw_date:
            continue

        dt_et = dt.astimezone(et)

        ticker = str(
            x.get("ticker")
            or x.get("symbol")
            or ""
        ).strip()

        name = str(x.get("name") or "").strip()

        if not ticker:
            continue

        label = ticker

        if name and name.lower() != ticker.lower():
            label += f"（{name}）"

        if dt_et.hour >= 16:
            post.append(label)
        elif 4 <= dt_et.hour < 10:
            pre.append(label)
        elif dt_tw.hour < 12:
            post.append(label)
        else:
            pre.append(label)

    if not pre and not post:
        print("No earnings for next Taiwan date:", target_key)
        return False

    lines = [f"{target_tw_date.strftime('%m/%d')} 財報提醒"]

    if pre:
        lines.append("盤前｜" + "、".join(pre))

    if post:
        lines.append("盤後｜" + "、".join(post))

    ok, reason = _send_telegram(
        "明日美股財報",
        "\n".join(lines),
        url=SITE_EARNINGS_URL,
    )

    print("Telegram earnings reminder:", ok, reason)

    if ok:
        sent_days.add(target_key)
        state["earningsReminderDays"] = sorted(sent_days)[-120:]
        state["updatedAt"] = datetime.now(timezone.utc).isoformat()
        _save_json(PUSH_STATE_FILE, state)

    return ok


def main():
    backfill_recent_macro_actuals(days=7)
    send_next_day_earnings_reminder()


if __name__ == "__main__":
    main()
