from pathlib import Path
import json, re, subprocess, textwrap

ROOT = Path(__file__).resolve().parent
UPDATER = ROOT / "updater.py"
CONFIG = ROOT / "config.json"
INDEX = ROOT / "index.html"
BASE_COMMIT = "cf6dc16ca0f105c1a005874ab8198c5e037a2076"
TARGET_VERSION = 11

def fetch_full():
    subprocess.run(
        ["git","fetch","origin",BASE_COMMIT,"--depth=1"],
        cwd=ROOT,check=True,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True
    )
    r=subprocess.run(
        ["git","show",f"{BASE_COMMIT}:updater.py"],
        cwd=ROOT,check=True,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True
    )
    src=r.stdout
    if len(src)<180000:
        raise RuntimeError(f"verified v9 updater unexpectedly small: {len(src)}")
    return src

def rep(src,old,new,label,required=True):
    old=old.replace("\\n","\n")
    new=new.replace("\\n","\n")
    if old not in src:
        if required:
            raise RuntimeError(f"{label}: expected block not found")
        return src
    return src.replace(old,new,1)

NEWS_HELPERS = textwrap.dedent("""
def _v11_false_alphabet_story(x):
    if (x.get("ticker") or "").upper() != "GOOGL":
        return False
    blob=" ".join([
        x.get("originalTitle") or "",
        x.get("title") or "",
        x.get("originalSummary") or ""
    ]).lower()
    if any(k in blob for k in [
        "google","googl","goog","alphabet inc","alphabet class a",
        "alphabet class c","nasdaq: googl","nasdaq: goog",
        "alphabet stock","alphabet shares","alphabet earnings"
    ]):
        return False
    return any(k in blob for k in [
        "genetic alphabet","dna alphabet","rna alphabet",
        "eight-letter dna","eight letter dna","expand genetic alphabet","genetic code"
    ])

def _v11_article_body_good(body):
    body=re.sub(r"\\s+"," ",(body or "")).strip()
    if len(body)<160:
        return False
    low=body.lower()
    if any(k in low[:1200] for k in [
        "error 404","page not found","404: page not found",
        "premium upgrade now","55% off premium",
        "top analyst stocks popular","smart score stocks",
        "class action lawsuits tools plans"
    ]):
        return False
    nav_hits=sum(low.count(k) for k in [
        "stocks","etfs","crypto","screeners","calendars","plans","tools","newsletter"
    ])
    sentence_marks=sum(body.count(k) for k in [".","!","?","。"])
    if nav_hits>=12 and sentence_marks<8:
        return False
    return not _is_polluted_text(body)

def _v11_title_terms(title):
    words=re.findall(r"[A-Za-z0-9][A-Za-z0-9.'-]+",(title or ""))
    stop={
        "the","a","an","and","or","but","to","of","on","in","for","with","as","at",
        "is","are","was","were","be","been","why","how","stock","stocks","shares",
        "company","says","said","seeks","after","before","from","its","it"
    }
    out=[]
    seen=set()
    for w in words:
        low=w.lower().strip(".'-")
        if len(low)<3 or low in stop or low in seen:
            continue
        seen.add(low)
        out.append(w.strip(".'-"))
        if len(out)>=8:
            break
    return out

def _v11_same_event_context(x):
    title=(x.get("originalTitle") or x.get("title") or "").strip()
    terms=_v11_title_terms(title)
    if not terms:
        return "",""
    q=" ".join(terms[:7])+" when:3d"
    url="https://news.google.com/rss/search?"+urllib.parse.urlencode({
        "q":q,"hl":"en-US","gl":"US","ceid":"US:en"
    })
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req,timeout=10) as r:
            root=ET.fromstring(r.read())
    except Exception:
        return "",""
    wanted=set(z.lower() for z in terms)
    cand=[]
    for item in root.findall(".//item")[:12]:
        ct=(item.findtext("title") or "").strip()
        link=(item.findtext("link") or "").strip()
        if not ct or not link:
            continue
        cw=set(z.lower() for z in _v11_title_terms(ct))
        overlap=len(wanted & cw)
        if overlap<max(2,min(4,max(1,len(wanted)//2))):
            continue
        score=overlap
        low=(ct+" "+link).lower()
        if "reuters" in low: score+=6
        elif "bloomberg" in low: score+=5
        elif any(k in low for k in ["cnbc","marketwatch","barron","techcrunch","investing.com","yahoo"]): score+=3
        cand.append((score,link))
    cand.sort(reverse=True)
    for _,link in cand[:6]:
        try:
            body,resolved=_fetch_article_context(link)
        except Exception:
            continue
        if _v11_article_body_good(body):
            return body.strip(),(resolved or link)
    return "",""

def _v11_needs_context(x):
    if _v11_article_body_good(x.get("originalSummary") or ""):
        return False
    title=(x.get("originalTitle") or x.get("title") or "").lower()
    return any(k in title for k in [
        "force majeure","data center","datacenter","contract","order","customer",
        "partnership","partner","launch","unveil","project","test","supply",
        "shortage","capacity","guidance","outlook","acquisition","merger",
        "approval","investment","factory","plant","facility","price target",
        "upgrade","downgrade","tpu","gpu","satellite","space"
    ])

def _v11_enrich_context(rows,direct_limit=28,fallback_limit=12):
    direct_done=0
    fallback_done=0
    for x in rows or []:
        if not _v11_needs_context(x):
            continue
        body=""
        resolved=""
        url=(x.get("url") or "").strip()
        if direct_done<direct_limit and url:
            direct_done+=1
            try:
                body,resolved=_fetch_article_context(url)
            except Exception:
                body,resolved="",""
        if not _v11_article_body_good(body) and fallback_done<fallback_limit:
            fallback_done+=1
            body,resolved=_v11_same_event_context(x)
        if _v11_article_body_good(body):
            x["originalSummary"]=re.sub(r"\\s+"," ",body).strip()[:3200]
            x["hasArticleBody"]=True
            x["detailSource"]="article_or_same_event"
            if resolved and _is_valid_article_url(resolved):
                x["url"]=resolved
        else:
            if not _v11_article_body_good(x.get("originalSummary") or ""):
                x["originalSummary"]=""
                x["hasArticleBody"]=False
    return rows

def _v11_group_for_ticker(ticker):
    ticker=(ticker or "").upper()
    for group,rows in (cfg.get("groups") or {}).items():
        for row in rows:
            try:
                if str(row[0]).upper()==ticker:
                    return group
            except Exception:
                pass
    return "跨公司事件"

def _v11_related_tickers(x):
    text=(x.get("originalTitle") or x.get("title") or "")
    if _v11_article_body_good(x.get("originalSummary") or ""):
        text+=" "+(x.get("originalSummary") or "")
    low=text.lower()
    primary=(x.get("ticker") or "").upper().strip()
    found=[]
    def add(t):
        t=(t or "").upper().strip()
        if t and t!="MARKET" and t not in found:
            found.append(t)
    if primary and not _v11_false_alphabet_story(x):
        add(primary)
    aliases={
        "GOOGL":[r"\\bgoogle\\b",r"\\balphabet inc\\b",r"\\balphabet class [ac]\\b",r"\\bnasdaq:\\s*googl?\\b"],
        "PL":[r"\\bplanet labs?\\b"],
        "SPCX":[r"\\bspacex\\b"],
        "RKLB":[r"\\brocket lab\\b"],
        "ASTS":[r"\\bast spacemobile\\b"],
        "BE":[r"\\bbloom energy\\b"],
        "ORCL":[r"\\boracle\\b"],
        "MSFT":[r"\\bmicrosoft\\b"],
        "AMZN":[r"\\bamazon\\b",r"\\bamazon web services\\b",r"\\baws\\b"],
        "META":[r"\\bmeta platforms\\b",r"\\bfacebook\\b"],
        "NVDA":[r"\\bnvidia\\b"],
        "AVGO":[r"\\bbroadcom\\b"],
        "MU":[r"\\bmicron\\b"],
        "CRWV":[r"\\bcoreweave\\b"],
        "NBIS":[r"\\bnebius\\b"],
        "VRT":[r"\\bvertiv\\b"],
        "AMD":[r"\\badvanced micro devices\\b",r"\\bamd\\b"],
        "INTC":[r"\\bintel\\b"],
        "ARM":[r"\\barm holdings\\b"],
    }
    for ticker,pats in aliases.items():
        if any(re.search(p,low,re.I) for p in pats):
            add(ticker)
    for _,rows in (cfg.get("groups") or {}).items():
        for row in rows:
            try:
                ticker,name,_=row
            except Exception:
                continue
            ticker=str(ticker).upper().strip()
            name=str(name).strip()
            if ticker in found or not name or name.lower()=="alphabet":
                continue
            if len(name)>=5 and re.search(
                r"(?<![a-z0-9])"+re.escape(name.lower())+r"(?![a-z0-9])",low
            ):
                add(ticker)
    return found[:8]

def _v11_event_type(x):
    blob=" ".join([
        x.get("originalTitle") or "",
        x.get("originalSummary") or ""
    ]).lower()
    rel=x.get("relatedTickers") or []
    event_words=[
        "project","partnership","partners with","contract","customer","supplier",
        "supply agreement","launch","test","pilot","facility","data center",
        "datacenter","factory","plant","deployment","order"
    ]
    if len(rel)>=2 and any(k in blob for k in event_words):
        return "跨公司事件"
    if any(k in blob for k in [
        "new project","launches","unveils","first","pilot","test",
        "new customer","new contract","new partnership","new facility"
    ]):
        return "新題材"
    return ""

def _v11_cross_company_event_search():
    queries=[
        '("Google" OR "Microsoft" OR "Amazon" OR "Meta" OR "Oracle" OR "Nvidia") '
        '(project OR partnership OR contract OR customer OR supplier OR launch OR test OR facility OR "data center") when:2d',
        '("Planet Labs" OR "SpaceX" OR "Rocket Lab" OR "Bloom Energy" OR "CoreWeave" OR "Nebius") '
        '(Google OR Microsoft OR Amazon OR Meta OR Oracle OR Nvidia OR AI) when:2d',
        '("Project Suncatcher" OR (Google TPU satellite) OR (Google TPU SpaceX) OR (Google TPU "Planet Labs")) when:7d',
    ]
    out=[]
    seen=set()
    now=datetime.now(timezone.utc)
    for q in queries:
        url="https://news.google.com/rss/search?"+urllib.parse.urlencode({
            "q":q,"hl":"en-US","gl":"US","ceid":"US:en"
        })
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req,timeout=10) as r:
                root=ET.fromstring(r.read())
        except Exception:
            continue
        for item in root.findall(".//item")[:18]:
            raw=(item.findtext("title") or "").strip()
            link=(item.findtext("link") or "").strip()
            pub=parse_rss_date(item.findtext("pubDate"))
            if not raw or not link:
                continue
            if pub and now-pub>timedelta(days=7):
                continue
            title=re.sub(r"\\s+-\\s+[^-]{2,80}$","",raw).strip() or raw
            key=re.sub(r"\\W+","",title.lower())
            if not key or key in seen:
                continue
            seen.add(key)
            row={
                "ticker":"MARKET","group":"跨公司事件","tag":"題材",
                "title":title,"summary":"","originalTitle":title,"originalSummary":"",
                "url":link,"ts":pub.isoformat() if pub else "",
                "sourceType":"cross_event_search","analystPriority":False
            }
            row["relatedTickers"]=_v11_related_tickers(row)
            low=title.lower()
            if len(row["relatedTickers"])<2:
                if not (
                    row["relatedTickers"] and
                    any(k in low for k in [
                        "project","launch","test","pilot","partnership","contract","tpu","satellite"
                    ])
                ):
                    continue
            if row["relatedTickers"]:
                row["ticker"]=row["relatedTickers"][0]
                row["group"]=_v11_group_for_ticker(row["ticker"])
            out.append(row)
    return out[:36]

""")

def patch_parser(src):
    src=rep(
        src,
        "if old.get('parserVersion') == 9 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',0))):",
        "if old.get('parserVersion') == 11 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',0))):",
        "refresh parser version"
    )
    src=rep(src,"same_parser = old.get('parserVersion') == 9","same_parser = old.get('parserVersion') == 11","same parser")
    src=rep(src,"payload={'parserVersion':9,'updatedAtUtc':now.isoformat()","payload={'parserVersion':11,'updatedAtUtc':now.isoformat()","payload parser")
    src=src.replace("earningsCheckedV9","earningsCheckedV11")
    return src

def patch_news(src):
    anchor="# Score + deduplicate news."
    if anchor not in src:
        raise RuntimeError("score anchor missing")
    src=src.replace(anchor,NEWS_HELPERS+"\n"+anchor,1)

    old="# Enrich broker/analyst stories so the detail modal has useful context and\\n# normalize_analyst_title can see exact old/new target values when available.\\nall_news = enrich_analyst_details(all_news)"
    new=old+"\\nall_news = [x for x in all_news if not _v11_false_alphabet_story(x)]\\nall_news = _v11_enrich_context(all_news, direct_limit=26, fallback_limit=10)\\nall_news.extend(_v11_cross_company_event_search())\\nfor x in all_news:\\n    x['relatedTickers']=_v11_related_tickers(x)\\n    x['eventType']=_v11_event_type(x)"
    src=rep(src,old,new,"news enrich and cross-event scan")

    old='    # Broad market-moving events get extra weight for the daily Top 10.\\n    if x.get("marketWide") or x.get("sourceType") == "market_search":\\n        score += 2.5'
    new=old+'\\n    if len(x.get("relatedTickers") or []) >= 2:\\n        score += 6\\n    if x.get("eventType") in ("跨公司事件","新題材"):\\n        score += 5\\n    if x.get("sourceType") == "cross_event_search":\\n        score += 4'
    src=rep(src,old,new,"event score boost")

    old='history_candidates = []\\nhistory_candidates.extend(old_history_items)\\nhistory_candidates.extend(history_items)\\nhistory_candidates.extend([x for x in all_news if x.get("sourceType") == "history_search"])'
    new='history_candidates = []\\nhistory_candidates.extend([x for x in old_history_items if not _v11_false_alphabet_story(x)])\\nhistory_candidates.extend([x for x in history_items if not _v11_false_alphabet_story(x)])\\nhistory_candidates.extend([x for x in all_news if x.get("sourceType") == "history_search" and not _v11_false_alphabet_story(x)])'
    src=rep(src,old,new,"history alphabet cleanup")

    old='# Cap only the database size, not per-ticker history.\\nhistory_db = history_db[:cfg.get("history_news_pool", 800)]\\n\\n# Translate historical titles only when they are not already cached.'
    new='# Cap only the database size, not per-ticker history.\\nhistory_db = history_db[:cfg.get("history_news_pool", 800)]\\nhistory_db = _v11_enrich_context(history_db, direct_limit=28, fallback_limit=12)\\nfor x in history_db:\\n    x["relatedTickers"]=_v11_related_tickers(x)\\n    x["eventType"]=_v11_event_type(x)\\n\\n# Translate historical titles only when they are not already cached.'
    src=rep(src,old,new,"history enrich")

    old='    x["translated"] = has_cjk(x.get("title", ""))\\n    x["summary"] = ""\\n    if x.get("analystPriority"):'
    new='    x["translated"] = has_cjk(x.get("title", ""))\\n    raw_summary=(x.get("originalSummary") or "").strip()\\n    if _v11_article_body_good(raw_summary):\\n        compact=_best_summary_sentences(raw_summary,max_sentences=4,max_chars=760)\\n        x["summary"]=(zh(compact) or compact)[:820] if compact else ""\\n    else:\\n        x["summary"]=""\\n    if x.get("analystPriority"):'
    src=rep(src,old,new,"history summary")

    old='    source_blob = " ".join([original_title, original_summary]).strip()\\n    blob = source_blob.lower()\\n\\n    # Use the translated summary if available; otherwise translate a compact,\\n    # information-dense source excerpt.\\n    event = _best_summary_sentences(zh_summary, max_sentences=4, max_chars=520)'
    new='    source_blob = " ".join([original_title, original_summary]).strip()\\n    blob = source_blob.lower()\\n\\n    substantive=_v11_article_body_good(original_summary)\\n    if not substantive:\\n        return {\\n            "event": "目前只取得新聞標題，尚未取得足夠內文，無法可靠補充事件細節。",\\n            "numbers": [],\\n            "why": "目前來源資訊不足，暫不推論對營收、毛利、訂單或公司展望的實際影響。",\\n            "stance": "",\\n            "watch": [],\\n            "takeaway": "來源不足，等待可驗證內文。",\\n            "sourceQuality": "headline_only"\\n        }\\n\\n    # Use the translated summary if available; otherwise translate a compact,\\n    # information-dense source excerpt.\\n    event = _best_summary_sentences(zh_summary, max_sentences=4, max_chars=720)'
    src=rep(src,old,new,"headline-only guard")

    old='        # Keep old key for backward compatibility with push/UI.\\n        "takeaway": why,\\n    }\\n\\nfor x in news:\\n    x["investorBrief"] = build_investor_brief(x)'
    new='        # Keep old key for backward compatibility with push/UI.\\n        "takeaway": why,\\n        "sourceQuality": "body"\\n    }\\n\\nfor x in news:\\n    x["relatedTickers"]=_v11_related_tickers(x)\\n    x["eventType"]=_v11_event_type(x)\\n    x["investorBrief"] = build_investor_brief(x)\\n\\nfor x in history_db:\\n    x["relatedTickers"]=_v11_related_tickers(x)\\n    x["eventType"]=_v11_event_type(x)\\n    x["investorBrief"] = build_investor_brief(x)'
    src=rep(src,old,new,"brief history")

    marker='    if any(k in blob for k in ["guidance","outlook","forecast","earnings","revenue","eps"]):'
    if marker not in src:
        raise RuntimeError("why marker missing")
    force='    if "force majeure" in blob and any(k in blob for k in ["data center","datacenter"]):\\n        return "這代表資料中心建置可能遇到電力、施工、供應或合約時程風險。投資上應直接追蹤專案是否延後、誰承擔新增成本，以及延誤是否影響雲端容量上線與資本支出效率。"\\n\\n'.replace("\\n","\n")
    src=src.replace(marker,force+marker,1)
    return src

def patch_index():
    html=INDEX.read_text(encoding="utf-8")
    html=rep(html,' const q=raw.toUpperCase();',' const q=(raw.toUpperCase()==="GOOG"?"GOOGL":raw.toUpperCase());',"GOOG alias")
    html=rep(
        html,
        '     .filter(x=>(x.ticker||"").trim().toUpperCase()===q);',
        '     .filter(x=>{\\n       const primary=(x.ticker||"").trim().toUpperCase();\\n       const related=(Array.isArray(x.relatedTickers)?x.relatedTickers:[]).map(t=>String(t).toUpperCase());\\n       return primary===q||related.includes(q);\\n     });',
        "related ticker search"
    )
    old=' const ticker=x.ticker==="MARKET"?"美股市場":(x.ticker||"");\\n document.getElementById("newsDetailTicker").textContent=ticker+(x.group?`｜${x.group}`:"");'
    new=' const ticker=x.ticker==="MARKET"?"美股市場":(x.ticker||"");\\n const related=(Array.isArray(x.relatedTickers)?x.relatedTickers:[]).filter(Boolean);\\n const tickerLabel=related.length>1?related.join(" / "):ticker;\\n document.getElementById("newsDetailTicker").textContent=tickerLabel+(x.group?`｜${x.group}`:"");'
    html=rep(html,old,new,"detail related tickers")
    old=' document.getElementById("newsDetailMeta").textContent=[newsAge(x.ts),newsExactTime(x.ts),x.tag].filter(Boolean).join("｜");'
    new=' document.getElementById("newsDetailMeta").textContent=[newsAge(x.ts),newsExactTime(x.ts),x.eventType,x.tag].filter(Boolean).join("｜");'
    html=rep(html,old,new,"event type meta")
    old=' const brief=x.investorBrief||{\\n   event:(summary||x.title||""),\\n   numbers:[],\\n   why:fallbackInvestorTakeaway(x),\\n   stance:"",\\n   watch:[]\\n };'
    new=' const brief=x.investorBrief||{\\n   event:summary||"目前只取得新聞標題，尚未取得足夠內文，無法可靠補充事件細節。",\\n   numbers:[],\\n   why:summary?fallbackInvestorTakeaway(x):"目前來源資訊不足，暫不推論對營收、毛利、訂單或公司展望的實際影響。",\\n   stance:"",\\n   watch:[],\\n   sourceQuality:summary?"snippet":"headline_only"\\n };'
    html=rep(html,old,new,"safe detail fallback")
    html=rep(
        html,
        ' return "先看這件事是否會影響營收、毛利、訂單能見度或公司展望；若沒有量化財務資訊，先視為題材訊號";',
        ' return "目前來源若沒有提供足夠事件細節或量化資訊，就不先推論對營收、毛利、訂單或公司展望的實際影響";',
        "safe generic fallback"
    )
    INDEX.write_text(html,encoding="utf-8")

def patch_config():
    d=json.loads(CONFIG.read_text(encoding="utf-8"))
    d["earnings_parser_version"]=TARGET_VERSION
    CONFIG.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

def self_test(src):
    if "\\n# Score + deduplicate news." in src:
        raise RuntimeError("literal escaped newline leaked into final updater")
    compile(src,"updater.py","exec")
    for token in [
        "payload={'parserVersion':11",
        "_v11_same_event_context",
        "_v11_cross_company_event_search",
        "_v11_false_alphabet_story",
        "_v11_article_body_good",
        "_v11_related_tickers",
        "sourceQuality",
        "cross_event_search",
        "_earnings_session_reaction",
        "_push_new_earnings_reports",
    ]:
        if token not in src:
            raise RuntimeError(f"self-test missing {token}")
    if len(src)<185000:
        raise RuntimeError(f"patched updater unexpectedly small: {len(src)}")

def main():
    src=fetch_full()
    src=patch_parser(src)
    src=patch_news(src)
    self_test(src)
    UPDATER.write_text(src,encoding="utf-8")
    patch_index()
    patch_config()

    # Guard against malformed config.json before the full updater imports it.
    json.loads(CONFIG.read_text(encoding="utf-8"))

    print(f"Final updater installed: {len(src)} bytes, parserVersion={TARGET_VERSION}")
    code=compile(src,str(UPDATER),"exec")
    g={"__name__":"__main__","__file__":str(UPDATER),"__package__":None}
    exec(code,g,g)

if __name__=="__main__":
    main()
