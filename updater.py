from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
UPDATER=ROOT/"updater.py"
INDEX=ROOT/"index.html"
BASE_COMMIT="2f5cb2c18e8bc84348e72609423ae3a28e3fa398"

def fetch_file(path):
    subprocess.run(["git","fetch","origin",BASE_COMMIT,"--depth=1"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    r=subprocess.run(["git","show",f"{BASE_COMMIT}:{path}"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    return r.stdout

PATCH_CODE='_extract_analyst_details_v16_base = _extract_analyst_details\n\ndef _extract_analyst_details(text):\n    out=_extract_analyst_details_v16_base(text)\n    src=re.sub(r"\\s+"," ",text or "").strip()\n    if not src:\n        return out\n\n    # Headline-only broker stories can still contain enough structured facts:\n    # "Piper Sandler raises Meta stock price target to $875 on AI optimism"\n    if not out:\n        broker=_broker_from_text(src)\n        low=src.lower()\n        has_broker_action=bool(broker and re.search(\n            r"\\b(?:price target|target price|upgrade(?:s|d)?|downgrade(?:s|d)?|"\n            r"maintain(?:s|ed)?|reiterate(?:s|d)?|initiat(?:e|es|ed)|"\n            r"raise(?:s|d)?|cut(?:s)?|lower(?:s|ed)?)\\b",\n            low\n        ))\n        if has_broker_action:\n            new_target=""\n            old_target=""\n\n            m=re.search(\n                r"(?:price target|target price|target)\\s+(?:of|to|at)\\s+\\$([\\d,.]+)",\n                src,re.I\n            )\n            if m:\n                new_target=_fmt_target(m.group(1))\n\n            m2=re.search(\n                r"(?:from\\s+\\$([\\d,.]+)\\s+(?:to|→)\\s+\\$([\\d,.]+)|"\n                r"(?:target|price target|target price)[^$]{0,40}to\\s+\\$([\\d,.]+)[^$]{0,35}from\\s+\\$([\\d,.]+))",\n                src,re.I\n            )\n            if m2:\n                if m2.group(1) and m2.group(2):\n                    old_target=_fmt_target(m2.group(1))\n                    new_target=_fmt_target(m2.group(2))\n                elif m2.group(3):\n                    new_target=_fmt_target(m2.group(3))\n                    old_target=_fmt_target(m2.group(4) or "")\n\n            rating=""\n            rm=re.search(\n                r"\\b(Strong Buy|Buy|Outperform|Overweight|Neutral|Equal[- ]Weight|Hold|"\n                r"Market Perform|Underperform|Underweight|Sell)\\b",\n                src,re.I\n            )\n            if rm:\n                rating=rm.group(1)\n\n            action=""\n            if re.search(r"\\b(?:initiat(?:e|es|ed)|starts? coverage|begins? coverage)\\b",low):\n                action="初評"\n            elif re.search(r"\\bdowngrad(?:e|ed|es|ing)\\b",low):\n                action="降評"\n            elif re.search(r"\\bupgrad(?:e|ed|es|ing)\\b",low):\n                action="升評"\n            elif re.search(r"\\b(?:cut|cuts|lowered|reduced)\\b.{0,45}\\b(?:target|price target|target price)\\b|\\b(?:target|price target|target price)\\b.{0,45}\\b(?:cut|cuts|lowered|reduced)\\b",low):\n                action="下修"\n            elif re.search(r"\\b(?:raise|raises|raised|boosted|lifted|increased|hiked)\\b.{0,45}\\b(?:target|price target|target price)\\b|\\b(?:target|price target|target price)\\b.{0,45}\\b(?:raise|raises|raised|boosted|lifted|increased|hiked)\\b",low):\n                action="上修"\n            elif re.search(r"\\b(?:maintain(?:s|ed)?|reiterate(?:s|d)?)\\b",low):\n                action="重申"\n\n            reason=""\n            # Explicit short headline rationale after "on": AI optimism, data-center growth,\n            # pricing, demand, valuation, etc.\n            m3=re.search(\n                r"\\bon\\s+(.+?(?:optimism|growth|demand|pricing|pricing power|margins?|"\n                r"revenue|orders?|backlog|ai|cloud|data center|datacenter|adoption|"\n                r"visibility|valuation|undersupply|supply).*)$",\n                src,re.I\n            )\n            if m3:\n                reason=_clean_reason_text_v16(m3.group(1))\n            if not reason:\n                reason,confidence=_best_analyst_reason_v16(src,broker)\n            else:\n                confidence="high"\n\n            if reason:\n                translated=zh(reason)\n                reason=(translated or reason)[:420]\n            else:\n                confidence="none"\n\n            if new_target or rating or action:\n                out=[{\n                    "broker":broker,\n                    "analyst":"",\n                    "rating":rating,\n                    "action":action,\n                    "newTarget":new_target,\n                    "oldTarget":old_target,\n                    "reason":reason,\n                    "reasonConfidence":confidence,\n                }]\n\n    return out\n\n\n'

def patch_updater(src):
    if len(src)<230000: raise RuntimeError(f'v16 updater unexpectedly small: {len(src)}')
    marker='def _analyst_details_summary(details):'
    if marker not in src: raise RuntimeError('analyst summary marker missing')
    src=src.replace(marker,PATCH_CODE+marker,1)

    old='news.sort(key=lambda z: (1 if z.get("analystPriority") else 0, z.get("score", 0), z.get("ts", "")), reverse=True)'
    new='news.sort(key=lambda z: z.get("ts", ""), reverse=True)'
    if old not in src: raise RuntimeError('backend news sort marker missing')
    src=src.replace(old,new,1)
    return src

def patch_index(src):
    old='''    }).sort((a,b)=>{\n      const ap=(a.analystPriority?1:0), bp=(b.analystPriority?1:0);\n      if(bp!==ap) return bp-ap;\n      return String(b.ts||"").localeCompare(String(a.ts||""));\n    });'''
    new='''    }).sort((a,b)=>String(b.ts||"").localeCompare(String(a.ts||"")));'''
    if old not in src: raise RuntimeError('frontend search sort marker missing')
    src=src.replace(old,new,1)

    old='items=all.filter(x=>x.featured!==false).slice(0,18);'
    new='items=all.filter(x=>x.featured!==false).sort((a,b)=>String(b.ts||"").localeCompare(String(a.ts||""))).slice(0,18);'
    if old not in src: raise RuntimeError('frontend featured sort marker missing')
    src=src.replace(old,new,1)
    return src

def main():
    updater=patch_updater(fetch_file('updater.py'))
    index=patch_index(fetch_file('index.html'))
    compile(updater,'updater.py','exec')
    for token in ['_extract_analyst_details_v16_base','AI optimism','news.sort(key=lambda z: z.get("ts", ""), reverse=True)']:
        if token not in updater: raise RuntimeError(f'v17 updater self-test missing: {token}')
    if 'analystPriority?1:0' in index: raise RuntimeError('old analyst-first frontend sort still present')
    UPDATER.write_text(updater,encoding='utf-8')
    INDEX.write_text(index,encoding='utf-8')
    print(f'Final updater installed: {len(updater)} bytes, newsPatch=v17')
    print('Frontend news sorting patched: newest-first')
    code=compile(updater,str(UPDATER),'exec')
    g={'__name__':'__main__','__file__':str(UPDATER),'__package__':None}
    exec(code,g,g)

if __name__=='__main__':
    main()
