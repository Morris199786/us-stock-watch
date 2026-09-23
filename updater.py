import json, math, os, re, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import yfinance as yf
from deep_translator import GoogleTranslator

ROOT = Path(__file__).resolve().parent
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

POS = [
    "raises guidance","raise guidance","boosts outlook","beats estimates","beats expectations",
    "record revenue","record order","new order","wins contract","contract win","partnership",
    "partners with","launches","unveils","approval","approved","price target raised",
    "upgraded","upgrade","buy rating","expands capacity","strong demand"
]
NEG = [
    "cuts guidance","lower guidance","misses estimates","misses expectations","downgraded",
    "downgrade","price target cut","lawsuit","investigation","recall","delay","delays",
    "postpones","strike","shutdown","outage","security breach","data breach","weak demand",
    "tariff risk","regulatory probe"
]
CAT = [
    "guidance","order","contract","partnership","launch","unveil","approval","upgrade",
    "downgrade","price target","lawsuit","investigation","recall","delay","strike","capacity",
    "demand","revenue","earnings","customer","agreement","acquisition","merger","ai",
    "datacenter","data center"
]

def safe_float(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except Exception:
        return None

def latest_price_from_hist(hist):
    try:
        vals = [safe_float(x) for x in hist["Close"].tolist()]
        vals = [x for x in vals if x is not None and x > 0]
        return vals[-1] if vals else None
    except Exception:
        return None

def robust_market_cap(t, hist):
    # 1) FastInfo – try both property and mapping styles
    try:
        fi = t.fast_info
        for getter in (
            lambda: getattr(fi, "market_cap", None),
            lambda: fi["market_cap"],
            lambda: fi.get("market_cap") if hasattr(fi, "get") else None,
        ):
            try:
                mc = safe_float(getter())
                if mc and mc > 0:
                    return mc, "fast_info"
            except Exception:
                pass
    except Exception:
        pass

    # 2) Full quote info
    info = {}
    try:
        info = t.info or {}
        mc = safe_float(info.get("marketCap"))
        if mc and mc > 0:
            return mc, "info"
    except Exception:
        info = {}

    # 3) Shares outstanding × latest price
    px = latest_price_from_hist(hist)
    try:
        shares = safe_float(info.get("sharesOutstanding")) if info else None
        if shares and shares > 0 and px and px > 0:
            return shares * px, "shares_info"
    except Exception:
        pass

    # 4) Last reported shares × latest price
    if px and px > 0:
        try:
            start = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
            s = t.get_shares_full(start=start)
            if s is not None and len(s):
                shares = safe_float(s.dropna().iloc[-1])
                if shares and shares > 0:
                    return shares * px, "shares_history"
        except Exception:
            pass

    return None, None

def _fi_value(fi, key):
    """Read yfinance FastInfo robustly across mapping/property variants."""
    try:
        v = getattr(fi, key, None)
        v = safe_float(v)
        if v is not None:
            return v
    except Exception:
        pass
    try:
        v = safe_float(fi[key])
        if v is not None:
            return v
    except Exception:
        pass
    try:
        if hasattr(fi, "get"):
            v = safe_float(fi.get(key))
            if v is not None:
                return v
    except Exception:
        pass
    return None

def get_quote(symbol):
    """
    Latest change = latest available price / previous regular close - 1.

    Important:
    We no longer derive the displayed change from the last two daily candles,
    because Yahoo can expose a stale/incomplete final daily candle in Actions.
    """
    t = yf.Ticker(symbol)
    chg = mc = None
    mc_source = None
    hist = None
    last_price = prev_close = None

    # Primary source: FastInfo latest price + previous close
    try:
        fi = t.fast_info
        last_price = _fi_value(fi, "last_price")
        prev_close = _fi_value(fi, "previous_close")

        # Some yfinance versions expose these aliases.
        if last_price is None:
            last_price = _fi_value(fi, "lastPrice")
        if prev_close is None:
            prev_close = _fi_value(fi, "previousClose")

        if last_price is not None and prev_close not in (None, 0):
            chg = (last_price / prev_close - 1) * 100
    except Exception:
        pass

    # Secondary source: quote info. This also helps during Yahoo FastInfo hiccups.
    info = {}
    if chg is None:
        try:
            info = t.info or {}
            last_price = safe_float(
                info.get("currentPrice")
                or info.get("regularMarketPrice")
                or info.get("postMarketPrice")
                or info.get("preMarketPrice")
            )
            prev_close = safe_float(
                info.get("regularMarketPreviousClose")
                or info.get("previousClose")
            )
            if last_price is not None and prev_close not in (None, 0):
                chg = (last_price / prev_close - 1) * 100
        except Exception:
            info = {}

    # Final fallback only: use recent daily candles.
    # This path is deliberately last because it can lag one session.
    try:
        hist = t.history(period="5d", interval="1d", auto_adjust=False)
    except Exception:
        hist = None

    if chg is None and hist is not None:
        try:
            closes = [safe_float(x) for x in hist["Close"].tolist()]
            closes = [x for x in closes if x is not None]
            if len(closes) >= 2 and closes[-2]:
                chg = (closes[-1] / closes[-2] - 1) * 100
        except Exception:
            pass

    # Market cap: prefer FastInfo, then robust fallbacks.
    try:
        fi = t.fast_info
        mc = _fi_value(fi, "market_cap") or _fi_value(fi, "marketCap")
        if mc and mc > 0:
            mc_source = "fast_info"
        else:
            mc = None
    except Exception:
        mc = None

    if mc is None:
        try:
            mc, mc_source = robust_market_cap(t, hist)
        except Exception:
            pass

    return chg, mc, mc_source

# Reuse translations from the prior news.json so we don't translate the same item every 5 minutes
translation_cache = {}

def has_cjk(text):
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))

try:
    old = json.loads((ROOT / "news.json").read_text(encoding="utf-8"))
    for x in old.get("items", []):
        ot = (x.get("originalTitle") or "").strip()
        translated_title = (x.get("title") or "").strip()
        translated_summary = (x.get("summary") or "").strip()
        # Only cache a prior result if it was actually translated to Chinese.
        # This avoids permanently reusing the old English output.
        if ot and has_cjk(translated_title):
            translation_cache[ot] = (translated_title, translated_summary)
except Exception:
    pass

translator = GoogleTranslator(source="auto", target="zh-TW")

def zh_http(text):
    """
    Primary translation path.
    Uses Google's lightweight translate endpoint directly.
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        params = urllib.parse.urlencode({
            "client": "gtx",
            "sl": "auto",
            "tl": "zh-TW",
            "dt": "t",
            "q": text
        })
        url = "https://translate.googleapis.com/translate_a/single?" + params
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"
            }
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        translated = "".join(
            part[0] for part in (data[0] or [])
            if isinstance(part, list) and part and isinstance(part[0], str)
        ).strip()
        if translated and translated != text:
            return translated
    except Exception:
        pass
    return ""

def zh(text):
    text = (text or "").strip()
    if not text:
        return ""

    result = zh_http(text)
    if result and has_cjk(result):
        return result

    for attempt in range(2):
        try:
            result = translator.translate(text)
            if result and has_cjk(result):
                return result
        except Exception:
            if attempt == 0:
                time.sleep(0.15)

    return text


def target_relevance(text, display_ticker, company_name):
    """
    Keep only stories that clearly mention the target company or ticker.
    This avoids loose Yahoo/Google cross-stock matches.
    """
    text = (text or "").lower()
    ticker = (display_ticker or "").strip().lower()
    company = (company_name or "").strip().lower()

    if company and company in text:
        return True

    roots = []
    for token in re.findall(r"[a-z0-9]+", company):
        if len(token) >= 5 and token not in {
            "holdings","technologies","technology","systems","semiconductor",
            "semiconductors","solutions","devices","materials","electronics",
            "international","corporation","company","inc"
        }:
            roots.append(token)

    if any(root in text for root in roots):
        return True

    if len(ticker) >= 3 and re.search(
        rf"(?<![a-z0-9]){re.escape(ticker)}(?![a-z0-9])",
        text
    ):
        return True

    return False

def parse_rss_date(text):
    if not text:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def google_news_search(display_ticker, company_name, symbol, group, move_pct):
    """
    Active event search used for large movers.
    This is the second layer beyond Yahoo's ticker feed, designed to catch
    guidance/order/license/contract/analyst catalysts that Yahoo may miss.
    """
    threshold = cfg.get("active_search_move_threshold", 4.0)
    if move_pct is None or abs(move_pct) < threshold:
        return []

    # Use company name + ticker, and event words that tend to explain sharp moves.
    catalyst_terms = (
        'guidance OR outlook OR earnings OR order OR contract OR licensing OR license '
        'OR customer OR partnership OR upgrade OR downgrade OR "price target" OR acquisition '
        'OR merger OR approval OR capacity OR demand OR AI'
    )
    q = f'("{company_name}" OR {display_ticker}) ({catalyst_terms}) when:2d'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": q,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    })

    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            xml = r.read()
        root = ET.fromstring(xml)
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    out = []
    for item in root.findall(".//item")[:cfg.get("active_search_max_results", 8)]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = parse_rss_date(item.findtext("pubDate"))
        if pub and now - pub > timedelta(hours=cfg.get("news_lookback_hours", 48)):
            continue

        # Google News title usually ends with " - Publisher"; trim publisher only for scoring/translation.
        clean_title = re.sub(r"\s+-\s+[^-]{2,80}$", "", title).strip() or title
        blob = clean_title.lower()

        # Reject stories that don't explicitly reference the stock/company.
        if not target_relevance(clean_title, display_ticker, company_name):
            continue

        # Keep only event-oriented results. Avoid generic price-only stories.
        event_hit = any(k in blob for k in CAT + [
            "license","licensing","outlook","forecast","analyst","target","surges","jumps","rallies",
            "falls","drops","plunges","raises","cuts","deal"
        ])
        if not event_hit:
            continue

        tag = "題材"
        if any(k in blob for k in NEG):
            tag = "利空"
        elif any(k in blob for k in POS) or any(k in blob for k in ["licensing","license agreement","raises outlook","raises forecast"]):
            tag = "利多"

        out.append({
            "ticker": display_ticker,
            "group": group,
            "tag": tag,
            "title": clean_title,
            "summary": "",
            "originalTitle": clean_title,
            "originalSummary": "",
            "url": link,
            "ts": pub.isoformat() if pub else "",
            "sourceType": "active_search",
            "movePct": move_pct
        })
    return out

def item_news(display_ticker, company_name, symbol, group):
    out = []
    try:
        raw = yf.Ticker(symbol).news or []
    except Exception:
        raw = []

    now = datetime.now(timezone.utc)
    for obj in raw[:12]:
        c = obj.get("content", obj) if isinstance(obj, dict) else {}
        title = (c.get("title") or obj.get("title") or "").strip()
        summary = (c.get("summary") or c.get("description") or "").strip()
        url = ""

        ctu = c.get("clickThroughUrl") or c.get("canonicalUrl")
        if isinstance(ctu, dict):
            url = ctu.get("url", "")
        elif isinstance(ctu, str):
            url = ctu
        url = url or obj.get("link", "")

        pd = c.get("pubDate") or obj.get("providerPublishTime")
        dt = None
        try:
            if isinstance(pd, (int, float)):
                dt = datetime.fromtimestamp(pd, timezone.utc)
            elif pd:
                dt = datetime.fromisoformat(str(pd).replace("Z", "+00:00"))
        except Exception:
            pass

        if dt and now - dt > timedelta(hours=cfg.get("news_lookback_hours", 36)):
            continue

        full_text = title + " " + summary
        blob = full_text.lower()

        # Yahoo's ticker feed can contain cross-stock comparison/portfolio stories.
        # Only keep stories that clearly mention the target stock/company.
        if not target_relevance(full_text, display_ticker, company_name):
            continue

        if not any(k in blob for k in CAT):
            continue

        tag = "題材"
        if any(k in blob for k in NEG):
            tag = "利空"
        elif any(k in blob for k in POS):
            tag = "利多"

        out.append({
            "ticker": display_ticker,
            "group": group,
            "tag": tag,
            "title": title,
            "summary": summary[:320],
            "originalTitle": title,
            "originalSummary": summary[:500],
            "url": url,
            "ts": dt.isoformat() if dt else "",
            "sourceType": "ticker_feed"
        })
    return out

groups = []
all_news = []
active_search_candidates = []

for group, arr in cfg["groups"].items():
    stocks = []
    for ticker, name, symbol in arr:
        chg, mc, mc_source = get_quote(symbol)
        stocks.append({
            "ticker": ticker,
            "name": name,
            "symbol": symbol,
            "changePct": chg,
            "marketCap": mc,
            "marketCapSource": mc_source
        })
        feed_items = item_news(ticker, name, symbol, group)
        for x in feed_items:
            x["movePct"] = chg
        all_news.extend(feed_items)

        # Queue sharp movers for a second-layer active search.
        if chg is not None and abs(chg) >= cfg.get("active_search_move_threshold", 4.0):
            active_search_candidates.append((abs(chg), ticker, name, symbol, group, chg))

    changeable = [s for s in stocks if s["changePct"] is not None]
    weighted = [
        s for s in changeable
        if s["marketCap"] is not None and s["marketCap"] > 0
    ]

    # Preferred: true market-cap weighting
    if weighted:
        total = sum(s["marketCap"] for s in weighted)
        ret = sum(s["changePct"] * s["marketCap"] for s in weighted) / total
        weight_mode = "market_cap"
        coverage = len(weighted) / len(changeable) if changeable else 0
    # Safety fallback: never leave heatmap blank just because upstream market-cap data is unavailable
    elif changeable:
        ret = sum(s["changePct"] for s in changeable) / len(changeable)
        weight_mode = "equal_fallback"
        coverage = 0
    else:
        ret = None
        weight_mode = "no_data"
        coverage = 0

    groups.append({
        "name": group,
        "returnPct": ret,
        "weightMode": weight_mode,
        "marketCapCoverage": coverage,
        "stocks": stocks
    })

# Second-layer active search: only the biggest movers each run.
# This keeps the 5-minute workflow fast while still targeting the names most likely to have a fresh catalyst.
active_search_candidates.sort(reverse=True, key=lambda x: x[0])
for _, ticker, name, symbol, group, chg in active_search_candidates[:cfg.get("active_search_top_movers", 10)]:
    all_news.extend(google_news_search(ticker, name, symbol, group, chg))

# Score + deduplicate news.
# Goal: "latest + important", rather than simply newest.
def importance_score(x):
    blob = ((x.get("originalTitle") or "") + " " + (x.get("originalSummary") or "")).lower()

    score = 1.0
    # Highest-impact company events
    if any(k in blob for k in ["raises guidance","raise guidance","cuts guidance","lower guidance"]):
        score += 5
    if any(k in blob for k in ["new order","record order","wins contract","contract win","major contract","licensing deal","license agreement"]):
        score += 5
    if any(k in blob for k in ["beats estimates","misses estimates","beats expectations","misses expectations","earnings"]):
        score += 4
    if any(k in blob for k in ["acquisition","merger","approval","approved","partnership","partners with","launches","unveils"]):
        score += 4
    if any(k in blob for k in ["upgraded","upgrade","downgraded","downgrade","price target raised","price target cut","buy rating"]):
        score += 3
    if any(k in blob for k in ["ai","datacenter","data center","capacity","demand","customer"]):
        score += 2

    # Sharp price moves make same-day company news more relevant to the investor.
    # This is a relevance boost, not a claim that the article caused the move.
    mv = safe_float(x.get("movePct"))
    if mv is not None:
        a = abs(mv)
        if a >= 15:
            score += 6
        elif a >= 10:
            score += 5
        elif a >= 7:
            score += 4
        elif a >= 4:
            score += 3

    if x.get("sourceType") == "active_search":
        score += 1.5

    # Explicit target-company relevance gets a small bonus.
    score += 1.0

    # Time decay
    try:
        dt = datetime.fromisoformat(x.get("ts","").replace("Z","+00:00"))
        age_h = max(0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
        if age_h <= 6:
            score *= 1.5
        elif age_h <= 12:
            score *= 1.3
        elif age_h <= 24:
            score *= 1.1
        elif age_h <= 48:
            score *= 0.8
        else:
            score *= 0.6
    except Exception:
        pass

    return round(score, 3)

seen = set()
news = []
for x in sorted(all_news, key=lambda z: z.get("ts", ""), reverse=True):
    raw_key = x.get("originalTitle") or x.get("title") or ""
    key = re.sub(r"\W+", "", raw_key.lower())
    if not key or key in seen:
        continue
    seen.add(key)
    x["score"] = importance_score(x)
    news.append(x)

# Keep a broader searchable pool, but mark the strongest stories for the default homepage.
news.sort(key=lambda z: (z.get("score", 0), z.get("ts", "")), reverse=True)
per_ticker = {}
ranked = []
for x in news:
    t = x.get("ticker","")
    if per_ticker.get(t, 0) >= 3:
        continue
    per_ticker[t] = per_ticker.get(t, 0) + 1
    ranked.append(x)

search_pool = ranked[:cfg.get("search_news_pool", 80)]
for i, x in enumerate(search_pool):
    x["featured"] = i < cfg.get("featured_news", 18)

news = search_pool

# Translate only the final selected pool. This is much faster than translating every raw candidate.
for i, x in enumerate(news):
    raw_title = (x.get("originalTitle") or x.get("title") or "").strip()
    raw_summary = (x.get("originalSummary") or x.get("summary") or "").strip()

    if raw_title in translation_cache:
        cached_title, cached_summary = translation_cache[raw_title]
        x["title"] = cached_title or raw_title
        x["translated"] = has_cjk(x["title"])
        if i < cfg.get("featured_news", 18):
            x["summary"] = (cached_summary or raw_summary)[:320]
        else:
            x["summary"] = ""
        continue

    translated_title = zh(raw_title)
    x["title"] = translated_title or raw_title
    x["translated"] = has_cjk(x["title"])

    # Only translate summaries for the featured homepage stories.
    if i < cfg.get("featured_news", 18) and raw_summary:
        translated_summary = zh(raw_summary[:420])
        x["summary"] = (translated_summary or raw_summary)[:320]
    else:
        x["summary"] = ""

tw = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
(ROOT / "data.json").write_text(
    json.dumps({"updatedAt": tw + " 台灣時間", "mode": "auto", "groups": groups},
               ensure_ascii=False, indent=2),
    encoding="utf-8"
)
(ROOT / "news.json").write_text(
    json.dumps({"updatedAt": tw + " 台灣時間", "items": news},
               ensure_ascii=False, indent=2),
    encoding="utf-8"
)
print(f"updated {len(groups)} groups, {len(news)} news items")
