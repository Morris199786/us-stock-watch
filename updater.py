import json, math, os, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import yfinance as yf

ROOT=Path(__file__).resolve().parent
cfg=json.loads((ROOT/"config.json").read_text(encoding="utf-8"))

POS=["raises guidance","raise guidance","boosts outlook","beats estimates","beats expectations","record revenue","record order","new order","wins contract","contract win","partnership","partners with","launches","unveils","approval","approved","price target raised","upgraded","upgrade","buy rating","expands capacity","strong demand"]
NEG=["cuts guidance","lower guidance","misses estimates","misses expectations","downgraded","downgrade","price target cut","lawsuit","investigation","recall","delay","delays","postpones","strike","shutdown","outage","security breach","data breach","weak demand","tariff risk","regulatory probe"]
CAT=["guidance","order","contract","partnership","launch","unveil","approval","upgrade","downgrade","price target","lawsuit","investigation","recall","delay","strike","capacity","demand","revenue","earnings","customer","agreement","acquisition","merger","ai","datacenter","data center"]

def safe_float(x):
    try:
        x=float(x)
        return x if math.isfinite(x) else None
    except: return None

def get_quote(symbol):
    t=yf.Ticker(symbol)
    chg=mc=None
    try:
        hist=t.history(period="5d", interval="1d", auto_adjust=False)
        closes=[safe_float(x) for x in hist["Close"].tolist() if safe_float(x) is not None]
        if len(closes)>=2 and closes[-2]:
            chg=(closes[-1]/closes[-2]-1)*100
    except Exception: pass
    try:
        fi=t.fast_info
        mc=safe_float(fi.get("market_cap") if hasattr(fi,"get") else fi["market_cap"])
    except Exception: pass
    return chg,mc

def item_news(display_ticker, symbol, group):
    out=[]
    try:
        raw=yf.Ticker(symbol).news or []
    except Exception:
        raw=[]
    now=datetime.now(timezone.utc)
    for obj in raw[:12]:
        c=obj.get("content",obj) if isinstance(obj,dict) else {}
        title=(c.get("title") or obj.get("title") or "").strip()
        summary=(c.get("summary") or c.get("description") or "").strip()
        url=""
        ctu=c.get("clickThroughUrl") or c.get("canonicalUrl")
        if isinstance(ctu,dict): url=ctu.get("url","")
        elif isinstance(ctu,str): url=ctu
        url=url or obj.get("link","")
        pd=c.get("pubDate") or obj.get("providerPublishTime")
        dt=None
        try:
            if isinstance(pd,(int,float)): dt=datetime.fromtimestamp(pd,timezone.utc)
            elif pd: dt=datetime.fromisoformat(str(pd).replace("Z","+00:00"))
        except: pass
        if dt and now-dt>timedelta(hours=cfg.get("news_lookback_hours",36)): continue
        text=(title+" "+summary).lower()
        if not any(k in text for k in CAT): continue
        tag="題材"
        if any(k in text for k in NEG): tag="利空"
        elif any(k in text for k in POS): tag="利多"
        out.append({"ticker":display_ticker,"group":group,"tag":tag,"title":title,"summary":summary[:260],"url":url,"ts":dt.isoformat() if dt else ""})
    return out

groups=[]
all_news=[]
for group, arr in cfg["groups"].items():
    stocks=[]
    for ticker,name,symbol in arr:
        chg,mc=get_quote(symbol)
        stocks.append({"ticker":ticker,"name":name,"symbol":symbol,"changePct":chg,"marketCap":mc})
        all_news.extend(item_news(ticker,symbol,group))
    weighted=[s for s in stocks if s["changePct"] is not None and s["marketCap"] is not None and s["marketCap"]>0]
    if weighted:
        total=sum(s["marketCap"] for s in weighted)
        ret=sum(s["changePct"]*s["marketCap"] for s in weighted)/total
    else:
        ret=None
    groups.append({"name":group,"returnPct":ret,"stocks":stocks})

# Deduplicate news, newest first
seen=set(); news=[]
for x in sorted(all_news,key=lambda z:z.get("ts",""),reverse=True):
    key=re.sub(r"\W+","",x["title"].lower())
    if not key or key in seen: continue
    seen.add(key); news.append(x)
news=news[:cfg.get("max_news",24)]

tw=datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
(ROOT/"data.json").write_text(json.dumps({"updatedAt":tw+" 台灣時間","mode":"auto","groups":groups},ensure_ascii=False,indent=2),encoding="utf-8")
(ROOT/"news.json").write_text(json.dumps({"updatedAt":tw+" 台灣時間","items":news},ensure_ascii=False,indent=2),encoding="utf-8")
print(f"updated {len(groups)} groups, {len(news)} news items")
