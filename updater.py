from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
UPDATER=ROOT/"updater.py"
INDEX=ROOT/"index.html"
BASE_COMMIT="ac9200daaea51491e286571cb6b239e9097993e8"

def fetch_full_updater():
    subprocess.run(["git","fetch","origin",BASE_COMMIT,"--depth=1"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    r=subprocess.run(["git","show",f"{BASE_COMMIT}:updater.py"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    src=r.stdout
    if len(src)<230000: raise RuntimeError(f"verified full updater unexpectedly small: {len(src)}")
    return src

OLD_LOAD='async function load(){\n  const [d,n]=await Promise.all([fetch("data.json?"+Date.now()).then(r=>r.json()),fetch("news.json?"+Date.now()).then(r=>r.json())]);'
NEW_LOAD='let __loadInFlight=null;\nlet __lastForegroundRefresh=0;\n\nasync function load(){\n  if(__loadInFlight)return __loadInFlight;\n  __loadInFlight=(async()=>{\n    const nonce=Date.now();\n    const [d,n]=await Promise.all([\n      fetch("data.json?"+nonce,{cache:"no-store"}).then(r=>{if(!r.ok)throw new Error("data.json "+r.status);return r.json()}),\n      fetch("news.json?"+nonce,{cache:"no-store"}).then(r=>{if(!r.ok)throw new Error("news.json "+r.status);return r.json()})\n    ]);'
OLD_END='  }\n}\nwindow.__top10Mode=false;'
NEW_END='  }\n  })();\n  try{\n    await __loadInFlight;\n  }finally{\n    __loadInFlight=null;\n  }\n}\nwindow.__top10Mode=false;'
OLD_TIMER='setInterval(()=>load().catch(()=>{}),300000);\nsetInterval(()=>{if(document.getElementById("news")?.classList.contains("on"))renderNews();},60000);'
NEW_TIMER='async function refreshWhenActive(force=false){\n  if(document.visibilityState!=="visible")return;\n  const now=Date.now();\n  if(!force && now-__lastForegroundRefresh<2000)return;\n  __lastForegroundRefresh=now;\n  try{await load();}catch(e){console.error("foreground refresh failed",e);}\n}\n\nwindow.addEventListener("pageshow",()=>refreshWhenActive(true));\nwindow.addEventListener("focus",()=>refreshWhenActive(false));\ndocument.addEventListener("visibilitychange",()=>{\n  if(document.visibilityState==="visible")refreshWhenActive(true);\n});\n\nsetInterval(()=>refreshWhenActive(false),300000);\nsetInterval(()=>{if(document.getElementById("news")?.classList.contains("on"))renderNews();},60000);'

def patch_index(src):
    if OLD_LOAD not in src: raise RuntimeError("current index load() marker missing")
    src=src.replace(OLD_LOAD,NEW_LOAD,1)
    if OLD_END not in src: raise RuntimeError("current index load() end marker missing")
    src=src.replace(OLD_END,NEW_END,1)
    if OLD_TIMER not in src: raise RuntimeError("current index timer marker missing")
    src=src.replace(OLD_TIMER,NEW_TIMER,1)
    return src

def main():
    index_src=INDEX.read_text(encoding="utf-8")
    index=patch_index(index_src)
    updater=fetch_full_updater()
    compile(updater,"updater.py","exec")
    for token in [
        'window.addEventListener("pageshow"',
        'document.addEventListener("visibilitychange"',
        'window.addEventListener("focus"',
        'cache:"no-store"',
        'let __loadInFlight=null;'
    ]:
        if token not in index: raise RuntimeError(f"refresh patch self-test missing: {token}")
    INDEX.write_text(index,encoding="utf-8")
    UPDATER.write_text(updater,encoding="utf-8")
    print(f"Refresh patch installed; full updater restored: {len(updater)} bytes")
    print("Foreground/pageshow/visibility refresh enabled")
    code=compile(updater,str(UPDATER),"exec")
    g={"__name__":"__main__","__file__":str(UPDATER),"__package__":None}
    exec(code,g,g)

if __name__=="__main__":
    main()
