import csv, io, json, math, os, re, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Reuse translations/history from prior news.json.
translation_cache = {}
old_news_items = []
old_history_items = []
old_history_date = ""
old_history_last_attempt = ""

def has_cjk(text):
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))

try:
    old = json.loads((ROOT / "news.json").read_text(encoding="utf-8"))
    old_news_items = old.get("items", []) or []
    old_history_items = old.get("historyItems", []) or []
    old_history_date = (old.get("historyUpdatedDate") or "").strip()
    old_history_last_attempt = (old.get("historyLastAttemptAt") or "").strip()
    for x in (old_news_items + old_history_items):
        ot = (x.get("originalTitle") or "").strip()
        translated_title = (x.get("title") or "").strip()
        translated_summary = (x.get("summary") or "").strip()
        if ot and has_cjk(translated_title):
            translation_cache[ot] = (translated_title, translated_summary)
except Exception:
    old_news_items = []
    old_history_items = []
    old_history_date = ""
    old_history_last_attempt = ""

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



BROKER_NAMES = [
    "Evercore ISI", "Citi", "Citigroup", "JPMorgan", "JP Morgan", "Morgan Stanley",
    "Goldman Sachs", "Bank of America", "BofA", "UBS", "Jefferies", "Barclays",
    "Wells Fargo", "Bernstein", "Mizuho", "Needham", "Raymond James", "TD Cowen",
    "Cowen", "Stifel", "Susquehanna", "Rosenblatt", "KeyBanc", "Piper Sandler",
    "Truist", "Deutsche Bank", "Cantor Fitzgerald", "Baird", "Oppenheimer",
    "Loop Capital", "Wedbush", "Benchmark", "HSBC", "BMO Capital", "Scotiabank",
    "DA Davidson", "William Blair", "Craig-Hallum"
]

def _fmt_target(v):
    try:
        f = float(v.replace(",", ""))
        if f.is_integer():
            return f"${int(f):,}"
        return f"${f:,.2f}".rstrip("0").rstrip(".")
    except Exception:
        return f"${v}"

def normalize_analyst_title(x):
    """
    Turn analyst/broker headlines into investor-friendly Traditional Chinese.
    Examples:
      Evercore ISI 上修 CIEN 目標價至 $550（原 $375）
      Citi 下修 MU 目標價至 $120（原 $135）
      JPMorgan 升評 AMD 至 Overweight，目標價上修至 $250
    Falls back to the translated/original title if key fields cannot be extracted.
    """
    if not x.get("analystPriority"):
        return x.get("title") or x.get("originalTitle") or ""

    raw_title = (x.get("originalTitle") or "").strip()
    raw_summary = (x.get("originalSummary") or "").strip()
    text = f"{raw_title} {raw_summary}"
    low = text.lower()
    ticker = (x.get("ticker") or "").strip()

    broker = ""
    for name in BROKER_NAMES:
        if name.lower() in low:
            broker = name
            break

    # Detect rating action
    rating_action = ""
    rating = ""
    upgrade_words = [
        "upgraded to", "upgrade to", "raised to", "initiated with", "initiates with",
        "initiated at", "initiates at"
    ]
    downgrade_words = ["downgraded to", "downgrade to", "lowered to", "cut to"]

    for p in upgrade_words:
        m = re.search(re.escape(p) + r"\s+([A-Za-z][A-Za-z \-/]+?)(?:[,.]| from | with | and |$)", text, re.I)
        if m:
            rating_action = "升評"
            rating = m.group(1).strip()
            break
    if not rating_action:
        for p in downgrade_words:
            m = re.search(re.escape(p) + r"\s+([A-Za-z][A-Za-z \-/]+?)(?:[,.]| from | with | and |$)", text, re.I)
            if m:
                rating_action = "降評"
                rating = m.group(1).strip()
                break

    # Detect target price patterns
    old_target = ""
    new_target = ""

    patterns = [
        r"(?:price target|target price|target)\s+(?:was\s+)?(?:raised|increased|boosted|lifted)\s+(?:to\s+)?\$?([\d,.]+)\s+(?:from|vs\.?|versus)\s+\$?([\d,.]+)",
        r"(?:price target|target price|target)\s+(?:was\s+)?(?:cut|lowered|reduced)\s+(?:to\s+)?\$?([\d,.]+)\s+(?:from|vs\.?|versus)\s+\$?([\d,.]+)",
        r"(?:raised|increased|boosted|lifted)\s+(?:its\s+)?(?:price target|target price|target)\s+(?:to\s+)?\$?([\d,.]+)\s+(?:from|vs\.?|versus)\s+\$?([\d,.]+)",
        r"(?:cut|lowered|reduced)\s+(?:its\s+)?(?:price target|target price|target)\s+(?:to\s+)?\$?([\d,.]+)\s+(?:from|vs\.?|versus)\s+\$?([\d,.]+)",
        r"(?:price target|target price|target)\s+(?:from\s+)?\$?([\d,.]+)\s+(?:to|→)\s+\$?([\d,.]+)",
        r"from\s+\$?([\d,.]+)\s+(?:to|→)\s+\$?([\d,.]+)"
    ]

    for idx, pat in enumerate(patterns):
        m = re.search(pat, text, re.I)
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        if idx in (0,1,2,3):
            new_target, old_target = a, b
        else:
            old_target, new_target = a, b
        break

    # Single "to $X" pattern if old target isn't available
    if not new_target:
        m = re.search(
            r"(?:price target|target price|target)\s+(?:raised|increased|boosted|lifted|cut|lowered|reduced)?\s*(?:to|at)\s+\$?([\d,.]+)",
            text, re.I
        )
        if m:
            new_target = m.group(1)

    # Determine target direction
    target_action = ""
    if any(k in low for k in ["price target raised", "raises price target", "raised its price target",
                               "target raised", "boosts price target", "increases price target",
                               "lifted its price target", "price target increased"]):
        target_action = "上修"
    elif any(k in low for k in ["price target cut", "cuts price target", "cut its price target",
                                 "target cut", "lowers price target", "lowered its price target",
                                 "reduced price target", "price target lowered"]):
        target_action = "下修"
    elif old_target and new_target:
        try:
            target_action = "上修" if float(new_target.replace(",","")) > float(old_target.replace(",","")) else "下修"
        except Exception:
            pass

    # Build concise investor-style headline
    if broker and ticker and new_target:
        newp = _fmt_target(new_target)
        oldp = _fmt_target(old_target) if old_target else ""

        if rating_action and rating:
            rating = re.sub(r"\s+", " ", rating).strip()
            if target_action:
                title = f"{broker} {rating_action} {ticker} 至 {rating}，目標價{target_action}至 {newp}"
            else:
                title = f"{broker} {rating_action} {ticker} 至 {rating}，目標價至 {newp}"
        else:
            verb = target_action or "調整"
            title = f"{broker} {verb} {ticker} 目標價至 {newp}"

        if oldp:
            title += f"（原 {oldp}）"
        return title

    if broker and ticker and rating_action:
        return f"{broker} {rating_action} {ticker}" + (f" 至 {rating}" if rating else "")

    return x.get("title") or raw_title

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


BROKER_POS = [
    "price target raised", "raises price target", "raised price target", "target raised",
    "price target increased", "increases price target", "boosts price target",
    "upgraded to buy", "upgraded to outperform", "upgraded to overweight",
    "upgrade to buy", "upgrade to outperform", "upgrade to overweight",
    "initiates with buy", "initiates at buy", "initiates with outperform",
    "initiates with overweight", "reiterates buy", "reiterates outperform",
    "reiterates overweight"
]
BROKER_NEG = [
    "price target cut", "cuts price target", "cut price target", "target cut",
    "price target lowered", "lowers price target", "reduced price target",
    "downgraded to sell", "downgraded to underperform", "downgraded to underweight",
    "downgrade to sell", "downgrade to underperform", "downgrade to underweight",
    "initiates with sell", "initiates at sell", "initiates with underperform",
    "initiates with underweight", "reiterates sell", "reiterates underperform",
    "reiterates underweight"
]
BROKER_ACTION = [
    "price target", "target price", "upgraded", "downgraded", "upgrade", "downgrade",
    "initiates coverage", "initiates with", "initiates at", "reiterates",
    "rating raised", "rating cut", "raises target", "cuts target",
    "lowers target", "boosts target"
]

def broker_news_search(display_ticker, company_name, symbol, group):
    """
    Dedicated analyst/broker scan for every watchlist stock.
    These stories get first priority regardless of the stock's price move.
    """
    q = (
        f'("{company_name}" OR {display_ticker}) '
        '("price target" OR "target price" OR upgraded OR downgraded OR '
        '"initiates coverage" OR "initiates with" OR reiterates OR '
        '"raises target" OR "cuts target" OR "lowers target" OR "boosts target") when:2d'
    )
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": q,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    })

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            xml = r.read()
        root = ET.fromstring(xml)
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    out = []
    for item in root.findall(".//item")[:6]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = parse_rss_date(item.findtext("pubDate"))
        if pub and now - pub > timedelta(hours=cfg.get("news_lookback_hours", 48)):
            continue

        clean_title = re.sub(r"\s+-\s+[^-]{2,80}$", "", title).strip() or title
        blob = clean_title.lower()

        if not target_relevance(clean_title, display_ticker, company_name):
            continue
        if not any(k in blob for k in BROKER_ACTION):
            continue

        tag = "題材"
        if any(k in blob for k in BROKER_NEG):
            tag = "利空"
        elif any(k in blob for k in BROKER_POS):
            tag = "利多"
        else:
            if "downgrad" in blob or "cuts target" in blob or "lowers target" in blob:
                tag = "利空"
            elif "upgrad" in blob or "raises target" in blob or "boosts target" in blob:
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
            "sourceType": "broker_search",
            "analystPriority": True
        })
    return out


def history_news_search(display_ticker, company_name, symbol, group):
    """
    30-day backfill used by ticker search.
    Query is intentionally broad; relevance filtering happens after retrieval.
    """
    q = f'("{company_name}" OR "{display_ticker}") when:30d'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": q,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    })

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            xml = r.read()
        root = ET.fromstring(xml)
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    out = []
    for item in root.findall(".//item")[:cfg.get("history_max_results_per_ticker", 8)]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = parse_rss_date(item.findtext("pubDate"))
        if pub and now - pub > timedelta(days=30):
            continue

        clean_title = re.sub(r"\s+-\s+[^-]{2,80}$", "", title).strip() or title
        blob = clean_title.lower()

        if not target_relevance(clean_title, display_ticker, company_name):
            continue

        tag = "題材"
        if any(k in blob for k in NEG + BROKER_NEG):
            tag = "利空"
        elif any(k in blob for k in POS + BROKER_POS):
            tag = "利多"

        analyst_priority = any(k in blob for k in BROKER_ACTION)

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
            "sourceType": "history_search",
            "analystPriority": analyst_priority
        })
    return out

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

        analyst_priority = any(k in blob for k in BROKER_ACTION)

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
            "sourceType": "ticker_feed",
            "analystPriority": analyst_priority
        })
    return out

groups = []
all_news = []
active_search_candidates = []
broker_scan_candidates = []
history_scan_candidates = []

for group, arr in cfg["groups"].items():
    stocks = []
    for ticker, name, symbol in arr:
        chg, mc, mc_source = get_quote(symbol)
        broker_scan_candidates.append((ticker, name, symbol, group))
        history_scan_candidates.append((ticker, name, symbol, group))
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

# Retain prior searchable news for up to 30 days.
# This lets ticker search keep history without re-fetching the whole month every 5 minutes.
now_utc = datetime.now(timezone.utc)
for x in old_news_items:
    try:
        dt = datetime.fromisoformat((x.get("ts") or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if now_utc - dt <= timedelta(days=30):
            all_news.append(x)
    except Exception:
        pass

# Dedicated broker/analyst scan across the full watchlist.
# Run concurrently so upgrades/downgrades/target changes are checked every cycle.
broker_items = []
with ThreadPoolExecutor(max_workers=14) as ex:
    futures = {
        ex.submit(broker_news_search, ticker, name, symbol, group): ticker
        for ticker, name, symbol, group in broker_scan_candidates
    }
    for fut in as_completed(futures):
        try:
            broker_items.extend(fut.result())
        except Exception:
            pass
all_news.extend(broker_items)

# Refresh the 30-day searchable history pool once per day.
# IMPORTANT: only mark the day complete when history items were actually fetched.
today_tw = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
existing_history_count = len(old_history_items) + sum(1 for x in old_news_items if x.get("sourceType") == "history_search")

def _history_retry_allowed():
    if old_history_date != today_tw:
        if not old_history_last_attempt:
            return True
        try:
            last = datetime.fromisoformat(old_history_last_attempt.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - last >= timedelta(minutes=60)
        except Exception:
            return True
    # If a previous buggy run marked today complete but produced no history, retry.
    return existing_history_count < cfg.get("history_min_items", 20)

history_attempted = False
history_new_count = 0
history_items = []
if _history_retry_allowed():
    history_attempted = True
    old_history_last_attempt = datetime.now(timezone.utc).isoformat()

    history_items = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {
            ex.submit(history_news_search, ticker, name, symbol, group): ticker
            for ticker, name, symbol, group in history_scan_candidates
        }
        for fut in as_completed(futures):
            try:
                history_items.extend(fut.result())
            except Exception:
                pass

    history_new_count = len(history_items)
    all_news.extend(history_items)

    # Only mark success if we actually created a meaningful history pool.
    if history_new_count >= cfg.get("history_min_items", 20):
        old_history_date = today_tw


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

    # Broker upgrades/downgrades/price-target changes are always first priority.
    if x.get("analystPriority"):
        score += 100

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

# Keep a broad 30-day searchable pool. Homepage featured stories are selected separately.
news.sort(key=lambda z: (1 if z.get("analystPriority") else 0, z.get("score", 0), z.get("ts", "")), reverse=True)

search_pool = news[:cfg.get("search_news_pool", 500)]

featured_count = cfg.get("featured_news", 18)
featured_per_ticker = {}
featured_ids = set()
for x in news:
    t = x.get("ticker", "")
    if not x.get("analystPriority") and featured_per_ticker.get(t, 0) >= 3:
        continue
    featured_per_ticker[t] = featured_per_ticker.get(t, 0) + 1
    featured_ids.add(id(x))
    if len(featured_ids) >= featured_count:
        break

for x in search_pool:
    x["featured"] = id(x) in featured_ids

news = search_pool


# Build an INDEPENDENT 30-day ticker-search database.
# It is not ranked against homepage news, so it cannot be squeezed out by the homepage cap.
history_candidates = []
history_candidates.extend(old_history_items)
history_candidates.extend(history_items)
history_candidates.extend([x for x in all_news if x.get("sourceType") == "history_search"])

history_seen = set()
history_db = []
history_cutoff = datetime.now(timezone.utc) - timedelta(days=30)

for x in sorted(history_candidates, key=lambda z: z.get("ts", ""), reverse=True):
    try:
        dt = datetime.fromisoformat((x.get("ts") or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < history_cutoff:
            continue
    except Exception:
        continue

    raw_title = (x.get("originalTitle") or x.get("title") or "").strip()
    ticker_key = (x.get("ticker") or "").strip().upper()
    key = ticker_key + "|" + re.sub(r"\W+", "", raw_title.lower())
    if not ticker_key or not raw_title or key in history_seen:
        continue
    history_seen.add(key)
    x["featured"] = False
    history_db.append(x)

# Cap only the database size, not per-ticker history.
history_db = history_db[:cfg.get("history_news_pool", 800)]

# Translate historical titles only when they are not already cached.
history_uncached = {}
for x in history_db:
    raw_title = (x.get("originalTitle") or x.get("title") or "").strip()
    if raw_title and raw_title not in translation_cache:
        history_uncached[raw_title] = None

if history_uncached:
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(zh, title): title for title in history_uncached}
        for fut in as_completed(futs):
            title = futs[fut]
            try:
                translated = fut.result() or title
            except Exception:
                translated = title
            history_uncached[title] = translated

for x in history_db:
    raw_title = (x.get("originalTitle") or x.get("title") or "").strip()
    if raw_title in translation_cache:
        cached_title, _ = translation_cache[raw_title]
        x["title"] = cached_title or raw_title
    else:
        x["title"] = history_uncached.get(raw_title) or raw_title
    x["translated"] = has_cjk(x.get("title", ""))
    x["summary"] = ""
    if x.get("analystPriority"):
        x["title"] = normalize_analyst_title(x)

# A daily backfill is considered complete only if the actually retained DB is meaningful.
history_min_items = cfg.get("history_min_items", 20)
if len(history_db) >= history_min_items:
    old_history_date = today_tw
elif old_history_date == today_tw:
    # Do not falsely mark today as successful if the stored DB is still too small.
    old_history_date = ""

# Translate final pool. Titles are parallelized because the 30-day pool can be larger.
uncached_titles = {}
for x in news:
    raw_title = (x.get("originalTitle") or x.get("title") or "").strip()
    if raw_title and raw_title not in translation_cache:
        uncached_titles[raw_title] = None

if uncached_titles:
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(zh, title): title for title in uncached_titles}
        for fut in as_completed(futs):
            title = futs[fut]
            try:
                translated = fut.result() or title
            except Exception:
                translated = title
            uncached_titles[title] = translated

for x in news:
    raw_title = (x.get("originalTitle") or x.get("title") or "").strip()
    raw_summary = (x.get("originalSummary") or x.get("summary") or "").strip()

    if raw_title in translation_cache:
        cached_title, cached_summary = translation_cache[raw_title]
        x["title"] = cached_title or raw_title
        x["translated"] = has_cjk(x["title"])
        if x.get("featured") and raw_summary:
            x["summary"] = (cached_summary or raw_summary)[:320]
        else:
            x["summary"] = ""
        continue

    translated_title = uncached_titles.get(raw_title) or raw_title
    x["title"] = translated_title
    x["translated"] = has_cjk(x["title"])

    # Only featured homepage stories get translated summaries.
    if x.get("featured") and raw_summary:
        translated_summary = zh(raw_summary[:420])
        x["summary"] = (translated_summary or raw_summary)[:320]
    else:
        x["summary"] = ""

# Normalize broker/analyst headlines after translation so key investment facts are visible at a glance.
for x in news:
    if x.get("analystPriority"):
        x["title"] = normalize_analyst_title(x)


# ---------- U.S. Congress trades ----------
# Historical source: Kadoa open Congress Trading Monitor (official House/Senate filings normalized)
# We keep records from 2024-01-01 onward so member searches can reach back to 2024.
CONGRESS_FILE = ROOT / "congress.json"
CONGRESS_SOURCE = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"
CONGRESS_FILERS_SOURCE = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/filers.json"
CONGRESS_START_DATE = datetime(2024, 1, 1).date()

def _parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None

def _owner_label(owner):
    o = (owner or "").strip().upper()
    return {
        "SP": "配偶",
        "JT": "共同",
        "DC": "子女",
        "SELF": "本人",
    }.get(o, owner.strip() if owner else "本人")

def _trade_action(raw):
    t = (raw or "").strip().lower()
    if "purchase" in t or t in {"buy", "p"}:
        return "buy", "買進"
    if "sale" in t or "sell" in t or t.startswith("s"):
        return "sell", "賣出"
    if "exchange" in t:
        return "exchange", "交換"
    return "other", (raw or "其他")

def _member_backtest(items):
    """
    Descriptive historical stats from the source's per-trade return fields.
    Uses BUY transactions only, beginning 2024.
    ret_30d / ret_1y are source-computed stock returns after the transaction date.
    """
    by_member = {}
    for x in items:
        if x.get("action") != "buy":
            continue
        m = x.get("member") or ""
        if not m:
            continue
        box = by_member.setdefault(m, {
            "buyCount": 0,
            "ret30Values": [],
            "ret1yValues": [],
        })
        box["buyCount"] += 1
        r30 = safe_float(x.get("backtest30d"))
        r1y = safe_float(x.get("backtest1y"))
        if r30 is not None:
            box["ret30Values"].append(r30)
        if r1y is not None:
            box["ret1yValues"].append(r1y)

    out = {}
    for m, b in by_member.items():
        r30 = b["ret30Values"]
        r1y = b["ret1yValues"]
        out[m] = {
            "buyCount": b["buyCount"],
            "sample30d": len(r30),
            "avg30d": (sum(r30) / len(r30)) if r30 else None,
            "winRate30d": (sum(1 for v in r30 if v > 0) / len(r30) * 100) if r30 else None,
            "sample1y": len(r1y),
            "avg1y": (sum(r1y) / len(r1y)) if r1y else None,
        }
    return out

def refresh_congress_data():
    today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
    try:
        previous = json.loads(CONGRESS_FILE.read_text(encoding="utf-8"))
        # Skip only when today's file is already the NEW 2024+ Kadoa/backtest format.
        # This forces migration away from the old InsiderWatch file even if it was
        # already refreshed earlier on the same day.
        is_new_format = (
            previous.get("updatedDate") == today
            and previous.get("items")
            and previous.get("source") == "Kadoa Congress Trading Monitor open dataset"
            and previous.get("historyStart") == "2024-01-01"
            and isinstance(previous.get("memberBacktests"), dict)
        )
        if is_new_format:
            return
    except Exception:
        previous = {}

    try:
        req = urllib.request.Request(
            CONGRESS_SOURCE,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}
        )
        with urllib.request.urlopen(req, timeout=35) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except Exception:
        # Keep last good file on source outage.
        return

    # Kadoa exports a list of normalized trades.
    if isinstance(raw, dict):
        raw = raw.get("trades") or raw.get("items") or []
    if not isinstance(raw, list):
        return

    # Full Congress filer directory. Global trades.json is capped to recent rows,
    # so the webpage loads a member-specific history file on demand.
    filers = []
    try:
        freq = urllib.request.Request(
            CONGRESS_FILERS_SOURCE,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}
        )
        with urllib.request.urlopen(freq, timeout=20) as r:
            fraw = json.loads(r.read().decode("utf-8"))
        if isinstance(fraw, list):
            for f in fraw:
                if not isinstance(f, dict) or (f.get("branch") or "").lower() != "congress":
                    continue
                filers.append({
                    "id": (f.get("id") or "").strip(),
                    "name": (f.get("full_name") or "").strip(),
                    "chamber": (f.get("chamber") or "").strip().lower(),
                    "party": f.get("party"),
                    "state": f.get("state"),
                    "office": f.get("office"),
                    "tradeCount": f.get("trade_count"),
                })
    except Exception:
        filers = previous.get("filers", []) if isinstance(previous, dict) else []

    items = []
    seen = set()

    for row in raw:
        if not isinstance(row, dict):
            continue

        # Congress only; exclude executive-branch disclosure rows.
        if (row.get("branch") or "").lower() not in ("", "congress"):
            continue

        traded = _parse_date(row.get("transaction_date"))
        filed = _parse_date(row.get("filing_date"))
        if not traded or traded < CONGRESS_START_DATE:
            continue

        member = (row.get("filer_name") or row.get("member") or "").strip()
        ticker = (row.get("ticker") or "").strip().upper()
        if not member:
            continue

        action, action_zh = _trade_action(row.get("transaction_type") or row.get("action"))
        chamber = (row.get("chamber") or "").strip().lower()
        chamber_zh = "參議院" if chamber == "senate" else "眾議院" if chamber == "house" else chamber

        amount_label = (row.get("amount_range_label") or row.get("amount_range") or "").strip()
        owner = _owner_label(row.get("owner"))

        key = "|".join([
            member.lower(),
            ticker,
            action,
            traded.isoformat(),
            amount_label,
            owner
        ])
        if key in seen:
            continue
        seen.add(key)

        days_to_file = row.get("days_to_file")
        try:
            days_to_file = int(float(days_to_file)) if days_to_file is not None else (
                (filed - traded).days if filed else None
            )
        except Exception:
            days_to_file = (filed - traded).days if filed else None

        items.append({
            "member": member,
            "memberId": (row.get("filer_id") or "").strip(),
            "chamber": chamber,
            "chamberZh": chamber_zh,
            "party": row.get("party"),
            "state": row.get("state"),
            "ticker": ticker,
            "asset": (row.get("asset_name") or row.get("asset") or "").strip(),
            "assetType": row.get("asset_type"),
            "action": action,
            "actionZh": action_zh,
            "amountRange": amount_label,
            "amountMinUsd": safe_float(row.get("amount_range_low") or row.get("amount_min_usd")),
            "amountMaxUsd": safe_float(row.get("amount_range_high")),
            "transactionDate": traded.isoformat(),
            "filedDate": filed.isoformat() if filed else "",
            "disclosureLagDays": days_to_file,
            "owner": owner,
            "filingId": (row.get("filing_id") or "").strip(),
            "sourceDoc": (row.get("doc_url") or "").strip(),
            # Source-computed historical return fields, used for backtest summary.
            "backtest30d": safe_float(row.get("ret_30d")),
            "backtest1y": safe_float(row.get("ret_1y")),
        })

    if not items:
        return

    items.sort(key=lambda x: (x.get("filedDate") or "", x.get("transactionDate") or ""), reverse=True)

    today_date = datetime.now(timezone.utc).date()
    seven_days = today_date - timedelta(days=7)
    recent7 = buys7 = sells7 = 0
    members = set()
    for x in items:
        members.add(x["member"])
        fd = _parse_date(x.get("filedDate"))
        if fd and fd >= seven_days:
            recent7 += 1
            if x.get("action") == "buy":
                buys7 += 1
            elif x.get("action") == "sell":
                sells7 += 1

    member_backtests = _member_backtest(items)

    payload = {
        "updatedAt": datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M 台灣時間"),
        "updatedDate": today,
        "historyStart": "2024-01-01",
        "source": "Kadoa Congress Trading Monitor open dataset",
        "sourceUrl": "https://github.com/kadoa-org/congress-trading-monitor",
        "sourceNote": "Normalized from official House Clerk and Senate financial disclosure filings",
        "backtestBasis": "transaction_date",
        "backtestNote": "回測欄位為來源資料提供的交易日後股票報酬；不是申報日後報酬",
        "summary": {
            "recent7": recent7,
            "buys7": buys7,
            "sells7": sells7,
            "members": len(filers) if filers else len(members),
            "total": len(items)
        },
        "filers": filers,
        "memberBacktests": member_backtests,
        "items": items
    }
    CONGRESS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

refresh_congress_data()

tw = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
(ROOT / "data.json").write_text(
    json.dumps({"updatedAt": tw + " 台灣時間", "mode": "auto", "groups": groups},
               ensure_ascii=False, indent=2),
    encoding="utf-8"
)
(ROOT / "news.json").write_text(
    json.dumps({"updatedAt": tw + " 台灣時間", "historyUpdatedDate": old_history_date, "historyLastAttemptAt": old_history_last_attempt, "historyItemCount": len(history_db), "items": news, "historyItems": history_db},
               ensure_ascii=False, indent=2),
    encoding="utf-8"
)
print(f"updated {len(groups)} groups, {len(news)} news items")
