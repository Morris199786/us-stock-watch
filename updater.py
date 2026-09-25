from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
UPDATER=ROOT/"updater.py"
BASE_COMMIT="bd8630d19967b42feae4d4d0b39aacf6eff00aed"

def fetch_current():
    subprocess.run(["git","fetch","origin",BASE_COMMIT,"--depth=1"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    r=subprocess.run(["git","show",f"{BASE_COMMIT}:updater.py"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    return r.stdout

OLD='    for obj in raw[:12]:\n        c = obj.get("content", obj) if isinstance(obj, dict) else {}\n        title = (c.get("title") or obj.get("title") or "").strip()\n        summary = (c.get("summary") or c.get("description") or "").strip()\n        url = ""\n\n        ctu = c.get("clickThroughUrl") or c.get("canonicalUrl")\n        if isinstance(ctu, dict):\n            url = ctu.get("url", "")\n        elif isinstance(ctu, str):\n            url = ctu\n        url = url or obj.get("link", "")\n\n        pd = c.get("pubDate") or obj.get("providerPublishTime")'
NEW='    for obj in raw[:12]:\n        # yfinance can occasionally return {"content": None} or even non-dict rows.\n        # Normalize both obj and content before any .get() access.\n        if not isinstance(obj, dict):\n            continue\n        c = obj.get("content") or obj\n        if not isinstance(c, dict):\n            c = {}\n\n        title = (c.get("title") or obj.get("title") or "").strip()\n        summary = (c.get("summary") or c.get("description") or "").strip()\n        url = ""\n\n        ctu = c.get("clickThroughUrl") or c.get("canonicalUrl")\n        if isinstance(ctu, dict):\n            url = ctu.get("url", "")\n        elif isinstance(ctu, str):\n            url = ctu\n        url = url or obj.get("link", "")\n\n        pd = c.get("pubDate") or obj.get("providerPublishTime")'

def patch(src):
    if OLD not in src: raise RuntimeError("item_news None-content marker missing")
    return src.replace(OLD,NEW,1)

def main():
    src=patch(fetch_current())
    compile(src,"updater.py","exec")
    for token in [
        'if not isinstance(obj, dict):',
        'c = obj.get("content") or obj',
        'if not isinstance(c, dict):'
    ]:
        if token not in src: raise RuntimeError(f"hotfix self-test missing: {token}")
    UPDATER.write_text(src,encoding="utf-8")
    print(f"Hotfix installed: {len(src)} bytes")
    code=compile(src,str(UPDATER),"exec")
    g={"__name__":"__main__","__file__":str(UPDATER),"__package__":None}
    exec(code,g,g)

if __name__=="__main__":
    main()
