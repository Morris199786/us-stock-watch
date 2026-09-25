from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
UPDATER=ROOT/"updater.py"
BASE_COMMIT="bf7459a658614433b630eaabdbad6b383eb78459"

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
    if len(src)<200000:
        raise RuntimeError(f"verified v11 updater unexpectedly small: {len(src)}")
    return src

def replace_between(src,start_marker,end_marker,replacement,label):
    a=src.find(start_marker)
    if a<0:
        raise RuntimeError(f"{label}: start marker not found")
    b=src.find(end_marker,a)
    if b<0:
        raise RuntimeError(f"{label}: end marker not found")
    return src[:a]+replacement+src[b:]

BING_AND_SAME_EVENT = r"""def _v12_bing_news_items(query, days=7):
    url="https://www.bing.com/news/search?"+urllib.parse.urlencode({
        "q":query,
        "format":"rss",
        "setlang":"en-us"
    })
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req,timeout=10) as r:
            root=ET.fromstring(r.read())
    except Exception:
        return []

    now=datetime.now(timezone.utc)
    out=[]
    for item in root.findall(".//item")[:20]:
        title=(item.findtext("title") or "").strip()
        link=(item.findtext("link") or "").strip()
        pub=parse_rss_date(item.findtext("pubDate"))
        if not title or not link:
            continue
        if pub and now-pub>timedelta(days=days):
            continue
        out.append((title,link,pub))
    return out


def _v11_same_event_context(x):
    title=(x.get("originalTitle") or x.get("title") or "").strip()
    terms=_v11_title_terms(title)
    if not terms:
        return "",""

    low=title.lower()
    queries=[title," ".join(terms[:6])]

    if "force majeure" in low or ("oracle" in low and "data center" in low):
        queries.insert(0,"Oracle force majeure data center New Mexico power delay")
    if "suncatcher" in low or ("google" in low and "tpu" in low):
        queries.insert(0,"Google Project Suncatcher TPU Planet Labs SpaceX")

    wanted=set(z.lower() for z in terms)
    candidates=[]
    seen=set()

    for q in queries:
        for ct,link,pub in _v12_bing_news_items(q,days=7):
            key=(ct+"|"+link).lower()
            if key in seen:
                continue
            seen.add(key)

            cw=set(z.lower() for z in _v11_title_terms(ct))
            overlap=len(wanted & cw)
            blob=(ct+" "+link).lower()

            same_force=("force majeure" in low and "force majeure" in blob)
            same_suncatcher=("suncatcher" in low and "suncatcher" in blob)
            if overlap<2 and not same_force and not same_suncatcher:
                continue

            score=overlap
            if "reuters.com" in blob or "reuters" in blob:
                score+=8
            elif "bloomberg" in blob:
                score+=6
            elif any(k in blob for k in [
                "cnbc","marketwatch","barron","techcrunch",
                "datacenterdynamics","siliconangle","investing.com","finance.yahoo"
            ]):
                score+=4
            candidates.append((score,link,ct))

    candidates.sort(reverse=True)
    for _,link,_ in candidates[:10]:
        try:
            body,resolved=_fetch_article_context(link)
        except Exception:
            body,resolved="",""
        if _v11_article_body_good(body):
            return re.sub(r"\s+"," ",body).strip(),(resolved or link)

    return "",""


"""

CROSS_EVENT = r"""def _v11_cross_company_event_search():
    queries=[
        "Project Suncatcher Google TPU Planet Labs SpaceX",
        "Google TPU satellite Planet Labs SpaceX",
        "Google AI space satellite TPU",
        "Oracle data center force majeure New Mexico",
        "Oracle data center Bloom Energy",
        "Google Microsoft Amazon Meta Oracle AI project partnership data center",
        "Nvidia AI partnership customer supplier data center",
        "CoreWeave Nebius AI partnership infrastructure",
    ]

    out=[]
    seen=set()

    for q in queries:
        for raw,link,pub in _v12_bing_news_items(q,days=7):
            title=re.sub(r"\s+-\s+[^-]{2,80}$","",raw).strip() or raw
            key=re.sub(r"\W+","",title.lower())
            if not key or key in seen:
                continue
            seen.add(key)

            row={
                "ticker":"MARKET",
                "group":"跨公司事件",
                "tag":"題材",
                "title":title,
                "summary":"",
                "originalTitle":title,
                "originalSummary":"",
                "url":link,
                "ts":pub.isoformat() if pub else "",
                "sourceType":"cross_event_search",
                "analystPriority":False,
            }

            try:
                body,resolved=_fetch_article_context(link)
            except Exception:
                body,resolved="",""

            if _v11_article_body_good(body):
                row["originalSummary"]=re.sub(r"\s+"," ",body).strip()[:3200]
                row["hasArticleBody"]=True
                row["detailSource"]="cross_event_article"
                if resolved and _is_valid_article_url(resolved):
                    row["url"]=resolved

            row["relatedTickers"]=_v11_related_tickers(row)
            row["eventType"]=_v11_event_type(row)

            low=(" ".join([
                row.get("originalTitle") or "",
                row.get("originalSummary") or ""
            ])).lower()

            strong_theme=any(k in low for k in [
                "suncatcher","tpu","satellite","space","data center","datacenter",
                "project","partnership","contract","customer","supplier","launch",
                "test","pilot","facility","deployment"
            ])

            if len(row["relatedTickers"])<2:
                if not (row["relatedTickers"] and strong_theme):
                    continue

            if row["relatedTickers"]:
                row["ticker"]=row["relatedTickers"][0]
                row["group"]=_v11_group_for_ticker(row["ticker"])

            if not row["eventType"] and strong_theme:
                row["eventType"]="新題材" if len(row["relatedTickers"])==1 else "跨公司事件"

            out.append(row)
            if len(out)>=36:
                return out

    return out


"""

TARGET_RELEVANCE_V13='def _explicit_ticker_mention(raw, ticker):\n    raw=raw or ""\n    ticker=(ticker or "").strip().upper()\n    if not ticker:\n        return False\n\n    if len(ticker)<=3:\n        pats=[\n            rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])",\n            rf"\\${re.escape(ticker)}\\b",\n            rf"\\({re.escape(ticker)}\\)",\n            rf"\\b(?:NASDAQ|NYSE|AMEX)\\s*:\\s*{re.escape(ticker)}\\b",\n        ]\n        return any(re.search(p,raw) for p in pats)\n\n    return bool(re.search(\n        rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])",\n        raw,re.I\n    ))\n\n\ndef target_relevance(text, display_ticker, company_name):\n    raw=text or ""\n    low=raw.lower()\n    ticker=(display_ticker or "").strip().upper()\n    company=(company_name or "").strip().lower()\n\n    if _explicit_ticker_mention(raw,ticker):\n        return True\n\n    aliases={\n        "ON":["on semiconductor","onsemi"],\n        "GOOGL":["google","alphabet inc","alphabet class a","alphabet class c"],\n        "META":["meta platforms","facebook"],\n        "AMZN":["amazon.com","amazon web services","aws"],\n        "MSFT":["microsoft"],\n        "PL":["planet labs"],\n        "BE":["bloom energy"],\n        "ARM":["arm holdings"],\n        "MU":["micron","micron technology"],\n    }\n    for alias in aliases.get(ticker,[]):\n        if re.search(r"(?<![a-z0-9])"+re.escape(alias)+r"(?![a-z0-9])",low):\n            return True\n\n    if company and company!="alphabet" and re.search(\n        rf"(?<![a-z0-9]){re.escape(company)}(?![a-z0-9])",low\n    ):\n        return True\n\n    if len(ticker)<=3:\n        return False\n\n    stop={\n        "holdings","holding","technologies","technology","systems","system",\n        "semiconductor","semiconductors","solutions","solution","devices",\n        "materials","electronics","international","corporation","company",\n        "inc","limited","ltd","group","plc","common","stock",\n        "united","american","global","advanced","general"\n    }\n    roots=[]\n    for token in re.findall(r"[a-z0-9]+",company):\n        if len(token)>=4 and token not in stop:\n            roots.append(token)\n\n    matched=[\n        r for r in roots\n        if re.search(rf"(?<![a-z0-9]){re.escape(r)}(?![a-z0-9])",low)\n    ]\n    if len(matched)>=2:\n        return True\n    if len(matched)==1 and len(matched[0])>=12:\n        return True\n    return False\n\n\ndef _watchlist_company_name(ticker):\n    ticker=(ticker or "").upper()\n    for _,rows in (cfg.get("groups") or {}).items():\n        for row in rows:\n            try:\n                if str(row[0]).upper()==ticker:\n                    return str(row[1])\n            except Exception:\n                pass\n    return ticker\n\n\ndef _analyst_story_matches_assigned_target(x):\n    if not x.get("analystPriority"):\n        return True\n    ticker=(x.get("ticker") or "").upper()\n    company=_watchlist_company_name(ticker)\n    blob=" ".join([\n        x.get("originalTitle") or "",\n        x.get("originalSummary") or ""\n    ])\n    return target_relevance(blob,ticker,company)\n\n\ndef _attach_analyst_target_company(x):\n    if not x.get("analystPriority"):\n        return x\n    ticker=(x.get("ticker") or "").upper()\n    company=_watchlist_company_name(ticker)\n    for d in x.get("analystDetails") or []:\n        d["targetTicker"]=ticker\n        d["targetCompany"]=company\n    return x\n\n'
REASON_HELPER_V13='def _analyst_reason_from_sentence(sent):\n    text=re.sub(r"\\s+"," ",sent or "").strip()\n    if not text:\n        return ""\n\n    for pat in [\n        r"\\bciting\\s+(.+?)(?:[.;]|$)",\n        r"\\bdue to\\s+(.+?)(?:[.;]|$)",\n        r"\\bbecause of\\s+(.+?)(?:[.;]|$)",\n        r"\\bon the back of\\s+(.+?)(?:[.;]|$)",\n        r"\\bamid\\s+(.+?)(?:[.;]|$)",\n        r"\\bfollowing\\s+(.+?)(?:[.;]|$)",\n        r"\\bafter\\s+(.+?)(?:[.;]|$)",\n    ]:\n        m=re.search(pat,text,re.I)\n        if m:\n            reason=m.group(1).strip(" ,:-")\n            if 8<=len(reason)<=360:\n                return reason\n\n    m=re.search(\n        r"\\b(?:target|price target)\\b.{0,60}?\\bon\\s+"\n        r"(.+?(?:momentum|demand|growth|outlook|pricing|margins?|revenue|orders?|"\n        r"adoption|valuation|visibility|backlog|AI|cloud).*)$",\n        text,re.I\n    )\n    if m:\n        reason=m.group(1).strip(" ,:-")\n        if 8<=len(reason)<=360:\n            return reason\n    return ""\n\n\n'
SUMMARY_FUNC_V13='def _analyst_details_summary(details):\n    if not details:\n        return ""\n    lines=[]\n    for d in details[:5]:\n        company=d.get("targetCompany") or ""\n        ticker=d.get("targetTicker") or ""\n        target=company\n        if ticker:\n            target=(company+"（"+ticker+"）") if company and company.upper()!=ticker else ticker\n\n        parts=[]\n        if target:\n            parts.append("被評級公司："+target)\n        if d.get("broker"):\n            parts.append("券商："+d["broker"])\n        if d.get("action"):\n            parts.append("動作："+d["action"])\n        if d.get("rating"):\n            parts.append("評等："+d["rating"])\n        if d.get("oldTarget") and d.get("newTarget"):\n            parts.append(f\'目標價：{d["newTarget"]}（原 {d["oldTarget"]}）\')\n        elif d.get("newTarget"):\n            parts.append("目標價："+d["newTarget"])\n\n        reason=(d.get("reason") or "").strip()\n        parts.append("調整原因："+(reason if reason else "來源未提供具體理由"))\n        lines.append("｜".join(parts))\n    return "\\n".join(lines)\n\n\n'
BRIEF_BLOCK_V13='    details = x.get("analystDetails") or []\n    if x.get("analystPriority") and details:\n        _attach_analyst_target_company(x)\n        details=x.get("analystDetails") or []\n        summary=_analyst_details_summary(details)\n\n        reasons=[]\n        for d in details:\n            r=(d.get("reason") or "").strip()\n            if r and r not in reasons:\n                reasons.append(r)\n\n        why="；".join(reasons[:3]) if reasons else "來源未提供具體調整理由"\n        targets=[z for d in details for z in [d.get("oldTarget"),d.get("newTarget")] if z]\n\n        return {\n            "event": summary,\n            "numbers": targets[:8],\n            "why": "調整原因："+why,\n            "stance": "",\n            "watch": [],\n            "takeaway": "調整原因："+why,\n            "sourceQuality": "analyst_structured"\n        }\n\n'
OLD_TARGET_PARSE_V13='        # "$900 price target"\n        if not new_target:\n            m = re.search(r"\\$([\\d,.]+)\\s+(?:price target|target price)", sent, re.I)\n            if m:\n                new_target = m.group(1)\n'
NEW_TARGET_PARSE_V13='        # "$900 price target" / "$97 target"\n        if not new_target:\n            m = re.search(r"\\$([\\d,.]+)\\s+(?:(?:price\\s+)?target|target price)\\b", sent, re.I)\n            if m:\n                new_target = m.group(1)\n'
OLD_REASON_V13='        # Use the source sentence itself as reason, but remove mechanical target/rating clause.\n        reason = sent.strip()\n        reason = re.sub(r"^\\s*[-–—:;,\\s]+", "", reason)\n        if len(reason) > 420:\n            reason = reason[:420].rsplit(" ",1)[0] + "…"\n'
NEW_REASON_V13='        # Keep only an explicitly stated analyst rationale.\n        reason = _analyst_reason_from_sentence(sent)\n'
INDEX_OLD_V13='       <div class="newsbriefText">${analystDetails.map(d=>{\n         const target=d.oldTarget&&d.newTarget?`${escapeHtml(d.oldTarget)} → ${escapeHtml(d.newTarget)}`:(d.newTarget?escapeHtml(d.newTarget):"");\n         const head=[escapeHtml(d.broker||""),escapeHtml(d.rating||""),target].filter(Boolean).join("｜");\n         const who=d.analyst?`<br>分析師：${escapeHtml(d.analyst)}`:"";\n         const why=d.reason?`<br><span style="color:#cbd8e6">${escapeHtml(d.reason)}</span>`:"";\n         return `<div style="padding:9px 0;border-bottom:1px solid rgba(255,255,255,.08)"><b>${head}</b>${who}${why}</div>`;\n       }).join("")}</div>\n'
INDEX_NEW_V13='       <div class="newsbriefText">${analystDetails.map(d=>{\n         const company=(d.targetCompany||"").trim();\n         const ticker=(d.targetTicker||x.ticker||"").trim();\n         const targetName=company?(ticker?`${escapeHtml(company)}（${escapeHtml(ticker)}）`:escapeHtml(company)):escapeHtml(ticker);\n         const target=d.oldTarget&&d.newTarget?`${escapeHtml(d.newTarget)}（原 ${escapeHtml(d.oldTarget)}）`:(d.newTarget?escapeHtml(d.newTarget):"未提供");\n         const broker=escapeHtml(d.broker||"未辨識");\n         const action=escapeHtml(d.action||"未提供");\n         const rating=escapeHtml(d.rating||"未提供");\n         const who=d.analyst?`<br>分析師：${escapeHtml(d.analyst)}`:"";\n         const reason=escapeHtml((d.reason||"").trim()||"來源未提供具體理由");\n         return `<div style="padding:9px 0;border-bottom:1px solid rgba(255,255,255,.08)">\n           <b>被評級公司：${targetName}</b><br>\n           券商：${broker}｜動作：${action}｜評等：${rating}<br>\n           目標價：${target}<br>\n           <span style="color:#cbd8e6">調整原因：${reason}</span>${who}\n         </div>`;\n       }).join("")}</div>\n'

def patch_broker_v13(src):
    src=replace_between(src,'def target_relevance(text, display_ticker, company_name):','\ndef parse_rss_date(text):',TARGET_RELEVANCE_V13,'strict target mapping')
    marker='def _extract_analyst_details(text):'
    if marker not in src: raise RuntimeError('analyst details marker missing')
    src=src.replace(marker,REASON_HELPER_V13+marker,1)
    if OLD_TARGET_PARSE_V13 not in src: raise RuntimeError('target parser block missing')
    src=src.replace(OLD_TARGET_PARSE_V13,NEW_TARGET_PARSE_V13,1)
    if OLD_REASON_V13 not in src: raise RuntimeError('reason block missing')
    src=src.replace(OLD_REASON_V13,NEW_REASON_V13,1)
    src=replace_between(src,'def _analyst_details_summary(details):','\ndef _title_similarity(a, b):',SUMMARY_FUNC_V13,'analyst summary')
    marker='all_news = enrich_analyst_details(all_news)'
    if marker not in src: raise RuntimeError('analyst enrichment marker missing')
    src=src.replace(marker,marker+'\nall_news = [x for x in all_news if _analyst_story_matches_assigned_target(x)]',1)
    src=src.replace('history_candidates.extend([x for x in old_history_items if not _v11_false_alphabet_story(x)])','history_candidates.extend([x for x in old_history_items if not _v11_false_alphabet_story(x) and _analyst_story_matches_assigned_target(x)])')
    src=src.replace('history_candidates.extend([x for x in history_items if not _v11_false_alphabet_story(x)])','history_candidates.extend([x for x in history_items if not _v11_false_alphabet_story(x) and _analyst_story_matches_assigned_target(x)])')
    old='for x in news:\n    x["relatedTickers"]=_v11_related_tickers(x)\n    x["eventType"]=_v11_event_type(x)\n    x["investorBrief"] = build_investor_brief(x)\n'
    new='for x in news:\n    x["relatedTickers"]=_v11_related_tickers(x)\n    x["eventType"]=_v11_event_type(x)\n    _attach_analyst_target_company(x)\n    x["investorBrief"] = build_investor_brief(x)\n'
    if old not in src: raise RuntimeError('news brief loop missing')
    src=src.replace(old,new,1)
    old='for x in history_db:\n    x["relatedTickers"]=_v11_related_tickers(x)\n    x["eventType"]=_v11_event_type(x)\n    x["investorBrief"] = build_investor_brief(x)\n'
    new='for x in history_db:\n    x["relatedTickers"]=_v11_related_tickers(x)\n    x["eventType"]=_v11_event_type(x)\n    _attach_analyst_target_company(x)\n    x["investorBrief"] = build_investor_brief(x)\n'
    if old not in src: raise RuntimeError('history brief loop missing')
    src=src.replace(old,new,1)
    a=src.find('    details = x.get("analystDetails") or []\n    if x.get("analystPriority") and details:\n',src.find('def build_investor_brief'))
    if a<0: raise RuntimeError('analyst brief start missing')
    b=src.find('    zh_summary = (x.get("summary") or "").strip()',a)
    if b<0: raise RuntimeError('analyst brief end missing')
    src=src[:a]+BRIEF_BLOCK_V13+src[b:]
    return src

def patch_index_v13():
    p=ROOT/'index.html'
    html=p.read_text(encoding='utf-8')
    if INDEX_OLD_V13 not in html: raise RuntimeError('analyst detail UI block missing')
    html=html.replace(INDEX_OLD_V13,INDEX_NEW_V13,1)
    old='<div class="newsbriefLabel">為什麼重要</div>'
    new='<div class="newsbriefLabel">${x.analystPriority?"為什麼調整":"為什麼重要"}</div>'
    if old not in html: raise RuntimeError('why label missing')
    html=html.replace(old,new,1)
    p.write_text(html,encoding='utf-8')

def main():
    src=fetch_full()
    src=replace_between(
        src,
        "def _v11_same_event_context(x):",
        "\ndef _v11_needs_context(x):",
        BING_AND_SAME_EVENT,
        "same-event fallback"
    )
    src=replace_between(
        src,
        "def _v11_cross_company_event_search():",
        "\n\n# Enrich broker/analyst stories",
        CROSS_EVENT,
        "cross-company event search"
    )

    src=patch_broker_v13(src)
    patch_index_v13()

    compile(src,"updater.py","exec")

    for token in [
        "def _v12_bing_news_items",
        "Oracle force majeure data center New Mexico power delay",
        "Project Suncatcher Google TPU Planet Labs SpaceX",
        'sourceType":"cross_event_search',
        "def _explicit_ticker_mention",
        "_analyst_story_matches_assigned_target",
        "_analyst_reason_from_sentence",
        "targetCompany",
        "來源未提供具體調整理由",
    ]:
        if token not in src:
            raise RuntimeError(f"v12 self-test missing: {token}")

    if len(src)<200000:
        raise RuntimeError(f"final updater unexpectedly small: {len(src)}")

    UPDATER.write_text(src,encoding="utf-8")
    print(f"Final updater installed: {len(src)} bytes, newsPatch=v13")

    code=compile(src,str(UPDATER),"exec")
    g={"__name__":"__main__","__file__":str(UPDATER),"__package__":None}
    exec(code,g,g)

if __name__=="__main__":
    main()
