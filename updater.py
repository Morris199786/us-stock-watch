from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
UPDATER=ROOT/"updater.py"
BASE_COMMIT="780ac8addf4a00eb4e19e029c5a682c53bc13f38"

def fetch_full():
    subprocess.run(["git","fetch","origin",BASE_COMMIT,"--depth=1"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    r=subprocess.run(["git","show",f"{BASE_COMMIT}:updater.py"],cwd=ROOT,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    src=r.stdout
    if len(src)<220000: raise RuntimeError(f"verified v14 updater unexpectedly small: {len(src)}")
    return src

REASON_ENGINE='def _analyst_reason_score_v15(sentence, broker="", idx=0, broker_idx=None):\n    s=re.sub(r"\\s+"," ",sentence or "").strip()\n    if not s:\n        return -999\n    low=s.lower()\n    score=0\n\n    strong=[\n        "because","citing","due to","driven by","on the back of","supported by",\n        "expects","expecting","believes","sees","forecast","forecasting",\n        "demand","pricing","pricing power","margin","margins","revenue","sales",\n        "orders","backlog","growth","adoption","customer","customers","market share",\n        "capex","capacity","visibility","valuation","earnings","eps","cash flow",\n        "ai","artificial intelligence","cloud","data center","datacenter",\n        "product mix","launch","pipeline","deliveries","cost savings","execution"\n    ]\n    score += sum(2 for k in strong if k in low)\n\n    # Explicit causal connectors are especially valuable.\n    if re.search(r"\\b(?:because|citing|due to|driven by|supported by|on the back of|as a result of)\\b",low):\n        score += 7\n\n    # Broker sentence / nearby context.\n    if broker and broker.lower() in low:\n        score += 5\n    if broker_idx is not None:\n        dist=abs(idx-broker_idx)\n        if dist==0:\n            score += 5\n        elif dist==1:\n            score += 3\n        elif dist==2:\n            score += 1\n\n    # Quantified operating facts usually make a rationale stronger.\n    if re.search(r"\\b\\d+(?:\\.\\d+)?%|\\$[\\d,.]+(?:\\s*(?:billion|million|bn|mn))?\\b",s,re.I):\n        score += 1\n\n    # Mechanical rating/PT language alone is not a reason.\n    mechanical=[\n        "price target","target price","maintains","maintained","reiterates","reiterated",\n        "upgrades","upgraded","downgrades","downgraded","initiates","initiated",\n        "outperform","overweight","underperform","underweight","neutral","buy","hold","sell"\n    ]\n    mech_hits=sum(1 for k in mechanical if k in low)\n    score -= min(5,mech_hits)\n\n    # Background / market-performance sentences are not analyst thesis.\n    background=[\n        "shares rose","shares fell","stock rose","stock fell","stock gained","stock dropped",\n        "year to date","year-to-date","past 12 months","last 12 months","over the past year",\n        "market capitalization","market cap","trading at","closed at","was up","was down",\n        "has risen","has fallen","has gained","has dropped","52-week high","52-week low"\n    ]\n    if any(k in low for k in background):\n        score -= 8\n\n    # Publisher/footer/navigation contamination.\n    if any(k in low for k in [\n        "investing.com","subscribe","newsletter","cookie","privacy policy",\n        "read more","advertisement","premium","copyright"\n    ]):\n        score -= 5\n\n    # Very short fragments are weak unless they carry a clear thesis keyword.\n    if len(s.split())<5:\n        score -= 3\n\n    return score\n\n\ndef _clean_reason_text_v15(text):\n    s=re.sub(r"\\s+"," ",text or "").strip(" ,:;–—-")\n    if not s:\n        return ""\n\n    # Remove publisher suffixes.\n    s=re.sub(\n        r"\\s+(?:By\\s+)?(?:Investing\\.com|Reuters|Bloomberg|MarketWatch|Barron\'?s|CNBC|Yahoo Finance)\\s*$",\n        "",\n        s,\n        flags=re.I\n    ).strip(" ,:;–—-")\n\n    # If sentence has a direct causal connector, keep only the actual rationale.\n    for pat in [\n        r"\\bciting\\s+(.+)$",\n        r"\\bdue to\\s+(.+)$",\n        r"\\bbecause of\\s+(.+)$",\n        r"\\bdriven by\\s+(.+)$",\n        r"\\bsupported by\\s+(.+)$",\n        r"\\bon the back of\\s+(.+)$",\n    ]:\n        m=re.search(pat,s,re.I)\n        if m:\n            candidate=m.group(1).strip(" ,:;–—-")\n            if len(candidate)>=8:\n                s=candidate\n                break\n\n    # Headline forms: "... on data center growth", "... after Muse AI launch".\n    if len(s)>120:\n        for pat in [\n            r"\\bon\\s+(.+?(?:growth|demand|pricing|margins?|revenue|orders?|backlog|AI|cloud|adoption|visibility|valuation).*)$",\n            r"\\bafter\\s+(.+?(?:launch|results?|earnings|guidance|outlook|update).*)$",\n        ]:\n            m=re.search(pat,s,re.I)\n            if m:\n                candidate=m.group(1).strip(" ,:;–—-")\n                if len(candidate)>=8:\n                    s=candidate\n                    break\n\n    # Do not accept pure stock-performance background.\n    low=s.lower()\n    if any(k in low for k in [\n        "past 12 months","last 12 months","year to date","year-to-date",\n        "shares rose","shares fell","stock rose","stock fell","market cap",\n        "52-week high","52-week low"\n    ]):\n        return ""\n\n    generic={\n        "project update","company update","stock update","quarterly update",\n        "ahead of earnings","ahead of q3 deliveries","ahead of q4 results"\n    }\n    if low in generic:\n        return ""\n\n    return s[:420]\n\n\ndef _best_analyst_reason_v15(text, broker=""):\n    src=re.sub(r"\\s+"," ",text or "").strip()\n    if not src:\n        return "",""\n\n    sents=[s.strip() for s in re.split(r"(?<=[.!?])\\s+|[\\u2022•]\\s*",src) if s.strip()]\n    if not sents:\n        return "",""\n\n    broker_idx=None\n    if broker:\n        for i,s in enumerate(sents):\n            if broker.lower() in s.lower():\n                broker_idx=i\n                break\n\n    scored=[]\n    for i,s in enumerate(sents):\n        score=_analyst_reason_score_v15(s,broker,i,broker_idx)\n        scored.append((score,i,s))\n\n    scored.sort(reverse=True,key=lambda x:x[0])\n    if not scored or scored[0][0] < 6:\n        return "",""\n\n    best_score,best_i,best=scored[0]\n    cleaned=_clean_reason_text_v15(best)\n    if not cleaned:\n        return "",""\n\n    confidence="high" if best_score>=11 else "medium"\n    return cleaned,confidence\n\n\n_extract_analyst_details_v14_base = _extract_analyst_details\n\ndef _extract_analyst_details(text):\n    out=_extract_analyst_details_v14_base(text)\n    src=re.sub(r"\\s+"," ",text or "").strip()\n\n    for d in out:\n        broker=d.get("broker") or ""\n        reason,confidence=_best_analyst_reason_v15(src,broker)\n\n        # Use only a semantically relevant sentence. If none clears threshold,\n        # leave reason blank instead of showing article background.\n        if reason:\n            translated=zh(reason)\n            d["reason"]=(translated or reason)[:420]\n            d["reasonConfidence"]=confidence\n        else:\n            d["reason"]=""\n            d["reasonConfidence"]="none"\n\n    return out\n\n\n'

def patch(src):
    marker='def _analyst_details_summary(details):'
    if marker not in src: raise RuntimeError('analyst summary marker missing')
    src=src.replace(marker,REASON_ENGINE+marker,1)

    # Re-parse analyst details after enrichment so full article context is available.
    marker='all_news = enrich_analyst_details(all_news)'
    if marker not in src: raise RuntimeError('analyst enrichment marker missing')
    src=src.replace(marker,marker+'\nfor _x in all_news:\n    if _x.get("analystPriority"):\n        _blob=" ".join([_x.get("originalTitle") or "",_x.get("originalSummary") or ""])\n        _x["analystDetails"]=_extract_analyst_details(_blob)',1)

    marker='for x in news:\n    if x.get("analystPriority"):'
    if marker not in src: raise RuntimeError('news analyst loop marker missing')
    src=src.replace(marker,'for x in news:\n    if x.get("analystPriority"):\n        _blob=" ".join([x.get("originalTitle") or "",x.get("originalSummary") or ""])\n        x["analystDetails"]=_extract_analyst_details(_blob)\n    if x.get("analystPriority"):',1)

    marker='for x in history_db:\n    if x.get("analystPriority"):'
    if marker not in src: raise RuntimeError('history analyst loop marker missing')
    src=src.replace(marker,'for x in history_db:\n    if x.get("analystPriority"):\n        _blob=" ".join([x.get("originalTitle") or "",x.get("originalSummary") or ""])\n        x["analystDetails"]=_extract_analyst_details(_blob)\n    if x.get("analystPriority"):',1)

    return src

def main():
    src=fetch_full()
    src=patch(src)
    compile(src,'updater.py','exec')
    for token in ['_best_analyst_reason_v15','reasonConfidence','_analyst_reason_score_v15','_clean_reason_text_v15']:
        if token not in src: raise RuntimeError(f'v15 self-test missing: {token}')
    UPDATER.write_text(src,encoding='utf-8')
    print(f'Final updater installed: {len(src)} bytes, newsPatch=v15')
    code=compile(src,str(UPDATER),'exec')
    g={'__name__':'__main__','__file__':str(UPDATER),'__package__':None}
    exec(code,g,g)

if __name__=='__main__':
    main()
