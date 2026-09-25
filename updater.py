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

OLD_TIMER='setInterval(()=>load().catch(()=>{}),300000);\nsetInterval(()=>{if(document.getElementById("news")?.classList.contains("on"))renderNews();},60000);'
NEW_TIMER='async function refreshWhenActive(){\n  if(document.visibilityState!=="visible")return;\n  try{await load();}catch(e){console.error("foreground refresh failed",e);}\n}\n\nwindow.addEventListener("pageshow",()=>refreshWhenActive());\nwindow.addEventListener("focus",()=>refreshWhenActive());\ndocument.addEventListener("visibilitychange",()=>{\n  if(document.visibilityState==="visible")refreshWhenActive();\n});\n\nsetInterval(()=>refreshWhenActive(),300000);\nsetInterval(()=>{if(document.getElementById("news")?.classList.contains("on"))renderNews();},60000);'

def patch_index(src):
    if 'window.addEventListener("pageshow",()=>refreshWhenActive())' in src:
        return src
    if OLD_TIMER not in src: raise RuntimeError("current index timer marker missing")
    return src.replace(OLD_TIMER,NEW_TIMER,1)

def main():
    index_src=INDEX.read_text(encoding="utf-8")
    index=patch_index(index_src)
    updater=fetch_full_updater()
    compile(updater,"updater.py","exec")
    for token in [
        'window.addEventListener("pageshow",()=>refreshWhenActive())',
        'window.addEventListener("focus",()=>refreshWhenActive())',
        'document.addEventListener("visibilitychange"',
    ]:
        if token not in index: raise RuntimeError(f"refresh patch self-test missing: {token}")
    INDEX.write_text(index,encoding="utf-8")
    UPDATER.write_text(updater,encoding="utf-8")
    print(f"Refresh patch installed; full updater restored: {len(updater)} bytes")
    print("Foreground resume refresh enabled")
    code=compile(updater,str(UPDATER),"exec")
    g={"__name__":"__main__","__file__":str(UPDATER),"__package__":None}
    exec(code,g,g)

if __name__=="__main__":
    main()
