import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
EARNINGS_FILE = ROOT / "earnings.json"
MACRO_FILE = ROOT / "macro.json"
PUSH_STATE_FILE = ROOT / "push_state.json"

SITE_EARNINGS_URL = "https://morris199786.github.io/us-stock-watch/?page=earnings"


def _load_json(path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
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


def _ff_clean_html_text(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _ff_web_calendar_day(day_et):
    try:
        token = day_et.strftime("%b%d.%Y").lower()

        req = urllib.request.Request(
            f"https://www.forexfactory.com/calendar?day={token}",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.forexfactory.com/calendar",
            },
        )

        with urllib.request.urlopen(req, timeout=15) as r:
            html_text = r.read().decode("utf-8", "ignore")

    except Exception:
        return {}

    rows = re.findall(
        r'(<tr[^>]*class="[^"]*calendar__row[^"]*"[^>]*>.*?</tr>)',
        html_text,
        flags=re.I | re.S,
    )

    out = {}

    for row in rows:
        m = re.search(
            r'class="[^"]*calendar__event[^"]*"[^>]*>(.*?)</td>',
            row,
            flags=re.I | re.S,
        )

        if not m:
            continue

        title = _ff_clean_html_text(m.group(1))

        if not title:
            continue

        def cell(cls):
            mm = re.search(
                rf'class="[^"]*{re.escape(cls)}[^"]*"[^>]*>(.*?)</td>',
                row,
                flags=re.I | re.S,
            )
            return _ff_clean_html_text(mm.group(1)) if mm else ""

        out[title.lower()] = {
            "actual": cell("calendar__actual"),
            "forecast": cell("calendar__forecast"),
            "previous": cell("calendar__previous"),
        }

    return out


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
            continue

        before = (
            event.get("actual"),
            event.get("forecast"),
            event.get("previous"),
        )

        if matched.get("actual"):
            event["actual"] = matched["actual"]

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
            print(
                "macro backfilled:",
                title,
                "| actual:",
                event.get("actual"),
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
    # GitHub Actions 可能延遲幾分鐘，所以 08:30～08:59 都可觸發
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

        # 美東 16:00 後 = 盤後
        if dt_et.hour >= 16:
            post.append(label)

        # 美東 04:00～09:59 = 盤前
        elif 4 <= dt_et.hour < 10:
            pre.append(label)

        # Yahoo 時間若只有 placeholder，使用台灣時間輔助判斷
        # 台灣凌晨 = 美國前一交易日盤後
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
