from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent

UPDATER = ROOT / "updater.py"
HELPER = ROOT / "earnings_macro_helper.py"
UPDATE_YML = ROOT / ".github" / "workflows" / "update.yml"
EARNINGS_YML = ROOT / ".github" / "workflows" / "earnings.yml"


NEW_SEND_TELEGRAM = r'''def _send_telegram(title, message, url=None):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()

    chat_ids = []
    for key in ("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID_2"):
        value = (os.environ.get(key) or "").strip()
        if value and value not in chat_ids:
            chat_ids.append(value)

    if not token or not chat_ids:
        return False, "missing_telegram_secrets"

    text = f"{title}\n{message}".strip()
    if url:
        text += f"\n{url}"

    success_count = 0
    errors = []

    for chat_id in chat_ids:
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

            if ok:
                success_count += 1
            else:
                errors.append(f"{chat_id}:http_error")

        except Exception as e:
            errors.append(f"{chat_id}:{type(e).__name__}")

    if success_count == len(chat_ids):
        return True, f"ok:{success_count}"

    if success_count > 0:
        return True, f"partial:{success_count}/{len(chat_ids)};" + ",".join(errors)

    return False, ",".join(errors) or "telegram_error"
'''


def replace_send_telegram(path: Path):
    if not path.exists():
        print("skip missing:", path)
        return

    s = path.read_text(encoding="utf-8")

    pattern = re.compile(
        r'^def _send_telegram\(title, message, url=None\):\n'
        r'.*?'
        r'(?=^def\s+[A-Za-z_][A-Za-z0-9_]*\s*\()',
        flags=re.M | re.S,
    )

    if not pattern.search(s):
        raise RuntimeError(f"_send_telegram not found in {path}")

    s = pattern.sub(NEW_SEND_TELEGRAM + "\n\n", s, count=1)

    compile(s, str(path), "exec")
    path.write_text(s, encoding="utf-8")
    print("patched dual Telegram:", path)


def remove_macro_morning_url():
    if not UPDATER.exists():
        raise RuntimeError("updater.py missing")

    s = UPDATER.read_text(encoding="utf-8")

    old = '''        ok, _ = _send_telegram(
            "今日美國重要數據",
            "\\n".join(lines[:12]),
            url="https://morris199786.github.io/us-stock-watch/?page=macro"
        )'''

    new = '''        ok, _ = _send_telegram(
            "今日美國重要數據",
            "\\n".join(lines[:12])
        )'''

    if old in s:
        s = s.replace(old, new, 1)
        print("removed macro morning URL")
    else:
        start = s.find('"今日美國重要數據"')
        if start < 0:
            raise RuntimeError("macro reminder title not found")

        window = s[start:start + 350]
        window2 = re.sub(
            r',\s*url="https://morris199786\.github\.io/us-stock-watch/\?page=macro"',
            "",
            window,
            count=1,
        )

        if window2 == window:
            print("macro morning URL already removed or pattern changed")
        else:
            s = s[:start] + window2 + s[start + len(window):]
            print("removed macro morning URL (fallback)")

    compile(s, str(UPDATER), "exec")
    UPDATER.write_text(s, encoding="utf-8")


def add_second_chat_secret(path: Path):
    if not path.exists():
        print("skip missing:", path)
        return

    s = path.read_text(encoding="utf-8")

    if "TELEGRAM_CHAT_ID_2:" in s:
        print("second Telegram secret already present:", path)
        return

    needle = "TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}"
    replacement = (
        "TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}\n"
        "          TELEGRAM_CHAT_ID_2: ${{ secrets.TELEGRAM_CHAT_ID_2 }}"
    )

    if needle not in s:
        raise RuntimeError(f"TELEGRAM_CHAT_ID env not found in {path}")

    s = s.replace(needle, replacement)
    path.write_text(s, encoding="utf-8")
    print("added TELEGRAM_CHAT_ID_2:", path)


def main():
    replace_send_telegram(UPDATER)
    replace_send_telegram(HELPER)

    remove_macro_morning_url()

    add_second_chat_secret(UPDATE_YML)
    add_second_chat_secret(EARNINGS_YML)

    us = UPDATER.read_text(encoding="utf-8")
    hs = HELPER.read_text(encoding="utf-8") if HELPER.exists() else ""

    if "TELEGRAM_CHAT_ID_2" not in us:
        raise RuntimeError("updater.py dual-channel check failed")

    if HELPER.exists() and "TELEGRAM_CHAT_ID_2" not in hs:
        raise RuntimeError("helper dual-channel check failed")

    print("")
    print("DONE")
    print("- Macro morning reminder: no URL")
    print("- Telegram channel 1: TELEGRAM_CHAT_ID")
    print("- Telegram channel 2: TELEGRAM_CHAT_ID_2")


if __name__ == "__main__":
    main()
