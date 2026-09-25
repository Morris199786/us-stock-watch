from pathlib import Path
import subprocess
import re

ROOT = Path(__file__).resolve().parent
UPDATER = ROOT / "updater.py"
INDEX = ROOT / "index.html"
BASE_COMMIT = "960a03b0e6203fee8fcdbf67368e1379bbf8fe52"

def fetch_file(path):
    subprocess.run(["git","fetch","origin",BASE_COMMIT,"--depth=1"], cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    r = subprocess.run(["git","show",f"{BASE_COMMIT}:{path}"], cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return r.stdout

HEADLINE_FALLBACK = '\n_extract_analyst_details_v16_core = _extract_analyst_details\n\ndef _extract_analyst_details(text):\n    out = _extract_analyst_details_v16_core(text)\n    src = re.sub(r"\\s+", " ", text or "").strip()\n    if not src or out:\n        return out\n\n    broker = _broker_from_text(src)\n    if not broker:\n        return []\n\n    low = src.lower()\n    if not re.search(\n        r"\\b(?:price target|target price|upgrade(?:s|d)?|downgrade(?:s|d)?|"\n        r"maintain(?:s|ed)?|reiterate(?:s|d)?|initiat(?:e|es|ed)|"\n        r"raise(?:s|d)?|cut(?:s)?|lower(?:s|ed)?|boost(?:s|ed)?|lift(?:s|ed)?)\\b",\n        low\n    ):\n        return []\n\n    new_target = ""\n    old_target = ""\n\n    for pat in [\n        r"(?:price target|target price|target)\\s+(?:of|to|at)\\s+\\$([\\d,.]+)",\n        r"\\$([\\d,.]+)\\s+(?:(?:price\\s+)?target|target price)\\b",\n    ]:\n        m = re.search(pat, src, re.I)\n        if m:\n            new_target = _fmt_target(m.group(1))\n            break\n\n    m = re.search(r"from\\s+\\$([\\d,.]+)\\s+(?:to|→)\\s+\\$([\\d,.]+)", src, re.I)\n    if m:\n        old_target = _fmt_target(m.group(1))\n        new_target = _fmt_target(m.group(2))\n    else:\n        m = re.search(\n            r"(?:target|price target|target price)[^$]{0,40}to\\s+\\$([\\d,.]+)"\n            r"[^$]{0,35}from\\s+\\$([\\d,.]+)",\n            src, re.I\n        )\n        if m:\n            new_target = _fmt_target(m.group(1))\n            old_target = _fmt_target(m.group(2))\n\n    rating = ""\n    rm = re.search(\n        r"\\b(Strong Buy|Buy|Outperform|Overweight|Neutral|Equal[- ]Weight|Hold|"\n        r"Market Perform|Underperform|Underweight|Sell)\\b",\n        src, re.I\n    )\n    if rm:\n        rating = rm.group(1)\n\n    action = ""\n    if re.search(r"\\b(?:initiat(?:e|es|ed)|starts? coverage|begins? coverage)\\b", low):\n        action = "初評"\n    elif re.search(r"\\bdowngrad(?:e|ed|es|ing)\\b", low):\n        action = "降評"\n    elif re.search(r"\\bupgrad(?:e|ed|es|ing)\\b", low):\n        action = "升評"\n    elif re.search(\n        r"\\b(?:cut|cuts|lowered|reduced)\\b.{0,45}\\b(?:target|price target|target price)\\b|"\n        r"\\b(?:target|price target|target price)\\b.{0,45}\\b(?:cut|cuts|lowered|reduced)\\b",\n        low\n    ):\n        action = "下修"\n    elif re.search(\n        r"\\b(?:raise|raises|raised|boosted|lifted|increased|hiked)\\b.{0,45}"\n        r"\\b(?:target|price target|target price)\\b|"\n        r"\\b(?:target|price target|target price)\\b.{0,45}"\n        r"\\b(?:raise|raises|raised|boosted|lifted|increased|hiked)\\b",\n        low\n    ):\n        action = "上修"\n    elif re.search(r"\\b(?:maintain(?:s|ed)?|reiterate(?:s|d)?)\\b", low):\n        action = "重申"\n\n    reason = ""\n    confidence = "none"\n    m = re.search(\n        r"\\bon\\s+(.+?(?:optimism|growth|demand|pricing|pricing power|margins?|"\n        r"revenue|orders?|backlog|ai|cloud|data center|datacenter|adoption|"\n        r"visibility|valuation|undersupply|supply|capacity|compute).*)$",\n        src, re.I\n    )\n    if m:\n        reason = _clean_reason_text_v16(m.group(1))\n        confidence = "high" if reason else "none"\n\n    if reason:\n        translated = zh(reason)\n        reason = (translated or reason)[:420]\n\n    if not (new_target or rating or action):\n        return []\n\n    return [{\n        "broker": broker,\n        "analyst": "",\n        "rating": rating,\n        "action": action,\n        "newTarget": new_target,\n        "oldTarget": old_target,\n        "reason": reason,\n        "reasonConfidence": confidence,\n    }]\n\n\n'

def patch_updater(src):
    if len(src) < 230000:
        raise RuntimeError(f"v16 base updater unexpectedly small: {len(src)}")

    marker = "def _analyst_details_summary(details):"
    if marker not in src:
        raise RuntimeError("analyst summary marker missing")
    src = src.replace(marker, HEADLINE_FALLBACK + marker, 1)

    old_sort = 'news.sort(key=lambda z: (1 if z.get("analystPriority") else 0, z.get("score", 0), z.get("ts", "")), reverse=True)'
    new_sort = 'news.sort(key=lambda z: z.get("ts", ""), reverse=True)'
    if old_sort in src:
        src = src.replace(old_sort, new_sort, 1)
    elif new_sort not in src:
        raise RuntimeError("backend news sort marker missing")
    return src

def patch_index(src):
    old_featured = 'items=all.filter(x=>x.featured!==false).slice(0,18);'
    new_featured = 'items=all.filter(x=>x.featured!==false).sort((a,b)=>String(b.ts||"").localeCompare(String(a.ts||""))).slice(0,18);'
    if old_featured in src:
        src = src.replace(old_featured, new_featured, 1)
    elif new_featured not in src:
        raise RuntimeError("frontend featured-news marker missing")

    # Replace the analyst-first search sort with newest-first using a tolerant regex.
    pattern = re.compile(
        r'\}\)\.sort\(\(a,b\)=>\{\s*'
        r'const\s+ap=\(a\.analystPriority\?1:0\),\s*bp=\(b\.analystPriority\?1:0\);\s*'
        r'if\(bp!==ap\)\s*return\s+bp-ap;\s*'
        r'return\s+String\(b\.ts\|\|""\)\.localeCompare\(String\(a\.ts\|\|""\)\);\s*'
        r'\}\);',
        re.S
    )
    replacement = '}).sort((a,b)=>String(b.ts||"").localeCompare(String(a.ts||"")));'
    src2, count = pattern.subn(replacement, src, count=1)
    if count == 0:
        if replacement not in src:
            raise RuntimeError("frontend ticker-search newest-first patch not found")
        src2 = src
    return src2

def main():
    updater = patch_updater(fetch_file("updater.py"))
    index = patch_index(fetch_file("index.html"))
    compile(updater, "updater.py", "exec")

    if "_extract_analyst_details_v16_core" not in updater:
        raise RuntimeError("headline analyst fallback missing")
    if 'news.sort(key=lambda z: z.get("ts", ""), reverse=True)' not in updater:
        raise RuntimeError("backend newest-first sort missing")
    if 'const ap=(a.analystPriority?1:0)' in index:
        raise RuntimeError("old analyst-first frontend sort still present")

    UPDATER.write_text(updater, encoding="utf-8")
    INDEX.write_text(index, encoding="utf-8")
    print(f"Final updater installed: {len(updater)} bytes, newsPatch=v17-fixed2")
    print("Frontend installed: newest-first feed + ticker search")

    code = compile(updater, str(UPDATER), "exec")
    g = {"__name__":"__main__","__file__":str(UPDATER),"__package__":None}
    exec(code, g, g)

if __name__ == "__main__":
    main()
