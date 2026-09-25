from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
UPDATER=ROOT/"updater.py"
BASE_COMMIT="08fcd6e4068019fc8bea4afd3206acf969e98ba1"

def fetch_full():
    subprocess.run(["git","fetch","origin",BASE_COMMIT,"--depth=1"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    r=subprocess.run(["git","show",f"{BASE_COMMIT}:updater.py"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    src=r.stdout
    if len(src)<210000: raise RuntimeError(f"verified v13 updater unexpectedly small: {len(src)}")
    return src

def replace_between(src,start_marker,end_marker,replacement,label):
    a=src.find(start_marker)
    if a<0: raise RuntimeError(f"{label}: start marker not found")
    b=src.find(end_marker,a)
    if b<0: raise RuntimeError(f"{label}: end marker not found")
    return src[:a]+replacement+src[b:]

NEWS_ENGINE_V14='def _v12_bing_news_items(query, days=14):\n    now=datetime.now(timezone.utc)\n    out=[]\n    seen=set()\n    sources=[\n        ("bing","https://www.bing.com/news/search?"+urllib.parse.urlencode({\n            "q":query,"format":"rss","setlang":"en-us"\n        })),\n        ("google","https://news.google.com/rss/search?"+urllib.parse.urlencode({\n            "q":query,"hl":"en-US","gl":"US","ceid":"US:en"\n        })),\n    ]\n    for engine,url in sources:\n        try:\n            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})\n            with urllib.request.urlopen(req,timeout=10) as r:\n                root=ET.fromstring(r.read())\n        except Exception:\n            continue\n        for item in root.findall(".//item")[:30]:\n            title=(item.findtext("title") or "").strip()\n            link=(item.findtext("link") or "").strip()\n            pub=parse_rss_date(item.findtext("pubDate"))\n            if not title or not link:\n                continue\n            if pub and now-pub>timedelta(days=days):\n                continue\n            key=re.sub(r"\\W+","",title.lower())\n            if not key or key in seen:\n                continue\n            seen.add(key)\n            out.append((title,link,pub))\n    return out\n\n\n'
ANALYST_HELPERS_V14='def _clean_analyst_reason_v14(reason):\n    r=re.sub(r"\\s+"," ",reason or "").strip(" ,:-")\n    if not r:\n        return ""\n\n    r=re.sub(\n        r"\\s+(?:By\\s+)?(?:Investing\\.com|Reuters|Bloomberg|MarketWatch|Barron\'?s|CNBC|Yahoo Finance)\\s*$",\n        "",\n        r,\n        flags=re.I\n    ).strip(" ,:-")\n\n    low=r.lower()\n    generic={\n        "project update","company update","stock update","quarterly update",\n        "ahead of earnings","ahead of q3 deliveries","ahead of q4 results"\n    }\n    if low in generic:\n        return ""\n\n    thesis_terms=[\n        "growth","demand","pricing","margin","revenue","orders","backlog","capex",\n        "ai","cloud","launch","product","adoption","valuation","visibility",\n        "deliveries","cost","profit","earnings","market share","momentum",\n        "outlook","guidance","capacity","compute","sales"\n    ]\n    if len(r.split())<=3 and not any(k in low for k in thesis_terms):\n        return ""\n\n    return r[:360]\n\n\ndef _refresh_analyst_details_v14(x):\n    if not x.get("analystPriority"):\n        return x\n\n    source=" ".join([\n        x.get("originalTitle") or "",\n        x.get("originalSummary") or ""\n    ]).strip()\n\n    parsed=_extract_analyst_details(source)\n    if parsed:\n        x["analystDetails"]=parsed\n\n    details=x.get("analystDetails") or []\n    title=(x.get("originalTitle") or "").strip()\n\n    for d in details:\n        if not d.get("action") and re.search(\n            r"\\b(?:initiat(?:e|es|ed|ing)|starts? coverage|begins? coverage)\\b",\n            title,re.I\n        ):\n            d["action"]="初評"\n\n        if not d.get("newTarget"):\n            m=re.search(\n                r"(?:price target|target price|target)\\s+(?:to|at)\\s+\\$([\\d,.]+)",\n                source,re.I\n            )\n            if not m:\n                m=re.search(r"\\$([\\d,.]+)\\s+(?:(?:price\\s+)?target|target price)\\b",source,re.I)\n            if m:\n                d["newTarget"]=_fmt_target(m.group(1))\n\n        reason=_clean_analyst_reason_v14(d.get("reason") or "")\n        if not reason:\n            reason=_clean_analyst_reason_v14(_analyst_reason_from_sentence(title))\n        d["reason"]=reason\n\n    return _attach_analyst_target_company(x)\n\n\ndef _translate_analyst_reasons_v14(x):\n    if not x.get("analystPriority"):\n        return x\n    for d in x.get("analystDetails") or []:\n        r=(d.get("reason") or "").strip()\n        if not r:\n            continue\n        translated=zh(r)\n        if translated:\n            d["reason"]=translated[:360]\n    return x\n\n\n'

def patch_news_engine(src):
    src=replace_between(src,'def _v12_bing_news_items(query, days=7):','\ndef _v11_same_event_context(x):',NEWS_ENGINE_V14,'multi-engine discovery')
    old='''    queries=[\n        "Project Suncatcher Google TPU Planet Labs SpaceX",\n        "Google TPU satellite Planet Labs SpaceX",\n        "Google AI space satellite TPU",\n        "Oracle data center force majeure New Mexico",\n'''
    new='''    queries=[\n        "Project Suncatcher",\n        "Google Project Suncatcher",\n        "Google TPU space",\n        "Google TPU satellite",\n        "Google TPU Planet Labs",\n        "Google TPU SpaceX",\n        "Planet Labs Google TPU",\n        "SpaceX Google TPU",\n        "Google AI space satellite TPU",\n        "Oracle data center force majeure New Mexico",\n'''
    if old not in src: raise RuntimeError('cross-event query list missing')
    src=src.replace(old,new,1)
    src=src.replace('for raw,link,pub in _v12_bing_news_items(q,days=7):','for raw,link,pub in _v12_bing_news_items(q,days=14):',1)
    return src

def patch_analyst(src):
    marker='def _analyst_reason_from_sentence(sent):'
    if marker not in src: raise RuntimeError('analyst reason marker missing')
    src=src.replace(marker,ANALYST_HELPERS_V14+marker,1)

    old='''        if not action:\n            if re.search(r"\\bupgrad(?:e|ed|es|ing)\\b", low):\n                action = "升評"\n            elif re.search(r"\\bdowngrad(?:e|ed|es|ing)\\b", low):\n                action = "降評"\n            elif any(k in low for k in ["maintained","maintains","reiterated","reiterates"]):\n                action = "重申"\n'''
    new='''        if not action:\n            if re.search(r"\\b(?:initiat(?:e|es|ed|ing)|starts? coverage|begins? coverage)\\b", low):\n                action = "初評"\n            elif re.search(r"\\bupgrad(?:e|ed|es|ing)\\b", low):\n                action = "升評"\n            elif re.search(r"\\bdowngrad(?:e|ed|es|ing)\\b", low):\n                action = "降評"\n            elif any(k in low for k in ["maintained","maintains","reiterated","reiterates"]):\n                action = "重申"\n'''
    if old not in src: raise RuntimeError('analyst action block missing')
    src=src.replace(old,new,1)

    old='''    upgrade_words = [\n        "upgraded to", "upgrade to", "raised to", "initiated with", "initiates with",\n        "initiated at", "initiates at"\n    ]\n'''
    new='''    upgrade_words = [\n        "upgraded to", "upgrade to", "raised to"\n    ]\n    init_words = ["initiated with","initiates with","initiated at","initiates at","initiated coverage at","initiates coverage at"]\n'''
    if old not in src: raise RuntimeError('normalize title words missing')
    src=src.replace(old,new,1)

    old='''    for p in upgrade_words:\n        m = re.search(re.escape(p) + r"\\s+([A-Za-z][A-Za-z \\-/]+?)(?:[,.]| from | with | and |$)", text, re.I)\n        if m:\n            rating_action = "升評"\n            rating = m.group(1).strip()\n            break\n'''
    new='''    for p in init_words:\n        m = re.search(re.escape(p) + r"\\s+([A-Za-z][A-Za-z \\-/]+?)(?:[,.]| from | with | and |$)", text, re.I)\n        if m:\n            rating_action = "初評"\n            rating = m.group(1).strip()\n            break\n    if not rating_action:\n        for p in upgrade_words:\n            m = re.search(re.escape(p) + r"\\s+([A-Za-z][A-Za-z \\-/]+?)(?:[,.]| from | with | and |$)", text, re.I)\n            if m:\n                rating_action = "升評"\n                rating = m.group(1).strip()\n                break\n'''
    if old not in src: raise RuntimeError('normalize title action loop missing')
    src=src.replace(old,new,1)

    old='''all_news = enrich_analyst_details(all_news)\nall_news = [x for x in all_news if _analyst_story_matches_assigned_target(x)]'''
    new='''all_news = enrich_analyst_details(all_news)\nfor x in all_news:\n    _refresh_analyst_details_v14(x)\nall_news = [x for x in all_news if _analyst_story_matches_assigned_target(x)]'''
    if old not in src: raise RuntimeError('analyst enrichment block missing')
    src=src.replace(old,new,1)

    old='''for x in news:\n    x["relatedTickers"]=_v11_related_tickers(x)\n    x["eventType"]=_v11_event_type(x)\n    _attach_analyst_target_company(x)\n    x["investorBrief"] = build_investor_brief(x)\n'''
    new='''for x in news:\n    x["relatedTickers"]=_v11_related_tickers(x)\n    x["eventType"]=_v11_event_type(x)\n    _refresh_analyst_details_v14(x)\n    _translate_analyst_reasons_v14(x)\n    x["investorBrief"] = build_investor_brief(x)\n'''
    if old not in src: raise RuntimeError('news brief loop missing')
    src=src.replace(old,new,1)

    old='''for x in history_db:\n    x["relatedTickers"]=_v11_related_tickers(x)\n    x["eventType"]=_v11_event_type(x)\n    _attach_analyst_target_company(x)\n    x["investorBrief"] = build_investor_brief(x)\n'''
    new='''for x in history_db:\n    x["relatedTickers"]=_v11_related_tickers(x)\n    x["eventType"]=_v11_event_type(x)\n    _refresh_analyst_details_v14(x)\n    _translate_analyst_reasons_v14(x)\n    x["investorBrief"] = build_investor_brief(x)\n'''
    if old not in src: raise RuntimeError('history brief loop missing')
    src=src.replace(old,new,1)
    return src

def main():
    src=fetch_full()
    src=patch_news_engine(src)
    src=patch_analyst(src)
    compile(src,'updater.py','exec')
    for token in ['Project Suncatcher','_refresh_analyst_details_v14','_translate_analyst_reasons_v14','action = "初評"','rating_action = "初評"']:
        if token not in src: raise RuntimeError(f'v14 self-test missing: {token}')
    UPDATER.write_text(src,encoding='utf-8')
    print(f'Final updater installed: {len(src)} bytes, newsPatch=v14')
    code=compile(src,str(UPDATER),'exec')
    g={'__name__':'__main__','__file__':str(UPDATER),'__package__':None}
    exec(code,g,g)

if __name__=='__main__':
    main()
