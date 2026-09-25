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

BING_MULTI='def _v12_bing_news_items(query, days=14):\n    now=datetime.now(timezone.utc)\n    out=[]\n    seen=set()\n    feeds=[\n        "https://www.bing.com/news/search?"+urllib.parse.urlencode({\n            "q":query,"format":"rss","setlang":"en-us"\n        }),\n        "https://news.google.com/rss/search?"+urllib.parse.urlencode({\n            "q":query,"hl":"en-US","gl":"US","ceid":"US:en"\n        }),\n    ]\n    for url in feeds:\n        try:\n            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})\n            with urllib.request.urlopen(req,timeout=10) as r:\n                root=ET.fromstring(r.read())\n        except Exception:\n            continue\n        for item in root.findall(".//item")[:30]:\n            title=(item.findtext("title") or "").strip()\n            link=(item.findtext("link") or "").strip()\n            pub=parse_rss_date(item.findtext("pubDate"))\n            if not title or not link:\n                continue\n            if pub and now-pub>timedelta(days=days):\n                continue\n            key=re.sub(r"\\W+","",title.lower())\n            if not key or key in seen:\n                continue\n            seen.add(key)\n            out.append((title,link,pub))\n    return out\n\n\n'
CROSS_EVENT='def _v11_cross_company_event_search():\n    queries=[\n        "Project Suncatcher",\n        "Google Project Suncatcher",\n        "Google TPU space",\n        "Google TPU satellite",\n        "Google TPU Planet Labs",\n        "Google TPU SpaceX",\n        "Planet Labs Google TPU",\n        "SpaceX Google TPU",\n        "Oracle data center force majeure New Mexico",\n        "Oracle data center Bloom Energy",\n        "Google Microsoft Amazon Meta Oracle AI project partnership data center",\n        "Nvidia AI partnership customer supplier data center",\n        "CoreWeave Nebius AI partnership infrastructure",\n    ]\n\n    out=[]\n    seen=set()\n    for q in queries:\n        for raw,link,pub in _v12_bing_news_items(q,days=14):\n            title=re.sub(r"\\s+-\\s+[^-]{2,80}$","",raw).strip() or raw\n            key=re.sub(r"\\W+","",title.lower())\n            if not key or key in seen:\n                continue\n            seen.add(key)\n\n            row={\n                "ticker":"MARKET","group":"跨公司事件","tag":"題材",\n                "title":title,"summary":"","originalTitle":title,"originalSummary":"",\n                "url":link,"ts":pub.isoformat() if pub else "",\n                "sourceType":"cross_event_search","analystPriority":False,\n            }\n\n            try:\n                body,resolved=_fetch_article_context(link)\n            except Exception:\n                body,resolved="",""\n\n            if _v11_article_body_good(body):\n                row["originalSummary"]=re.sub(r"\\s+"," ",body).strip()[:3200]\n                row["hasArticleBody"]=True\n                row["detailSource"]="cross_event_article"\n                if resolved and _is_valid_article_url(resolved):\n                    row["url"]=resolved\n\n            row["relatedTickers"]=_v11_related_tickers(row)\n            blob=(" ".join([row.get("originalTitle") or "",row.get("originalSummary") or ""])).lower()\n\n            # Explicitly recover Project Suncatcher participants when the article\n            # body/title states them.\n            if "suncatcher" in blob or ("google" in blob and "tpu" in blob and "space" in blob):\n                for t,pat in [\n                    ("GOOGL",r"\\bgoogle\\b|\\balphabet\\b"),\n                    ("PL",r"\\bplanet labs?\\b"),\n                    ("SPCX",r"\\bspacex\\b"),\n                ]:\n                    if re.search(pat,blob,re.I) and t not in row["relatedTickers"]:\n                        row["relatedTickers"].append(t)\n\n            row["eventType"]=_v11_event_type(row)\n            strong=any(k in blob for k in [\n                "suncatcher","tpu","satellite","space","data center","datacenter",\n                "project","partnership","contract","customer","supplier","launch",\n                "test","pilot","facility","deployment"\n            ])\n\n            if len(row["relatedTickers"])<2 and not (row["relatedTickers"] and strong):\n                continue\n\n            if row["relatedTickers"]:\n                row["ticker"]=row["relatedTickers"][0]\n                row["group"]=_v11_group_for_ticker(row["ticker"])\n\n            if not row["eventType"] and strong:\n                row["eventType"]="跨公司事件" if len(row["relatedTickers"])>=2 else "新題材"\n\n            out.append(row)\n            if len(out)>=40:\n                return out\n    return out\n\n\n'
ANALYST_HELPERS='def _clean_analyst_reason_v14(reason):\n    r=re.sub(r"\\s+"," ",reason or "").strip(" ,:-")\n    if not r:\n        return ""\n\n    r=re.sub(\n        r"\\s+(?:By\\s+)?(?:Investing\\.com|Reuters|Bloomberg|MarketWatch|Barron\'?s|CNBC|Yahoo Finance)\\s*$",\n        "",\n        r,\n        flags=re.I\n    ).strip(" ,:-")\n\n    low=r.lower()\n    generic={\n        "project update","company update","stock update","quarterly update",\n        "ahead of earnings","ahead of q3 deliveries","ahead of q4 results"\n    }\n    if low in generic:\n        return ""\n\n    thesis_terms=[\n        "growth","demand","pricing","margin","revenue","orders","backlog","capex",\n        "ai","cloud","launch","product","adoption","valuation","visibility",\n        "deliveries","cost","profit","earnings","market share","momentum",\n        "outlook","guidance","capacity","compute","sales"\n    ]\n    if len(r.split())<=3 and not any(k in low for k in thesis_terms):\n        return ""\n    return r[:360]\n\n\n'
ANALYST_WRAPPER='_extract_analyst_details_v13_base = _extract_analyst_details\n\ndef _extract_analyst_details(text):\n    out=_extract_analyst_details_v13_base(text)\n    src=re.sub(r"\\s+"," ",text or "").strip()\n\n    for d in out:\n        if not d.get("action") and re.search(\n            r"\\b(?:initiat(?:e|es|ed|ing)|starts? coverage|begins? coverage)\\b",\n            src,re.I\n        ):\n            d["action"]="初評"\n\n        if not d.get("newTarget"):\n            m=re.search(\n                r"(?:price target|target price|target)\\s+(?:to|at)\\s+\\$([\\d,.]+)",\n                src,re.I\n            )\n            if not m:\n                m=re.search(r"\\$([\\d,.]+)\\s+(?:(?:price\\s+)?target|target price)\\b",src,re.I)\n            if m:\n                d["newTarget"]=_fmt_target(m.group(1))\n\n        reason=_clean_analyst_reason_v14(d.get("reason") or "")\n        if not reason:\n            reason=_clean_analyst_reason_v14(_analyst_reason_from_sentence(src))\n        if reason:\n            translated=zh(reason)\n            d["reason"]=(translated or reason)[:360]\n        else:\n            d["reason"]=""\n\n    return out\n\n\n'
TITLE_WRAPPER='_normalize_analyst_title_v13_base = normalize_analyst_title\n\ndef normalize_analyst_title(x):\n    out=_normalize_analyst_title_v13_base(x)\n    src=" ".join([x.get("originalTitle") or "",x.get("originalSummary") or ""])\n    if x.get("analystPriority") and re.search(\n        r"\\b(?:initiat(?:e|es|ed|ing)|starts? coverage|begins? coverage)\\b",\n        src,re.I\n    ):\n        out=re.sub(r"\\b升評\\b","初評",out,1)\n        if "初評" not in out:\n            broker=""\n            for name in BROKER_NAMES:\n                if re.search(r"(?<![A-Za-z0-9])"+re.escape(name)+r"(?![A-Za-z0-9])",src,re.I):\n                    broker=name\n                    break\n            ticker=(x.get("ticker") or "").strip()\n            details=x.get("analystDetails") or []\n            rating=next((d.get("rating") for d in details if d.get("rating")),"")\n            target=next((d.get("newTarget") for d in details if d.get("newTarget")),"")\n            if broker and ticker:\n                out=f"{broker} 初評 {ticker}"\n                if rating:\n                    out+=f" {rating}"\n                if target:\n                    out+=f"，目標價 {target}"\n    return out\n\n\n'

def patch(src):
    src=replace_between(src,'def _v12_bing_news_items(query, days=7):','\ndef _v11_same_event_context(x):',BING_MULTI,'news engine')
    src=replace_between(src,'def _v11_cross_company_event_search():','\n\n# Enrich broker/analyst stories',CROSS_EVENT,'cross-event search')

    marker='def _analyst_reason_from_sentence(sent):'
    if marker not in src: raise RuntimeError('analyst reason marker missing')
    src=src.replace(marker,ANALYST_HELPERS+marker,1)

    marker='def _analyst_details_summary(details):'
    if marker not in src: raise RuntimeError('analyst summary marker missing')
    src=src.replace(marker,ANALYST_WRAPPER+marker,1)

    marker='def target_relevance(text, display_ticker, company_name):'
    if marker not in src: raise RuntimeError('target relevance marker missing')
    src=src.replace(marker,TITLE_WRAPPER+marker,1)

    marker='all_news = enrich_analyst_details(all_news)'
    if marker not in src: raise RuntimeError('analyst enrichment marker missing')
    src=src.replace(marker,marker+'\nfor _x in all_news:\n    if _x.get("analystPriority"):\n        _x["analystDetails"]=_extract_analyst_details(" ".join([_x.get("originalTitle") or "",_x.get("originalSummary") or ""]))',1)

    marker='for x in news:\n    x["relatedTickers"]=_v11_related_tickers(x)'
    if marker not in src: raise RuntimeError('news loop marker missing')
    src=src.replace(marker,'for x in news:\n    if x.get("analystPriority"):\n        x["analystDetails"]=_extract_analyst_details(" ".join([x.get("originalTitle") or "",x.get("originalSummary") or ""]))\n    x["relatedTickers"]=_v11_related_tickers(x)',1)

    marker='for x in history_db:\n    x["relatedTickers"]=_v11_related_tickers(x)'
    if marker not in src: raise RuntimeError('history loop marker missing')
    src=src.replace(marker,'for x in history_db:\n    if x.get("analystPriority"):\n        x["analystDetails"]=_extract_analyst_details(" ".join([x.get("originalTitle") or "",x.get("originalSummary") or ""]))\n    x["relatedTickers"]=_v11_related_tickers(x)',1)

    return src

def main():
    src=fetch_full()
    src=patch(src)
    compile(src,'updater.py','exec')
    for token in ['Project Suncatcher','Google TPU Planet Labs','_clean_analyst_reason_v14','_extract_analyst_details_v13_base','_normalize_analyst_title_v13_base','初評']:
        if token not in src: raise RuntimeError(f'v14 self-test missing: {token}')
    UPDATER.write_text(src,encoding='utf-8')
    print(f'Final updater installed: {len(src)} bytes, newsPatch=v14-fixed')
    code=compile(src,str(UPDATER),'exec')
    g={'__name__':'__main__','__file__':str(UPDATER),'__package__':None}
    exec(code,g,g)

if __name__=='__main__':
    main()
