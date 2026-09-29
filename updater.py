from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
UPDATER=ROOT/"updater.py"
BASE_COMMIT="7e5db2b35fa2d1971171d53fb1e497cbef958c10"

def fetch_file(path):
    subprocess.run(["git","fetch","origin",BASE_COMMIT,"--depth=1"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    r=subprocess.run(["git","show",f"{BASE_COMMIT}:{path}"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    return r.stdout

MACRO_HELPERS='\ndef _send_telegram(title, message, url=None):\n    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()\n    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()\n    if not token or not chat_id:\n        return False, "missing_telegram_secrets"\n    text = f"{title}\\n{message}".strip()\n    if url:\n        text += f"\\n{url}"\n    try:\n        payload = json.dumps({"chat_id":chat_id,"text":text[:3900],"disable_web_page_preview":True}, ensure_ascii=False).encode("utf-8")\n        req = urllib.request.Request(\n            f"https://api.telegram.org/bot{token}/sendMessage",\n            data=payload,\n            headers={"Content-Type":"application/json","User-Agent":"us-stock-watch/1.0"},\n            method="POST",\n        )\n        with urllib.request.urlopen(req, timeout=12) as r:\n            ok = 200 <= getattr(r, "status", 200) < 300\n        return ok, "ok" if ok else "http_error"\n    except Exception as e:\n        return False, type(e).__name__\n\ndef _ff_clean_html_text(s):\n    s = re.sub(r"<[^>]+>", " ", str(s or ""))\n    s = re.sub(r"\\s+", " ", s)\n    return _html.unescape(s).strip()\n\ndef _ff_web_calendar_today():\n    try:\n        req = urllib.request.Request(\n            "https://www.forexfactory.com/calendar?day=today",\n            headers={\n                "User-Agent":"Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",\n                "Accept":"text/html,application/xhtml+xml",\n                "Accept-Language":"en-US,en;q=0.9",\n                "Referer":"https://www.forexfactory.com/calendar",\n            },\n        )\n        with urllib.request.urlopen(req, timeout=15) as r:\n            html_text = r.read().decode("utf-8","ignore")\n    except Exception:\n        return {}\n    rows = re.findall(r\'(<tr[^>]+class="[^"]*calendar__row[^"]*"[^>]*>.*?</tr>)\', html_text, flags=re.I|re.S)\n    out = {}\n    for row in rows:\n        em = re.search(r\'class="[^"]*calendar__event[^"]*"[^>]*>(.*?)</td>\', row, flags=re.I|re.S)\n        if not em:\n            continue\n        title = _ff_clean_html_text(em.group(1))\n        if not title:\n            continue\n        def cell(cls):\n            m = re.search(rf\'class="[^"]*{re.escape(cls)}[^"]*"[^>]*>(.*?)</td>\', row, flags=re.I|re.S)\n            return _ff_clean_html_text(m.group(1)) if m else ""\n        out[title.lower()] = {\n            "actual": cell("calendar__actual"),\n            "forecast": cell("calendar__forecast"),\n            "previous": cell("calendar__previous"),\n        }\n    return out\n\ndef _ff_apply_web_fallback(raw, now_utc):\n    needs = False\n    for row in raw:\n        if str(row.get("country") or row.get("currency") or "").upper() != "USD":\n            continue\n        impact = str(row.get("impact") or "").strip().title()\n        if impact not in ("High","Medium"):\n            continue\n        dt = _ff_dt(row)\n        if dt and dt <= now_utc + timedelta(minutes=1) and not str(row.get("actual") or "").strip():\n            needs = True\n            break\n    if not needs:\n        return raw\n    web = _ff_web_calendar_today()\n    if not web:\n        return raw\n    for row in raw:\n        title = str(row.get("title") or "").strip()\n        if not title:\n            continue\n        w = web.get(title.lower())\n        if not w:\n            norm = re.sub(r"[^a-z0-9]+"," ",title.lower()).strip()\n            for k,v in web.items():\n                if re.sub(r"[^a-z0-9]+"," ",k).strip() == norm:\n                    w = v\n                    break\n        if not w:\n            continue\n        if w.get("actual"): row["actual"] = w["actual"]\n        if w.get("forecast"): row["forecast"] = w["forecast"]\n        if w.get("previous"): row["previous"] = w["previous"]\n    return raw\n'
OLD_RAW='    raw = _ff_fetch(FF_THIS_WEEK_JSON)\n    raw += _ff_fetch(FF_NEXT_WEEK_JSON)'
NEW_RAW='    raw = _ff_fetch(FF_THIS_WEEK_JSON)\n    raw += _ff_fetch(FF_NEXT_WEEK_JSON)\n    raw = _ff_apply_web_fallback(raw, now_utc)'
OLD_MORNING='        ok, _ = _send_pushover(\n            "今日美國重要數據",\n            "\\n".join(lines[:12]),\n            url="https://morris199786.github.io/us-stock-watch/?page=macro"\n        )'
NEW_MORNING='        ok, _ = _send_telegram(\n            "今日美國重要數據",\n            "\\n".join(lines[:12]),\n            url="https://morris199786.github.io/us-stock-watch/?page=macro"\n        )'
OLD_RESULT='        ok, _ = _send_pushover(\n            "美國數據公布",\n            msg,\n            url="https://morris199786.github.io/us-stock-watch/?page=macro"\n        )'
NEW_RESULT='        ok, _ = _send_telegram(\n            "美國數據公布",\n            msg,\n            url="https://morris199786.github.io/us-stock-watch/?page=macro"\n        )'

def patch_updater(src):
    if len(src)<230000: raise RuntimeError(f"base updater unexpectedly small: {len(src)}")
    if "import html as _html" not in src:
        marker="import urllib.request"
        if marker not in src: raise RuntimeError("urllib import marker missing")
        src=src.replace(marker,marker+"\nimport html as _html",1)
    if "_ff_web_calendar_today" not in src:
        marker="def refresh_macro_data():"
        if marker not in src: raise RuntimeError("refresh_macro_data marker missing")
        src=src.replace(marker,MACRO_HELPERS+"\n"+marker,1)
    if OLD_RAW in src and "_ff_apply_web_fallback(raw, now_utc)" not in src: src=src.replace(OLD_RAW,NEW_RAW,1)
    if OLD_MORNING in src: src=src.replace(OLD_MORNING,NEW_MORNING,1)
    if OLD_RESULT in src: src=src.replace(OLD_RESULT,NEW_RESULT,1)
    return src

def main():
    src=patch_updater(fetch_file("updater.py"))
    compile(src,"updater.py","exec")
    for token in [
        "def _send_telegram(",
        "def _ff_web_calendar_today(",
        "_ff_apply_web_fallback(raw, now_utc)",
        "_send_telegram(\n            \"今日美國重要數據\"",
        "_send_telegram(\n            \"美國數據公布\""
    ]:
        if token not in src: raise RuntimeError(f"macro telegram self-test missing: {token}")
    UPDATER.write_text(src,encoding="utf-8")
    print(f"Macro Telegram + FF web fallback installed: {len(src)} bytes")
    code=compile(src,str(UPDATER),"exec")
    g={"__name__":"__main__","__file__":str(UPDATER),"__package__":None}
    exec(code,g,g)

if __name__=="__main__":
    main()
