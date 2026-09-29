from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
UPDATER=ROOT/"updater.py"
INDEX=ROOT/"index.html"
BASE_COMMIT="5461f6641ed7cdcfaf88c436ffdde5a717a7ed4a"

MACRO_HELPERS='\ndef _send_telegram(title, message, url=None):\n    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()\n    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()\n    if not token or not chat_id:\n        return False, "missing_telegram_secrets"\n\n    text = f"{title}\\n{message}".strip()\n    if url:\n        text += f"\\n{url}"\n\n    try:\n        payload = json.dumps({\n            "chat_id": chat_id,\n            "text": text[:3900],\n            "disable_web_page_preview": True,\n        }, ensure_ascii=False).encode("utf-8")\n        req = urllib.request.Request(\n            f"https://api.telegram.org/bot{token}/sendMessage",\n            data=payload,\n            headers={\n                "Content-Type": "application/json",\n                "User-Agent": "us-stock-watch/1.0",\n            },\n            method="POST",\n        )\n        with urllib.request.urlopen(req, timeout=12) as r:\n            ok = 200 <= getattr(r, "status", 200) < 300\n        return ok, "ok" if ok else "http_error"\n    except Exception as e:\n        return False, type(e).__name__\n\n\ndef _ff_clean_html_text(value):\n    text = re.sub(r"<[^>]+>", " ", str(value or ""))\n    text = re.sub(r"\\s+", " ", text)\n    return _html.unescape(text).strip()\n\n\ndef _ff_web_calendar_today():\n    """\n    Same-source fallback:\n    Forex Factory\'s public JSON feed can lag behind the visible calendar page.\n    Read the visible calendar only after a release is due and JSON Actual is blank.\n    """\n    try:\n        req = urllib.request.Request(\n            "https://www.forexfactory.com/calendar?day=today",\n            headers={\n                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",\n                "Accept": "text/html,application/xhtml+xml",\n                "Accept-Language": "en-US,en;q=0.9",\n                "Referer": "https://www.forexfactory.com/calendar",\n            },\n        )\n        with urllib.request.urlopen(req, timeout=15) as r:\n            html_text = r.read().decode("utf-8", "ignore")\n    except Exception:\n        return {}\n\n    rows = re.findall(\n        r\'(<tr[^>]*class="[^"]*calendar__row[^"]*"[^>]*>.*?</tr>)\',\n        html_text,\n        flags=re.I | re.S,\n    )\n\n    out = {}\n    for row in rows:\n        event_match = re.search(\n            r\'class="[^"]*calendar__event[^"]*"[^>]*>(.*?)</td>\',\n            row,\n            flags=re.I | re.S,\n        )\n        if not event_match:\n            continue\n\n        title = _ff_clean_html_text(event_match.group(1))\n        if not title:\n            continue\n\n        def cell(class_name):\n            m = re.search(\n                rf\'class="[^"]*{re.escape(class_name)}[^"]*"[^>]*>(.*?)</td>\',\n                row,\n                flags=re.I | re.S,\n            )\n            return _ff_clean_html_text(m.group(1)) if m else ""\n\n        out[title.lower()] = {\n            "actual": cell("calendar__actual"),\n            "forecast": cell("calendar__forecast"),\n            "previous": cell("calendar__previous"),\n        }\n\n    return out\n\n\ndef _ff_apply_web_fallback(raw_rows, now_utc):\n    needs_fallback = False\n\n    for row in raw_rows:\n        if str(row.get("country") or row.get("currency") or "").upper() != "USD":\n            continue\n\n        impact = str(row.get("impact") or "").strip().title()\n        if impact not in ("High", "Medium"):\n            continue\n\n        dt = _ff_dt(row)\n        if (\n            dt\n            and dt <= now_utc + timedelta(minutes=1)\n            and not str(row.get("actual") or "").strip()\n        ):\n            needs_fallback = True\n            break\n\n    if not needs_fallback:\n        return raw_rows\n\n    web_rows = _ff_web_calendar_today()\n    if not web_rows:\n        return raw_rows\n\n    for row in raw_rows:\n        title = str(row.get("title") or "").strip()\n        if not title:\n            continue\n\n        web_row = web_rows.get(title.lower())\n\n        if not web_row:\n            wanted = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()\n            for candidate_title, candidate in web_rows.items():\n                normalized = re.sub(r"[^a-z0-9]+", " ", candidate_title).strip()\n                if normalized == wanted:\n                    web_row = candidate\n                    break\n\n        if not web_row:\n            continue\n\n        if web_row.get("actual"):\n            row["actual"] = web_row["actual"]\n        if web_row.get("forecast"):\n            row["forecast"] = web_row["forecast"]\n        if web_row.get("previous"):\n            row["previous"] = web_row["previous"]\n\n    return raw_rows\n'
CONGRESS_HELPERS='\nfunction congressAssetName(x){\n  const ticker=String(x.ticker||"").trim().toUpperCase();\n  if(ticker)return ticker;\n\n  let asset=String(x.asset||"").replace(/\\s+/g," ").trim();\n  if(!asset)return "其他資產";\n\n  const company=asset.match(/Company:\\s*(.+?)(?:\\s*\\([^)]*\\)|\\s*Description:|$)/i);\n  if(company&&company[1])return company[1].trim();\n\n  asset=asset.replace(/\\s*Description:.*$/i,"").trim();\n  return asset||"其他資產";\n}\n\nfunction congressAssetSubLabel(x){\n  const ticker=String(x.ticker||"").trim().toUpperCase();\n  if(ticker)return String(x.asset||"").replace(/\\s+/g," ").trim();\n\n  const asset=String(x.asset||"").replace(/\\s+/g," ").trim();\n  const desc=asset.match(/Description:\\s*(.+)$/i);\n  if(desc&&desc[1])return desc[1].trim();\n\n  return String(x.assetType||"").trim()||"非上市資產";\n}\n\n'
OLD_FILTERS='    <select id="actionFilter"><option value="">全部交易</option><option value="buy">只看買進</option><option value="sell">只看賣出</option></select>\n    <select id="chamberFilter"><option value="">House + Senate</option><option value="house">眾議院</option><option value="senate">參議院</option></select>\n    <button id="clearCongress" class="clearbtn full">清除</button>'
NEW_FILTERS='    <select id="actionFilter"><option value="">全部交易</option><option value="buy">只看買進</option><option value="sell">只看賣出</option></select>\n    <select id="chamberFilter"><option value="">House + Senate</option><option value="house">眾議院</option><option value="senate">參議院</option></select>\n    <select id="assetFilter"><option value="listed" selected>上市股票／ETF</option><option value="other">其他資產</option><option value="">全部資產</option></select>\n    <button id="clearCongress" class="clearbtn full">清除</button>'
UPDATER_OLD_RAW='    raw = _ff_fetch(FF_THIS_WEEK_JSON)\n    raw += _ff_fetch(FF_NEXT_WEEK_JSON)'
UPDATER_NEW_RAW='    raw = _ff_fetch(FF_THIS_WEEK_JSON)\n    raw += _ff_fetch(FF_NEXT_WEEK_JSON)\n    raw = _ff_apply_web_fallback(raw, now_utc)'
MORNING_OLD='        ok, _ = _send_pushover(\n            "今日美國重要數據",\n            "\\n".join(lines[:12]),\n            url="https://morris199786.github.io/us-stock-watch/?page=macro"\n        )'
MORNING_NEW='        ok, _ = _send_telegram(\n            "今日美國重要數據",\n            "\\n".join(lines[:12]),\n            url="https://morris199786.github.io/us-stock-watch/?page=macro"\n        )'
RESULT_OLD='        ok, _ = _send_pushover(\n            "美國數據公布",\n            msg,\n            url="https://morris199786.github.io/us-stock-watch/?page=macro"\n        )'
RESULT_NEW='        ok, _ = _send_telegram(\n            "美國數據公布",\n            msg,\n            url="https://morris199786.github.io/us-stock-watch/?page=macro"\n        )'
INDEX_REPLS=[('  const action=document.getElementById("actionFilter")?.value||"";\n  const chamber=document.getElementById("chamberFilter")?.value||"";', '  const action=document.getElementById("actionFilter")?.value||"";\n  const chamber=document.getElementById("chamberFilter")?.value||"";\n  const assetMode=document.getElementById("assetFilter")?.value??"listed";'), ('    if(action && (x.action||"")!==action)return false;\n    if(chamber && (x.chamber||"")!==chamber)return false;\n    return true;', '    if(action && (x.action||"")!==action)return false;\n    if(chamber && (x.chamber||"")!==chamber)return false;\n    const hasTicker=!!String(x.ticker||"").trim();\n    if(assetMode==="listed" && !hasTicker)return false;\n    if(assetMode==="other" && hasTicker)return false;\n    return true;'), ('    else info.textContent=`找到 ${rows.length.toLocaleString()} 筆｜${member&&filer?"單一議員完整 2024～現在歷史｜":""}以申報日期新 → 舊排序`;', '    else{\n      const assetText=assetMode==="listed"?"上市股票／ETF":assetMode==="other"?"其他資產":"全部資產";\n      info.textContent=`找到 ${rows.length.toLocaleString()} 筆｜${assetText}｜${member&&filer?"單一議員完整 2024～現在歷史｜":""}以申報日期新 → 舊排序`;\n    }'), ('    const actClass=x.action==="buy"?"cbuy":x.action==="sell"?"csell":"";\n    const tickerLabel=x.ticker||"N/A";\n    const ckey=congressDeepKey(x);\n    return `<div class="crow" data-congress-key="${escapeHtml(ckey)}">\n      <div><div class="ticker">${tickerLabel}</div><div class="csmall">${x.asset||""}</div></div>', '    const actClass=x.action==="buy"?"cbuy":x.action==="sell"?"csell":"";\n    const tickerLabel=congressAssetName(x);\n    const assetSub=congressAssetSubLabel(x);\n    const ckey=congressDeepKey(x);\n    return `<div class="crow" data-congress-key="${escapeHtml(ckey)}">\n      <div><div class="ticker">${escapeHtml(tickerLabel)}</div><div class="csmall">${escapeHtml(assetSub)}</div></div>'), ('["actionFilter","chamberFilter"].forEach(id=>document.getElementById(id)?.addEventListener("change",()=>{__congressLimit=50;renderCongress();}));', '["actionFilter","chamberFilter","assetFilter"].forEach(id=>document.getElementById(id)?.addEventListener("change",()=>{__congressLimit=50;renderCongress();}));'), (' const a=document.getElementById("actionFilter"),c=document.getElementById("chamberFilter");if(a)a.value="";if(c)c.value="";\n __congressLimit=50;renderCongress();', ' const a=document.getElementById("actionFilter"),c=document.getElementById("chamberFilter"),af=document.getElementById("assetFilter");\n if(a)a.value="";if(c)c.value="";if(af)af.value="listed";\n __congressLimit=50;renderCongress();')]

def fetch_file(path):
    subprocess.run(
        ["git","fetch","origin",BASE_COMMIT,"--depth=1"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    r=subprocess.run(
        ["git","show",f"{BASE_COMMIT}:{path}"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return r.stdout

def patch_updater(src):
    if len(src) < 240000:
        raise RuntimeError(f"Base updater unexpectedly small: {len(src)}")

    if "import html as _html" not in src:
        marker="import urllib.request"
        if marker not in src:
            raise RuntimeError("urllib import marker missing")
        src=src.replace(marker, marker+"\nimport html as _html", 1)

    if "def _send_telegram(" not in src:
        marker="def refresh_macro_data():"
        if marker not in src:
            raise RuntimeError("macro insertion marker missing")
        src=src.replace(marker, MACRO_HELPERS+"\n"+marker, 1)

    if UPDATER_OLD_RAW in src and "_ff_apply_web_fallback(raw, now_utc)" not in src:
        src=src.replace(UPDATER_OLD_RAW, UPDATER_NEW_RAW, 1)

    if MORNING_OLD in src:
        src=src.replace(MORNING_OLD, MORNING_NEW, 1)

    if RESULT_OLD in src:
        src=src.replace(RESULT_OLD, RESULT_NEW, 1)

    # Macro notifications must no longer call Pushover.
    macro_start=src.find("# ---------- U.S. macro data: Forex Factory ----------")
    macro_end=src.find("CONGRESS_FILE = ROOT", macro_start)
    macro_block=src[macro_start:macro_end]
    if '"今日美國重要數據"' in macro_block and '_send_pushover(' in macro_block:
        # Other Pushover calls elsewhere remain untouched.
        if MORNING_NEW not in macro_block or RESULT_NEW not in macro_block:
            raise RuntimeError("Macro Pushover replacement incomplete")

    return src

def patch_index(src):
    if len(src) < 65000:
        raise RuntimeError(f"Base index unexpectedly small: {len(src)}")

    if 'id="assetFilter"' not in src:
        if OLD_FILTERS not in src:
            raise RuntimeError("Congress filter marker missing")
        src=src.replace(OLD_FILTERS, NEW_FILTERS, 1)

    if "function congressAssetName(x)" not in src:
        marker="function renderCongress(){"
        if marker not in src:
            raise RuntimeError("renderCongress marker missing")
        src=src.replace(marker, CONGRESS_HELPERS+"\n"+marker, 1)

    for old,new in INDEX_REPLS:
        if old in src:
            src=src.replace(old,new,1)

    return src

def main():
    updater=patch_updater(fetch_file("updater.py"))
    index=patch_index(fetch_file("index.html"))

    compile(updater, "updater.py", "exec")

    updater_checks=[
        "def _send_telegram(",
        "def _ff_web_calendar_today(",
        "_ff_apply_web_fallback(raw, now_utc)",
        '_send_telegram(\n            "今日美國重要數據"',
        '_send_telegram(\n            "美國數據公布"',
    ]
    index_checks=[
        'id="assetFilter"',
        'option value="listed" selected',
        "function congressAssetName(x)",
        "function congressAssetSubLabel(x)",
        'assetMode==="listed"',
        '["actionFilter","chamberFilter","assetFilter"]',
    ]

    for token in updater_checks:
        if token not in updater:
            raise RuntimeError(f"Updater self-test missing: {token}")
    for token in index_checks:
        if token not in index:
            raise RuntimeError(f"Index self-test missing: {token}")

    # Guard against regression of existing features.
    for token in ['美股焦點股','document.addEventListener("visibilitychange"']:
        if token not in index:
            raise RuntimeError(f"Existing frontend feature missing: {token}")

    INDEX.write_text(index, encoding="utf-8")
    UPDATER.write_text(updater, encoding="utf-8")

    print("Integrated macro Telegram + FF fallback + Congress asset UI installed")
    print(f"updater bytes={len(updater)} index bytes={len(index)}")

    code=compile(updater, str(UPDATER), "exec")
    g={
        "__name__":"__main__",
        "__file__":str(UPDATER),
        "__package__":None,
    }
    exec(code,g,g)

if __name__=="__main__":
    main()
