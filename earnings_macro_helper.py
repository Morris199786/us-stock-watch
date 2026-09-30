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

        if tag == "tr" and any(
            c == "calendar__row" or c.startswith("calendar__row")
            for c in classes
        ):
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

            key = None
            for cls in self.current_classes:
                if cls.startswith("calendar__"):
                    key = cls
                    break

            if key:
                self.row[key] = text

            self.in_td = False
            self.current_classes = set()
            self.buf = []

        if self.in_row and tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.in_row = False
            self.row = {}


def _ff_direct_html(day_et):
    token = day_et.strftime("%b%d.%Y").lower()
    urls = [
        f"https://www.forexfactory.com/calendar?day={token}",
        f"https://www.forexfactory.com/calendar?day={token}&embed=true",
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.forexfactory.com/calendar",
        "Cache-Control": "no-cache",
        "Cookie": "fftimezone=America/New_York",
    }

    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=18) as r:
                raw = r.read().decode("utf-8", "ignore")

            if len(raw) > 5000:
                return raw
        except Exception as e:
            print("FF direct fetch failed:", type(e).__name__, url)

    return ""


def _ff_jina_text(day_et):
    token = day_et.strftime("%b%d.%Y").lower()
    targets = [
        f"https://r.jina.ai/http://www.forexfactory.com/calendar?day={token}",
        f"https://r.jina.ai/https://www.forexfactory.com/calendar?day={token}",
    ]

    for url in targets:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/plain,text/markdown,*/*",
                },
            )

            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode("utf-8", "ignore")

            if len(raw) > 1000:
                return raw

        except Exception as e:
            print("FF Jina fetch failed:", type(e).__name__, url)

    return ""


def _row_value(row, suffix):
    for k, v in row.items():
        if k == suffix or k.endswith(suffix):
            return _clean_text(v)
    return ""


def _parse_ff_html(html_text):
    if not html_text:
        return {}

    parser = _FFCalendarParser()

    try:
        parser.feed(html_text)
    except Exception:
        return {}

    out = {}

    for row in parser.rows:
        title = _row_value(row, "calendar__event")

        if not title:
            continue

        currency = _row_value(row, "calendar__currency").upper()

        out[title.lower()] = {
            "currency": currency,
            "actual": _row_value(row, "calendar__actual"),
            "forecast": _row_value(row, "calendar__forecast"),
            "previous": _row_value(row, "calendar__previous"),
        }

    return out


def _looks_numberish(value):
    s = str(value or "").strip()

    if not s:
        return False

    return bool(
        re.fullmatch(
            r"[-+]?[\d,.]+(?:\s?[KMBT])?%?(?:\|[-+]?[\d,.]+)?",
            s,
            flags=re.I,
        )
    )


def _parse_ff_markdown(text):
    if not text:
        return {}

    out = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if "| USD" not in line and "|USD" not in line:
            continue

        parts = [
            re.sub(r"\s+", " ", p).strip()
            for p in line.split("|")
        ]

        clean = []

        for p in parts:
            if not p:
                continue

            if p.lower() in {
                "usd",
                "image",
                "graph",
                "detail",
                "alerts",
            }:
                continue

            if p.startswith("[") and "Image" in p:
                continue

            p = re.sub(r"\[Image[^\]]*\]", "", p).strip()

            if p:
                clean.append(p)

        if not clean:
            continue

        # Search for known economic-event-looking text.
        # The event title is usually the last non-numberish textual field
        # before Actual / Forecast / Previous.
        title_idx = None

        for i, value in enumerate(clean):
            low = value.lower()

            if (
                not _looks_numberish(value)
                and not re.fullmatch(r"\d{1,2}:\d{2}(?:am|pm)?", low)
                and not re.fullmatch(r"(?:mon|tue|wed|thu|fri|sat|sun).*", low)
                and any(ch.isalpha() for ch in value)
            ):
                title_idx = i

        if title_idx is None:
            continue

        title = clean[title_idx]

        nums = [
            x for x in clean[title_idx + 1:]
            if _looks_numberish(x)
        ]

        if not nums:
            continue

        actual = nums[0] if len(nums) >= 1 else ""
        forecast = nums[1] if len(nums) >= 2 else ""
        previous = nums[2] if len(nums) >= 3 else ""

        out[title.lower()] = {
            "currency": "USD",
            "actual": actual,
            "forecast": forecast,
            "previous": previous,
        }

    return out


def _ff_web_calendar_day(day_et):
    # 1) Direct Forex Factory HTML
    html_text = _ff_direct_html(day_et)
    out = _parse_ff_html(html_text)

    if out:
        print(
            "FF direct parsed:",
            day_et.isoformat(),
            len(out),
            "rows",
        )
        return out

    # 2) Jina rendered-text fallback
    jina = _ff_jina_text(day_et)
    out = _parse_ff_markdown(jina)

    if out:
        print(
            "FF Jina parsed:",
            day_et.isoformat(),
            len(out),
            "rows",
        )
        return out

    print("FF history parse returned zero rows:", day_et.isoformat())
    return {}


def backfill_recent_macro_actuals(days=7):
    macro = _load_json(MACRO_FILE, {})
    events = macro.get("events") or []

    if not events:
        print("macro.json has no events")
        return False

    now_utc = datetime.now(timezone.utc)
    et = ZoneInfo("America/New_York")
    page_cache = {}

    def norm(v):
        return re.sub(
            r"[^a-z0-9]+",
            " ",
            str(v or "").lower(),
        ).strip()

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
        day_et = dt.astimezone(et).date()

        if day_et not in page_cache:
            page_cache[day_et] = _ff_web_calendar_day(day_et)

        web = page_cache.get(day_et) or {}
        matched = web.get(title.lower())

        if not matched:
            wanted = norm(title)

            for k, v in web.items():
                if norm(k) == wanted:
                    matched = v
                    break

        if not matched:
            print("No FF historical match:", title, day_et.isoformat())
            continue

        actual = str(matched.get("actual") or "").strip()

        if not actual:
            print("FF matched but Actual blank:", title)
            continue

        before = (
            event.get("actual"),
            event.get("forecast"),
            event.get("previous"),
        )

        event["actual"] = actual

        if matched.get("forecast"):
            event["forecast"] = matched["forecast"]

        if matched.get("previous"):
            event["previous"] = matched["previous"]

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
    # GitHub Actions 可能延遲數分鐘，因此 08:30～08:59 都可以觸發
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
    sent_days = set(
        state.get("earningsReminderDays") or []
    )

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

        name = str(
            x.get("name")
            or ""
        ).strip()

        if not ticker:
            continue

        label = ticker

        if (
            name
            and name.lower() != ticker.lower()
        ):
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

    lines = [
        f"{target_tw_date.strftime('%m/%d')} 財報提醒"
    ]

    if pre:
        lines.append(
            "盤前｜" + "、".join(pre)
        )

    if post:
        lines.append(
            "盤後｜" + "、".join(post)
        )

    ok, reason = _send_telegram(
        "明日美股財報",
        "\n".join(lines),
        url=SITE_EARNINGS_URL,
    )

    print("Telegram earnings reminder:", ok, reason)

    if ok:
        sent_days.add(target_key)

        state["earningsReminderDays"] = sorted(
            sent_days
        )[-120:]

        state["updatedAt"] = datetime.now(
            timezone.utc
        ).isoformat()

        _save_json(
            PUSH_STATE_FILE,
            state,
        )

    return ok


def main():
    backfill_recent_macro_actuals(days=7)
    send_next_day_earnings_reminder()


if __name__ == "__main__":
    main()
