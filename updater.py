import csv, io, json, math, os, re, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET, html as html_lib
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import yfinance as yf
from deep_translator import GoogleTranslator

ROOT = Path(__file__).resolve().parent
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
PUSH_STATE_FILE = ROOT / "push_state.json"

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
    Main displayed change = regular-session change only.
    Pre-market and post-market moves are stored separately and never overwrite it.
    """
    t = yf.Ticker(symbol)

    regular_chg = None
    pre_chg = None
    post_chg = None
    market_state = ""
    regular_price = prev_close = None
    pre_price = post_price = None
    mc = None
    mc_source = None
    hist = None
    info = {}

    # Session-aware Yahoo quote fields
    try:
        info = t.info or {}
        market_state = str(info.get("marketState") or "").upper()

        regular_price = safe_float(info.get("regularMarketPrice"))
        prev_close = safe_float(
            info.get("regularMarketPreviousClose")
            or info.get("previousClose")
        )
        pre_price = safe_float(info.get("preMarketPrice"))
        post_price = safe_float(info.get("postMarketPrice"))

        # Main number: regular session only
        if regular_price is not None and prev_close not in (None, 0):
            regular_chg = (regular_price / prev_close - 1) * 100

        # Extended sessions are separate
        if pre_price is not None:
            pre_base = regular_price or prev_close
            if pre_base not in (None, 0):
                pre_chg = (pre_price / pre_base - 1) * 100

        if post_price is not None and regular_price not in (None, 0):
            post_chg = (post_price / regular_price - 1) * 100
    except Exception:
        info = {}

    # Fallback for the main regular-session change only
    try:
        hist = t.history(period="5d", interval="1d", auto_adjust=False)
    except Exception:
        hist = None

    if regular_chg is None and hist is not None:
        try:
            closes = [safe_float(x) for x in hist["Close"].tolist()]
            closes = [x for x in closes if x is not None and x > 0]
            if len(closes) >= 2 and closes[-2]:
                regular_price = closes[-1]
                prev_close = closes[-2]
                regular_chg = (regular_price / prev_close - 1) * 100
        except Exception:
            pass

    # Market cap only; FastInfo.last_price is intentionally NOT used for changePct.
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

    return {
        "changePct": regular_chg,
        "regularPrice": regular_price,
        "previousClose": prev_close,
        "preMarketChangePct": pre_chg,
        "preMarketPrice": pre_price,
        "postMarketChangePct": post_chg,
        "postMarketPrice": post_price,
        "marketState": market_state,
        "marketCap": mc,
        "marketCapSource": mc_source,
    }

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


# URL/content guards must be defined before cached-news cleanup runs.
BAD_ARTICLE_HOSTS = {
    "www.w3.org", "w3.org", "schema.org", "www.schema.org",
    "fonts.googleapis.com", "fonts.gstatic.com", "www.google-analytics.com",
    "google-analytics.com", "googletagmanager.com", "www.googletagmanager.com",
}
BAD_ARTICLE_PATH_BITS = (
    "/2000/svg", "/svg", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".css", ".js", ".woff", ".woff2", ".ico", "/favicon"
)

def _is_valid_article_url(url):
    try:
        p = urllib.parse.urlparse(url or "")
        host = (p.hostname or "").lower()
        path = (p.path or "").lower()
        if p.scheme not in ("http", "https") or not host:
            return False
        if host in BAD_ARTICLE_HOSTS:
            return False
        if any(bit in path for bit in BAD_ARTICLE_PATH_BITS):
            return False
        return True
    except Exception:
        return False

def _is_polluted_text(text):
    low = (text or "").lower()
    bad = [
        "http://www.w3.org/2000/svg",
        "https://www.w3.org/2000/svg",
        "svg is an xml namespace",
        "svg namespace is mutable",
        "scalable vector graphics (svg)",
        "namespaces in xml specification",
    ]
    return any(x in low for x in bad)

# Drop cached bad resolver results from older builds so they do not keep
# re-entering the 30-day history / translation cache.
def _clean_cached_news(items):
    out = []
    for x in items or []:
        if not isinstance(x, dict):
            continue
        blob = " ".join([
            str(x.get("url") or ""),
            str(x.get("summary") or ""),
            str(x.get("originalSummary") or ""),
        ])
        if _is_polluted_text(blob):
            continue
        if x.get("url") and not _is_valid_article_url(x.get("url")):
            # Keep Google News redirect links, but reject known asset/schema URLs.
            host = (urllib.parse.urlparse(x.get("url")).hostname or "").lower()
            if host in BAD_ARTICLE_HOSTS:
                continue
        out.append(x)
    return out

old_news_items = _clean_cached_news(old_news_items)
old_history_items = _clean_cached_news(old_history_items)
translation_cache = {
    k: v for k, v in translation_cache.items()
    if not _is_polluted_text(" ".join(str(z or "") for z in v))
}

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
    "DA Davidson", "William Blair", "Craig-Hallum", "Tigress Financial", "Tigress"
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
        # Match broker names as standalone words/phrases.
        # This prevents false matches such as "Citi" inside "citing".
        pat = r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])"
        if re.search(pat, text, re.I):
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
        # e.g. "JPMorgan upgraded Tesla to Neutral from Underweight"
        m = re.search(r"\bupgraded\b\s+(?:\S+\s+){0,4}?to\s+([A-Za-z][A-Za-z \-/]+?)\s+from\s+[A-Za-z]", text, re.I)
        if m:
            rating_action = "升評"
            rating = m.group(1).strip()
    if not rating_action:
        for p in downgrade_words:
            m = re.search(re.escape(p) + r"\s+([A-Za-z][A-Za-z \-/]+?)(?:[,.]| from | with | and |$)", text, re.I)
            if m:
                rating_action = "降評"
                rating = m.group(1).strip()
                break
    if not rating_action:
        m = re.search(r"\bdowngraded\b\s+(?:\S+\s+){0,4}?to\s+([A-Za-z][A-Za-z \-/]+?)\s+from\s+[A-Za-z]", text, re.I)
        if m:
            rating_action = "降評"
            rating = m.group(1).strip()

    # Detect target price patterns
    old_target = ""
    new_target = ""

    patterns = [
        r"(?:price target|target price|target)\s+(?:was\s+)?(?:raised|increased|boosted|lifted|hiked)\s+(?:to\s+)?\$?([\d,.]+)\s+(?:from|vs\.?|versus)\s+\$?([\d,.]+)",
        r"(?:price target|target price|target)\s+(?:was\s+)?(?:cut|lowered|reduced)\s+(?:to\s+)?\$?([\d,.]+)\s+(?:from|vs\.?|versus)\s+\$?([\d,.]+)",
        r"(?:raised|increased|boosted|lifted|hiked)\s+(?:its\s+)?(?:price target|target price|target)\s+(?:to\s+)?\$?([\d,.]+)\s+(?:from|vs\.?|versus)\s+\$?([\d,.]+)",
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

    # Reverse wording used by many Investing.com headlines:
    # "$810 price target" / "$810.00 target price"
    if not new_target:
        m = re.search(r"\$([\d,.]+)\s+(?:price target|target price)", text, re.I)
        if m:
            new_target = m.group(1)

    # Additional target-price wordings used by finance sites.
    if not new_target:
        # "$860 price target" / "$860.00 target price"
        m = re.search(r"\$([\d,.]+)\s+(?:price target|target price)", text, re.I)
        if m:
            new_target = m.group(1)

    if not new_target:
        # "target to $860" / "price target at $860"
        m = re.search(
            r"(?:price target|target price|target)\s+(?:is\s+)?(?:now\s+)?(?:to|at)\s+\$?([\d,.]+)",
            text, re.I
        )
        if m:
            new_target = m.group(1)

    # Determine target direction
    target_action = ""
    if any(k in low for k in ["price target raised", "raises price target", "raised its price target",
                               "target raised", "boosts price target", "increases price target",
                               "lifted its price target", "hiked its price target", "price target increased"]):
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

    # Reiterated / maintained rating without upgrade/downgrade.
    if not rating_action:
        m = re.search(
            r"\b(?:reiterat(?:es|ed|ing)?|maintain(?:s|ed|ing)?)\b(?:\s+\w+){0,5}?\s+(?:a\s+)?(Buy|Outperform|Overweight|Neutral|Equal[- ]Weight|Hold|Underperform|Underweight|Sell)\b",
            text, re.I
        )
        if m:
            rating_action = "重申"
            rating = m.group(1).strip()

    # Reiterated / maintained rating without upgrade or downgrade.
    if not rating_action:
        m = re.search(
            r"\b(?:reiterat(?:e|es|ed|ing)|maintain(?:s|ed|ing)?)\b"
            r"(?:\s+\w+){0,6}?\s+(?:a\s+)?"
            r"(Buy|Outperform|Overweight|Neutral|Equal[- ]Weight|Hold|Underperform|Underweight|Sell)\b",
            text, re.I
        )
        if m:
            rating_action = "重申"
            rating = m.group(1).strip()

    # Build concise investor-style headline
    if broker and ticker and new_target:
        newp = _fmt_target(new_target)
        oldp = _fmt_target(old_target) if old_target else ""

        if rating_action and rating:
            rating = re.sub(r"\s+", " ", rating).strip()

            if rating_action == "重申":
                # Put the actual target-price change first because that is what
                # the investor needs to know at a glance.
                verb = target_action or "調整"
                title = f"{broker} {verb} {ticker} 目標價至 {newp}"
                if oldp:
                    title += f"（原 {oldp}）"
                title += f"，維持 {rating}"
                return title

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
        if rating_action == "重申":
            return f"{broker} 重申 {ticker}" + (f" {rating}" if rating else "")
        return f"{broker} {rating_action} {ticker}" + (f" 至 {rating}" if rating else "")

    return x.get("title") or raw_title

def target_relevance(text, display_ticker, company_name):
    """
    Strictly map a news headline to the intended watchlist company.

    Rules:
      1) Exact ticker mention always wins.
      2) Exact company name is accepted.
      3) Otherwise require at least TWO meaningful company-name tokens.
         This prevents:
           United Microelectronics (UMC)
         from falsely matching:
           United Therapeutics
    """
    raw = text or ""
    low = raw.lower()
    ticker = (display_ticker or "").strip().lower()
    company = (company_name or "").strip().lower()

    # Exact ticker is the safest signal.
    if len(ticker) >= 2 and re.search(
        rf"(?<![a-z0-9]){re.escape(ticker)}(?![a-z0-9])",
        low
    ):
        return True

    # Exact company phrase.
    if company and company in low:
        return True

    stop = {
        "holdings","holding","technologies","technology","systems","system",
        "semiconductor","semiconductors","solutions","solution","devices",
        "materials","electronics","international","corporation","company",
        "inc","limited","ltd","group","plc","common","stock",
        # Generic geographic/legal words are too weak alone.
        "united","american","global","advanced","general"
    }

    roots = []
    for token in re.findall(r"[a-z0-9]+", company):
        if len(token) >= 4 and token not in stop:
            roots.append(token)

    matched = [r for r in roots if re.search(rf"(?<![a-z0-9]){re.escape(r)}(?![a-z0-9])", low)]

    # Require at least 2 meaningful roots when the exact ticker/company isn't present.
    if len(matched) >= 2:
        return True

    # One very distinctive long token can be enough (e.g. "microelectronics"),
    # but not generic words.
    if len(matched) == 1 and len(matched[0]) >= 12:
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

def is_broker_action_text(text):
    blob = (text or "").lower()

    if any(k in blob for k in [
        "price target", "target price",
        "initiates coverage", "initiated coverage",
        "initiates with", "initiated with",
        "initiates at", "initiated at",
        "rating raised", "rating cut",
        "raises target", "raised target",
        "cuts target", "cut target",
        "lowers target", "lowered target",
        "boosts target", "boosted target",
        "hikes target", "hiked target",
    ]):
        return True

    if re.search(r"\bupgrad(?:e|ed|es|ing)\b", blob):
        return True
    if re.search(r"\bdowngrad(?:e|ed|es|ing)\b", blob):
        return True

    if "reiterat" in blob and any(r in blob for r in [
        " buy", " outperform", " overweight", " neutral",
        " equal weight", " equal-weight", " hold",
        " underperform", " underweight", " sell"
    ]):
        return True

    if ("maintain" in blob or "maintained" in blob) and any(r in blob for r in [
        " buy", " outperform", " overweight", " neutral",
        " equal weight", " equal-weight", " hold",
        " underperform", " underweight", " sell", "price target", "target price"
    ]):
        return True

    return False



def _clean_html_text(s):
    s = html_lib.unescape(s or "")
    s = re.sub(r"<script\b[^>]*>.*?</script>", " ", s, flags=re.I|re.S)
    s = re.sub(r"<style\b[^>]*>.*?</style>", " ", s, flags=re.I|re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _extract_meta_description(page_html):
    """
    Extract a short publisher-supplied description from HTML.
    We intentionally do NOT copy full article bodies.
    """
    if not page_html:
        return ""

    candidates = []

    meta_patterns = [
        r'<meta[^>]+(?:property|name)\s*=\s*["\']og:description["\'][^>]+content\s*=\s*["\']([^"\']+)["\']',
        r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]+(?:property|name)\s*=\s*["\']og:description["\']',
        r'<meta[^>]+(?:property|name)\s*=\s*["\']twitter:description["\'][^>]+content\s*=\s*["\']([^"\']+)["\']',
        r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]+(?:property|name)\s*=\s*["\']twitter:description["\']',
        r'<meta[^>]+name\s*=\s*["\']description["\'][^>]+content\s*=\s*["\']([^"\']+)["\']',
        r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]+name\s*=\s*["\']description["\']',
    ]
    for pat in meta_patterns:
        m = re.search(pat, page_html, flags=re.I|re.S)
        if m:
            candidates.append(_clean_html_text(m.group(1)))

    # JSON-LD "description" is another common source.
    for m in re.finditer(r'"description"\s*:\s*"((?:\\.|[^"\\])*)"', page_html, flags=re.I|re.S):
        try:
            val = bytes(m.group(1), "utf-8").decode("unicode_escape")
        except Exception:
            val = m.group(1)
        candidates.append(_clean_html_text(val))

    bad = (
        "google news", "latest news", "breaking news", "read the latest",
        "your source for", "sign in", "enable javascript"
    )
    for c in candidates:
        if len(c) < 70:
            continue
        low = c.lower()
        if any(b in low for b in bad):
            continue
        return c[:1200]

    return ""


def _decode_json_string(s):
    if not s:
        return ""
    try:
        return json.loads('"' + s.replace('"', '\\"') + '"')
    except Exception:
        try:
            return bytes(s, "utf-8").decode("unicode_escape")
        except Exception:
            return s

def _extract_public_article_text(page_html, final_url=""):
    """
    Extract a concise public article excerpt from publisher HTML.
    This is used only to create our own short Chinese summary; we do not
    reproduce the full article.

    Priority:
      1) JSON-LD articleBody
      2) Investing.com article-content blocks / paragraphs
      3) Generic article paragraphs
    """
    if not page_html:
        return ""

    candidates = []

    # JSON-LD / embedded schema is the cleanest source when available.
    for m in re.finditer(r'"articleBody"\s*:\s*"((?:\\.|[^"\\])*)"', page_html, re.I | re.S):
        raw = m.group(1)
        try:
            val = json.loads('"' + raw + '"')
        except Exception:
            val = raw.replace(r'\"', '"').replace(r'\n', ' ')
        val = _clean_html_text(val)
        if len(val) >= 140:
            candidates.append(val)

    # Some publishers use "text" rather than articleBody in NewsArticle JSON.
    if not candidates:
        for m in re.finditer(r'"text"\s*:\s*"((?:\\.|[^"\\])*)"', page_html, re.I | re.S):
            raw = m.group(1)
            try:
                val = json.loads('"' + raw + '"')
            except Exception:
                val = raw.replace(r'\"', '"').replace(r'\n', ' ')
            val = _clean_html_text(val)
            if len(val) >= 180:
                candidates.append(val)

    # Prefer paragraphs from likely article containers.
    container_chunks = []
    for pat in [
        r'<(?:div|section|article)[^>]+(?:data-test|data-testid)=["\'][^"\']*(?:article|content)[^"\']*["\'][^>]*>(.*?)</(?:div|section|article)>',
        r'<(?:div|section|article)[^>]+class=["\'][^"\']*(?:article|wysiwyg|content)[^"\']*["\'][^>]*>(.*?)</(?:div|section|article)>',
    ]:
        container_chunks.extend(re.findall(pat, page_html, re.I | re.S))

    search_spaces = container_chunks if container_chunks else [page_html]

    paras = []
    for chunk in search_spaces[:8]:
        for p in re.findall(r'<p\b[^>]*>(.*?)</p>', chunk, re.I | re.S):
            t = _clean_html_text(p)
            low = t.lower()

            if len(t) < 45:
                continue
            if any(bad in low for bad in [
                "sign up", "subscribe", "advertisement", "cookie",
                "download the app", "investingpro", "terms and conditions",
                "for more information see our", "unlock", "read more"
            ]):
                continue

            paras.append(t)

    # De-duplicate while preserving order.
    unique = []
    seen = set()
    for p in paras:
        key = re.sub(r"\W+", "", p.lower())[:180]
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(p)

    if unique:
        joined = " ".join(unique[:10]).strip()
        if len(joined) >= 140:
            candidates.append(joined)

    if not candidates:
        return ""

    # Prefer the richest candidate, but keep it bounded.
    candidates.sort(key=len, reverse=True)
    best = candidates[0]

    # Enough for a high-quality summary, but nowhere near a full article copy.
    return best[:3200].strip()


BAD_ARTICLE_HOSTS = {
    "www.w3.org", "w3.org", "schema.org", "www.schema.org",
    "fonts.googleapis.com", "fonts.gstatic.com", "www.google-analytics.com",
    "google-analytics.com", "googletagmanager.com", "www.googletagmanager.com",
}
BAD_ARTICLE_PATH_BITS = (
    "/2000/svg", "/svg", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".css", ".js", ".woff", ".woff2", ".ico", "/favicon"
)

def _is_valid_article_url(url):
    try:
        p = urllib.parse.urlparse(url or "")
        host = (p.hostname or "").lower()
        path = (p.path or "").lower()
        if p.scheme not in ("http", "https") or not host:
            return False
        if host in BAD_ARTICLE_HOSTS:
            return False
        if any(bit in path for bit in BAD_ARTICLE_PATH_BITS):
            return False
        return True
    except Exception:
        return False

def _is_polluted_text(text):
    low = (text or "").lower()
    bad = [
        "http://www.w3.org/2000/svg",
        "https://www.w3.org/2000/svg",
        "svg is an xml namespace",
        "svg namespace is mutable",
        "scalable vector graphics (svg)",
        "namespaces in xml specification",
    ]
    return any(x in low for x in bad)

def _extract_external_urls(page_html):
    """
    Find publisher URLs embedded in Google News HTML, including URL-encoded forms.
    """
    if not page_html:
        return []

    out = []

    # Plain URLs anywhere in the page, not only href attributes.
    for m in re.findall(r'https?://[^"\'<>\s\\]+', page_html, re.I):
        out.append(html_lib.unescape(m))

    # Percent-encoded publisher URLs.
    for m in re.findall(r'https?%3A%2F%2F[^"\'<>\s&]+', page_html, re.I):
        try:
            out.append(urllib.parse.unquote(html_lib.unescape(m)))
        except Exception:
            pass

    # JS-escaped URLs such as https:\/\/www.investing.com\/...
    for m in re.findall(r'https?:\\?/\\?/[^"\'<>\s]+', page_html, re.I):
        out.append(m.replace(r'\/', '/'))

    cleaned = []
    seen = set()
    for u in out:
        u = u.replace("\\u0026", "&").replace("\\/", "/")
        u = html_lib.unescape(u)
        if not u.startswith(("http://", "https://")):
            continue
        if not _is_valid_article_url(u):
            continue
        if u in seen:
            continue
        seen.add(u)
        cleaned.append(u)

    return cleaned

def _fetch_article_context(url):
    """
    Resolve Google News -> publisher when possible, then extract:
      - a concise public article excerpt for supported/open publisher pages
      - otherwise metadata description

    Investing.com is treated as a high-priority enrichment source because
    analyst-rating articles often expose the actual PT, rating, and rationale
    in the public article body while RSS contains only the headline.
    """
    if not url:
        return "", url

    def fetch(u):
        req = urllib.request.Request(
            u,
            headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            final = r.geturl()
            raw = r.read(1_200_000)
            charset = "utf-8"
            try:
                charset = r.headers.get_content_charset() or "utf-8"
            except Exception:
                pass
        return raw.decode(charset, "ignore"), final

    try:
        page, final = fetch(url)
    except Exception:
        return "", url

    final_low = (final or "").lower()

    # If already on the publisher, prefer article body before metadata.
    if "investing.com/" in final_low:
        body = _extract_public_article_text(page, final)
        if body and not _is_polluted_text(body) and _is_valid_article_url(final):
            return body, final

        desc = _extract_meta_description(page)
        if desc:
            return desc, final

    # For non-Google publisher pages, use body where available, then metadata.
    if "news.google." not in final_low:
        body = _extract_public_article_text(page, final)
        if body and not _is_polluted_text(body) and _is_valid_article_url(final):
            return body, final

        desc = _extract_meta_description(page)
        if desc:
            return desc, final

    # Google News: resolve embedded publisher URL BEFORE accepting a generic
    # Google meta description.
    if "news.google." in final_low:
        candidates = _extract_external_urls(page)

        # Prefer Investing.com / known finance publishers first.
        def rank_url(u):
            low = u.lower()
            if "investing.com/" in low:
                return 0
            if any(d in low for d in [
                "finance.yahoo.com/", "reuters.com/", "marketwatch.com/",
                "benzinga.com/", "barrons.com/", "seekingalpha.com/"
            ]):
                return 1
            return 2

        for candidate in sorted(candidates, key=rank_url)[:120]:
            if not _is_valid_article_url(candidate):
                continue
            low = candidate.lower()
            if any(d in low for d in [
                "google.com", "googleusercontent.com", "gstatic.com",
                "youtube.com", "policies.google", "accounts.google"
            ]):
                continue

            try:
                p2, f2 = fetch(candidate)
            except Exception:
                continue

            if "investing.com/" in (f2 or "").lower():
                body = _extract_public_article_text(p2, f2)
                if body and not _is_polluted_text(body) and _is_valid_article_url(f2):
                    return body, f2

            # Generic publisher body is also useful for analyst rationale.
            body2 = _extract_public_article_text(p2, f2)
            if body2 and not _is_polluted_text(body2) and _is_valid_article_url(f2):
                return body2, f2

            d2 = _extract_meta_description(p2)
            if d2 and not _is_polluted_text(d2) and _is_valid_article_url(f2):
                return d2, f2

        # Last resort: Google page metadata.
        desc = _extract_meta_description(page)
        if desc:
            return desc, final

    return "", final


def _title_similarity(a, b):
    a = re.sub(r"[^a-z0-9]+", " ", (a or "").lower()).strip()
    b = re.sub(r"[^a-z0-9]+", " ", (b or "").lower()).strip()
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    sa, sb = set(a.split()), set(b.split())
    jac = len(sa & sb) / max(1, len(sa | sb))
    return max(seq, jac)


def _yahoo_search_context(title, ticker=""):
    """
    Search Yahoo Finance news by headline. This is a fallback for Google News
    RSS stories that only contain a redirect URL and no summary.
    """
    title = (title or "").strip()
    if not title:
        return "", ""

    try:
        search = yf.Search(
            title,
            max_results=5,
            news_count=8,
            lists_count=0,
            include_cb=False,
            include_nav_links=False,
            include_research=False,
            enable_fuzzy_query=False,
        )
        rows = getattr(search, "news", None) or []
    except Exception:
        rows = []

    best = None
    best_score = 0.0
    wanted_ticker = (ticker or "").upper().strip()

    for row in rows:
        if not isinstance(row, dict):
            continue

        rt = (row.get("title") or "").strip()
        if not rt:
            continue

        score = _title_similarity(title, rt)

        related = row.get("relatedTickers") or row.get("related_tickers") or []
        related = [str(x).upper() for x in related if x]
        if wanted_ticker and wanted_ticker in related:
            score += 0.15

        if score > best_score:
            best_score = score
            best = row

    if not best or best_score < 0.48:
        return "", ""

    direct = best.get("link") or best.get("url") or best.get("clickThroughUrl") or ""
    if isinstance(direct, dict):
        direct = direct.get("url") or ""

    desc = _clean_html_text(best.get("summary") or best.get("description") or "")
    if desc:
        return desc[:1200], direct

    if direct:
        fetched, resolved = _fetch_article_context(direct)
        if fetched:
            return fetched[:1200], resolved or direct

    return "", direct


def _has_exact_analyst_target(text):
    """
    Whether the available text contains an actual numeric analyst target,
    not just a percentage change such as 'target raised 26%'.
    """
    t = text or ""
    pats = [
        r"(?:price target|target price|target)[^$]{0,35}\$[\d,.]+",
        r"\$[\d,.]+\s+(?:price target|target price)",
        r"from\s+\$[\d,.]+\s+(?:to|→)\s+\$[\d,.]+",
    ]
    return any(re.search(p, t, re.I) for p in pats)

def _analyst_detail_richness(text):
    """
    Higher means the snippet contains more useful analyst facts.
    """
    t = text or ""
    score = 0
    if _has_exact_analyst_target(t):
        score += 5
    if re.search(r"from\s+\$[\d,.]+\s+(?:to|→)\s+\$[\d,.]+", t, re.I):
        score += 4
    if re.search(r"\b(Buy|Outperform|Overweight|Neutral|Equal[- ]Weight|Hold|Underperform|Underweight|Sell)\b", t, re.I):
        score += 2
    score += min(2, len(t) // 180)
    return score

def _broker_from_text(text):
    for name in BROKER_NAMES:
        pat = r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])"
        if re.search(pat, text or "", re.I):
            return name
    return ""


def _broker_names_compatible(source_text, candidate_text, title_similarity=0.0):
    """
    Avoid cross-wiring one broker's article to another broker's summary.

    Example of a BAD match this blocks:
      JPMorgan META $920 headline  <-  Cantor META +26% summary
    """
    a = _broker_from_text(source_text or "")
    b = _broker_from_text(candidate_text or "")

    if a and b:
        return a.lower() == b.lower()

    # If the source explicitly names a broker but the candidate doesn't,
    # require a very high headline similarity before accepting it.
    if a and not b:
        return title_similarity >= 0.82

    # If candidate names a broker while source doesn't, be conservative.
    if b and not a:
        return title_similarity >= 0.82

    return title_similarity >= 0.68


def enrich_analyst_details(items):
    """
    Fill missing analyst summaries in two stages:
      1) match the Google broker-search headline to Yahoo/yfinance ticker-feed content
      2) if still blank, fetch only the article metadata description

    This makes the detail modal useful without copying full articles.
    """
    ticker_feed = {}
    for x in items:
        if x.get("sourceType") != "ticker_feed":
            continue
        if not (x.get("originalSummary") or "").strip():
            continue
        ticker_feed.setdefault((x.get("ticker") or "").upper(), []).append(x)

    targets = []
    for x in items:
        if not x.get("analystPriority"):
            continue
        existing_blob = " ".join([
            x.get("originalTitle") or "",
            x.get("originalSummary") or "",
        ]).strip()

        # If the current item already contains an exact target price, no need to
        # spend another request enriching it.
        if (x.get("originalSummary") or "").strip() and _has_exact_analyst_target(existing_blob):
            continue

        # First: fuzzy-match same-ticker Yahoo feed.
        best = None
        best_score = 0.0
        source_blob = " ".join([
            x.get("originalTitle") or "",
            x.get("originalSummary") or "",
        ])
        for y in ticker_feed.get((x.get("ticker") or "").upper(), []):
            s = _title_similarity(x.get("originalTitle"), y.get("originalTitle"))
            candidate_blob = " ".join([
                y.get("originalTitle") or "",
                y.get("originalSummary") or "",
            ])
            if not _broker_names_compatible(source_blob, candidate_blob, s):
                continue
            if s > best_score:
                best_score, best = s, y

        if best is not None and best_score >= 0.52:
            candidate = (best.get("originalSummary") or "").strip()
            current = (x.get("originalSummary") or "").strip()

            if _analyst_detail_richness(candidate) > _analyst_detail_richness(current):
                x["originalSummary"] = candidate[:2600]
                x["summary"] = (best.get("summary") or candidate)[:500]
                if best.get("url"):
                    x["url"] = best["url"]
                x["detailSource"] = "ticker_feed_match"

            if _has_exact_analyst_target(
                " ".join([x.get("originalTitle") or "", x.get("originalSummary") or ""])
            ):
                continue

        targets.append(x)

    # Keep every 5-minute run bounded.
    targets.sort(key=lambda z: z.get("ts", ""), reverse=True)
    targets = targets[:40]

    def one(x):
        title = x.get("originalTitle") or x.get("title") or ""
        ticker = x.get("ticker") or ""
        original_blob = " ".join([
            x.get("originalTitle") or "",
            x.get("originalSummary") or "",
        ])
        broker = _broker_from_text(original_blob)

        candidates = []

        desc1, url1 = _yahoo_search_context(title, ticker)
        if desc1:
            sim1 = _title_similarity(title, desc1)
            if _broker_names_compatible(original_blob, desc1, sim1):
                candidates.append((desc1, url1))

        # Second query is intentionally more factual than the media headline.
        # If the original story names a broker, the returned text must name the same broker.
        if broker and ticker:
            q2 = f"{ticker} {broker} price target"
            desc2, url2 = _yahoo_search_context(q2, ticker)
            if desc2:
                b2 = _broker_from_text(desc2)
                if b2 and b2.lower() == broker.lower():
                    candidates.append((desc2, url2))

        # Direct article metadata is safe to use because it comes from the story URL itself.
        desc3, url3 = _fetch_article_context(x.get("url") or "")
        if desc3:
            candidates.append((desc3, url3))

        if not candidates:
            return x, "", ""

        candidates.sort(
            key=lambda z: _analyst_detail_richness(z[0]),
            reverse=True
        )
        return x, candidates[0][0], candidates[0][1]

    if targets:
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = [ex.submit(one, x) for x in targets]
            for fut in as_completed(futs):
                try:
                    x, desc, resolved = fut.result()
                except Exception:
                    continue
                if desc:
                    current = (x.get("originalSummary") or "").strip()
                    if _analyst_detail_richness(desc) > _analyst_detail_richness(current):
                        x["originalSummary"] = desc[:2600]
                        x["summary"] = zh(desc[:1800])[:1200] if desc else ""
                        x["detailSource"] = "yahoo_or_article_metadata"
                if resolved and "news.google." not in resolved:
                    x["url"] = resolved

    return items

def broker_news_search(display_ticker, company_name, symbol, group):
    """
    Dedicated analyst/broker scan for every watchlist stock.
    These stories get first priority regardless of the stock's price move.
    """
    q = (
        f'("{company_name}" OR {display_ticker}) '
        '("price target" OR "target price" OR upgraded OR downgraded OR '
        '"initiates coverage" OR "initiates with" OR reiterates OR maintained OR maintains OR '
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
    for item in root.findall(".//item")[:30]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = parse_rss_date(item.findtext("pubDate"))
        if pub and now - pub > timedelta(hours=cfg.get("news_lookback_hours", 48)):
            continue

        clean_title = re.sub(r"\s+-\s+[^-]{2,80}$", "", title).strip() or title
        blob = clean_title.lower()

        if not target_relevance(clean_title, display_ticker, company_name):
            continue
        if not is_broker_action_text(blob):
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

        analyst_priority = is_broker_action_text(blob)

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


def moomoo_news_search(display_ticker, company_name, symbol, group, move_pct=None):
    """
    Search recent public moomoo/Futu web articles via Google News RSS.
    This does NOT depend on private app APIs or login-only content.
    It is used as an extra source for the biggest movers each cycle.
    """
    q = (
        f'("{display_ticker}" OR "{company_name}") '
        f'(site:moomoo.com OR site:futunn.com) when:2d'
    )
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": q,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })

    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return []

    out = []
    now = datetime.now(timezone.utc)

    for item in root.findall(".//item")[:10]:
        raw_title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = parse_rss_date(item.findtext("pubDate"))

        if pub and now - pub > timedelta(hours=52):
            continue

        title = re.sub(r"\s+-\s+[^-]{2,80}$", "", raw_title).strip() or raw_title

        if not target_relevance(title, display_ticker, company_name):
            continue

        blob = title.lower()
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
            "summary": "",
            "originalTitle": title,
            "originalSummary": "",
            "url": link,
            "ts": pub.isoformat() if pub else "",
            "sourceType": "moomoo_public",
            "analystPriority": is_broker_action_text(blob),
            "movePct": move_pct,
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




MAJOR_BROKER_QUERY_GROUPS = [
    ['"BofA Securities"', '"Bank of America"', 'JPMorgan', '"JP Morgan"'],
    ['"Morgan Stanley"', '"Goldman Sachs"', 'Citi', 'Citigroup'],
    ['UBS', 'Jefferies', '"Wells Fargo"', 'Barclays'],
    ['Evercore', '"Evercore ISI"', 'Bernstein', 'Mizuho'],
    ['Cantor', '"Cantor Fitzgerald"', 'Wedbush', '"Tigress Financial"'],
    ['KeyBanc', '"Piper Sandler"', 'Needham', 'Oppenheimer'],
]

def _broker_priority_scan_one(brokers, targets):
    """
    High-recall scan dedicated to analyst actions.
    One RSS request covers several major brokers and the full watchlist.
    """
    broker_terms = " OR ".join(brokers)
    action_terms = (
        '"price target" OR "target price" OR upgraded OR downgraded OR '
        '"initiates coverage" OR "initiated coverage" OR reiterates OR reiterated OR '
        'maintains OR maintained OR "raises target" OR "raised target" OR '
        '"cuts target" OR "cut target" OR "lowers target" OR "lowered target" OR '
        '"boosts target" OR "boosted target"'
    )
    q = f'({broker_terms}) ({action_terms}) when:2d'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": q,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    })

    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    out = []
    seen = set()

    for item in root.findall(".//item")[:100]:
        raw_title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = parse_rss_date(item.findtext("pubDate"))

        if pub and now - pub > timedelta(hours=52):
            continue

        clean_title = re.sub(r"\s+-\s+[^-]{2,80}$", "", raw_title).strip() or raw_title
        blob = clean_title.lower()

        if not is_broker_action_text(blob):
            continue

        best = None
        for display_ticker, company_name, symbol, group in targets:
            if not target_relevance(clean_title, display_ticker, company_name):
                continue

            explicit_ticker = bool(re.search(
                rf"(?<![A-Za-z0-9]){re.escape(display_ticker)}(?![A-Za-z0-9])",
                clean_title, re.I
            ))
            exact_company = (company_name or "").lower() in clean_title.lower()
            score = (3 if explicit_ticker else 0) + (2 if exact_company else 0)

            if best is None or score > best[0]:
                best = (score, display_ticker, company_name, symbol, group)

        if best is None:
            continue

        _, display_ticker, company_name, symbol, group = best
        k = display_ticker + "|" + re.sub(r"\W+", "", clean_title.lower())
        if k in seen:
            continue
        seen.add(k)

        tag = "題材"
        if any(k in blob for k in BROKER_NEG):
            tag = "利空"
        elif any(k in blob for k in BROKER_POS) or any(k in blob for k in [
            "reiterates buy", "reiterated buy", "maintains buy", "maintained buy",
            "reiterates outperform", "reiterates overweight",
        ]):
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
            "sourceType": "broker_priority_scan",
            "analystPriority": True,
        })

    return out

def major_broker_priority_scans(targets):
    """
    Run a small number of broker-focused searches in parallel.
    This specifically protects against missing high-value PT/rating news such as
    BofA / JPMorgan / Morgan Stanley notes that may rank low in a ticker RSS query.
    """
    out = []
    with ThreadPoolExecutor(max_workers=min(6, len(MAJOR_BROKER_QUERY_GROUPS))) as ex:
        futs = [ex.submit(_broker_priority_scan_one, g, targets) for g in MAJOR_BROKER_QUERY_GROUPS]
        for fut in as_completed(futs):
            try:
                out.extend(fut.result())
            except Exception:
                pass
    return out


def major_broker_market_scan(targets):
    """
    One extra Google News RSS request per cycle for major broker actions.
    This is deliberately global (not one request per ticker), so it improves
    recall without doubling request volume across the entire watchlist.

    Example event this is meant to catch:
      BofA Securities reiterates Buy on Meta stock, $810 price target
    """
    broker_terms = (
        '"BofA Securities" OR "Bank of America" OR JPMorgan OR "JP Morgan" OR Citi OR Citigroup OR '
        '"Morgan Stanley" OR "Goldman Sachs" OR UBS OR Jefferies OR "Wells Fargo" OR '
        'Barclays OR Bernstein OR Mizuho OR Evercore OR "Piper Sandler" OR KeyBanc OR '
        'Wedbush OR "Tigress Financial"'
    )
    action_terms = (
        '"price target" OR "target price" OR upgraded OR downgraded OR '
        '"initiates coverage" OR "initiated coverage" OR reiterates OR reiterated OR '
        'maintains OR maintained OR "raises target" OR "cuts target" OR '
        '"lowers target" OR "boosts target"'
    )

    q = f'({broker_terms}) ({action_terms}) when:2d'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": q,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    })

    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    out = []
    seen = set()

    # Google News can return a large result set; inspect more than the normal
    # per-ticker scan, then map each headline back onto the watchlist.
    for item in root.findall(".//item")[:100]:
        raw_title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = parse_rss_date(item.findtext("pubDate"))

        if pub and now - pub > timedelta(hours=52):
            continue

        clean_title = re.sub(r"\s+-\s+[^-]{2,80}$", "", raw_title).strip() or raw_title
        blob = clean_title.lower()

        if not is_broker_action_text(blob):
            continue

        # Map one article to the most specific watchlist target.
        best = None
        for display_ticker, company_name, symbol, group in targets:
            if target_relevance(clean_title, display_ticker, company_name):
                # Prefer explicit ticker mention when several companies might match.
                explicit = bool(re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(display_ticker)}(?![A-Za-z0-9])",
                    clean_title,
                    re.I
                ))
                score = 2 if explicit else 1
                if best is None or score > best[0]:
                    best = (score, display_ticker, company_name, symbol, group)

        if best is None:
            continue

        _, display_ticker, company_name, symbol, group = best
        dedupe = display_ticker + "|" + re.sub(r"\W+", "", clean_title.lower())
        if dedupe in seen:
            continue
        seen.add(dedupe)

        tag = "題材"
        if any(k in blob for k in BROKER_NEG):
            tag = "利空"
        elif any(k in blob for k in BROKER_POS) or any(k in blob for k in [
            "reiterates buy", "reiterated buy", "maintains buy", "maintained buy",
            "reiterates outperform", "reiterates overweight"
        ]):
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
            "sourceType": "major_broker_global",
            "analystPriority": True
        })

    return out



def tipranks_broker_search(display_ticker, company_name, symbol, group):
    """Extra high-priority analyst-rating discovery from TipRanks-indexed pages."""
    q = (
        f'("{display_ticker}" OR "{company_name}") '
        f'(site:tipranks.com) '
        f'("price target" OR upgrade OR upgraded OR downgrade OR downgraded OR '
        f'reiterate OR reiterated OR maintain OR maintained OR initiate OR initiated) when:3d'
    )
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": q, "hl":"en-US", "gl":"US", "ceid":"US:en"
    })
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req,timeout=10) as r:
            root=ET.fromstring(r.read())
    except Exception:
        return []
    now=datetime.now(timezone.utc); out=[]
    for item in root.findall('.//item')[:20]:
        raw=(item.findtext('title') or '').strip()
        link=(item.findtext('link') or '').strip()
        pub=parse_rss_date(item.findtext('pubDate'))
        if pub and now-pub>timedelta(hours=76):
            continue
        title=re.sub(r'\s+-\s+[^-]{2,80}$','',raw).strip() or raw
        if not target_relevance(title,display_ticker,company_name):
            continue
        blob=title.lower()
        if not is_broker_action_text(blob):
            continue
        tag='題材'
        if any(k in blob for k in BROKER_NEG): tag='利空'
        elif any(k in blob for k in BROKER_POS) or any(k in blob for k in ['maintains buy','reiterates buy','maintains overweight','reiterates overweight']): tag='利多'
        out.append({
            'ticker':display_ticker,'group':group,'tag':tag,'title':title,'summary':'',
            'originalTitle':title,'originalSummary':'','url':link,
            'ts':pub.isoformat() if pub else '', 'sourceType':'tipranks_broker',
            'analystPriority':True
        })
    return out

SOCIAL_THEME_WORDS = [
    'ai','人工智慧','agent','muse','semiconductor','半導體','chip','晶片','data center','datacenter','資料中心',
    'power','electricity','電力','nuclear','核能','memory','記憶體','hbm','optical','光通訊','cpo','npo',
    'cloud','雲端','robot','機器人','robotaxi','太空','space','defense','國防','drone','無人機',
    'earnings','財報','guidance','財測','revenue','營收','margin','毛利','order','訂單','capacity','產能',
    'price target','目標價','upgrade','downgrade','升評','降評','demand','需求','supply','供應'
]

def _plain_text_from_markdown(s):
    s=s or ''
    s=re.sub(r'!\[[^\]]*\]\([^)]*\)',' ',s)
    s=re.sub(r'\[([^\]]+)\]\([^)]*\)',r'\1',s)
    s=re.sub(r'^[#>*\-]+\s*','',s,flags=re.M)
    s=re.sub(r'\s+',' ',s).strip()
    return s

def _fetch_reader(url, timeout=12):
    candidates=[url]
    if url.startswith('https://'):
        candidates.append('https://r.jina.ai/http://'+url[len('https://'):])
    elif url.startswith('http://'):
        candidates.append('https://r.jina.ai/http://'+url[len('http://'):])
    for u in candidates:
        try:
            req=urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0','Accept':'text/html,text/plain,*/*'})
            with urllib.request.urlopen(req,timeout=timeout) as r:
                raw=r.read(900000).decode('utf-8','ignore')
            if len(raw)>120:
                return raw,u
        except Exception:
            pass
    return '',url

def _social_post_time(text):
    now=datetime.now(timezone.utc)
    pats=[
        (r'(20\d{2})[/-](\d{1,2})[/-](\d{1,2})',lambda m: datetime(int(m.group(1)),int(m.group(2)),int(m.group(3)),tzinfo=timezone.utc)),
        (r'(Sep|Oct|Nov|Dec|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug)\s+(\d{1,2}),\s*(20\d{2})',lambda m: datetime.strptime(' '.join(m.groups()),'%b %d %Y').replace(tzinfo=timezone.utc)),
    ]
    for p,fn in pats:
        m=re.search(p,text or '',re.I)
        if m:
            try:return fn(m)
            except Exception:pass
    return now

def _social_relevance_and_target(text, targets):
    raw=text or ''; low=raw.lower()
    best=None
    for ticker,name,symbol,group in targets:
        explicit=bool(re.search(rf'(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])',raw,re.I))
        company=(name or '').lower() in low and len(name or '')>=3
        if explicit or company:
            score=3 if explicit else 2
            if best is None or score>best[0]: best=(score,ticker,group)
    theme_hits=sum(1 for k in SOCIAL_THEME_WORDS if k.lower() in low)
    if best:
        return True,best[1],best[2],theme_hits+best[0]
    if theme_hits>=2:
        return True,'MARKET','專欄／市場觀點',theme_hits
    return False,'','',theme_hits

def _social_title(text,label):
    t=_plain_text_from_markdown(text)
    # remove common UI boilerplate
    t=re.sub(r'^(Title:\s*[^|]{0,80}\|\s*)','',t,flags=re.I)
    for sep in ['  All reactions:',' All reactions:',' Like Comment',' Most relevant replies']:
        if sep in t:t=t.split(sep,1)[0]
    # Prefer first sentence / line-like chunk
    parts=re.split(r'(?<=[。！？!?])\s+',t)
    cand=(parts[0] if parts else t).strip()
    return cand[:110] if cand else label


def _search_public_social_links(query, domain='facebook.com'):
    """Fallback discovery for public social posts when a stable page slug is unavailable."""
    urls=[]
    q=f'site:{domain} "{query}"'
    engines=[
        'https://www.google.com/search?num=20&'+urllib.parse.urlencode({'q':q}),
        'https://www.bing.com/search?count=20&'+urllib.parse.urlencode({'q':q}),
    ]
    for surl in engines:
        try:
            req=urllib.request.Request(surl,headers={'User-Agent':'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1'})
            with urllib.request.urlopen(req,timeout=10) as r: raw=r.read(700000).decode('utf-8','ignore')
        except Exception:
            continue
        for u in re.findall(r'https?://[^"\'<> ]+',html_lib.unescape(raw),re.I):
            u=urllib.parse.unquote(u)
            if domain not in u: continue
            if 'google.' in u or 'bing.com' in u: continue
            # strip search tracking suffixes
            u=u.split('&')[0]
            if '/posts/' in u or '/status/' in u or 'photo.php' in u:
                urls.append(u)
        if urls: break
    seen=set(); out=[]
    for u in urls:
        if u not in seen:
            seen.add(u);out.append(u)
    return out[:15]

def _discover_social_posts(source, targets):
    stype=source.get('type'); label=source.get('label','專欄')
    if stype=='facebook':
        slug=source.get('slug','')
        links=[]
        if slug:
            page=f'https://www.facebook.com/{slug}'
            raw,_=_fetch_reader(page)
            patterns=[
                rf'https?://(?:www\.)?facebook\.com/{re.escape(slug)}/posts/[^\s"<>]+',
                rf'https?://(?:www\.)?facebook\.com/{re.escape(slug)}/posts/pfbid[^\s"<>]+'
            ]
            for p in patterns: links+=re.findall(p,raw,re.I)
            for m in re.findall(rf'/{re.escape(slug)}/posts/[^\s)"<>]+',raw,re.I):
                links.append('https://www.facebook.com'+m)
        # Some pages (e.g. 美股追夢路) don't expose a reliable custom slug publicly.
        # In that case, discover indexed public post URLs by the page's exact display name.
        if not links and source.get('search_name'):
            links.extend(_search_public_social_links(source.get('search_name'),'facebook.com'))
    else:
        handle=source.get('handle','')
        links=[]
        # Direct X page + public mirror fallback improves freshness without needing an X API key.
        for page in [f'https://x.com/{handle}',f'https://site.twstalker.com/{handle}']:
            raw,_=_fetch_reader(page)
            links+=re.findall(rf'https?://(?:x\.com|twitter\.com)/{re.escape(handle)}/status/\d+',raw,re.I)
            for m in re.findall(rf'/{re.escape(handle)}/status/\d+',raw,re.I):
                links.append('https://x.com'+m)
        if not links:
            links.extend(_search_public_social_links(handle,'x.com'))
    # de-dup / newest-looking first; cap requests
    uniq=[]; seen=set()
    for u in links:
        u=html_lib.unescape(u).split('?')[0]
        if u not in seen:
            seen.add(u);uniq.append(u)
    out=[]
    for u in uniq[:10]:
        body,_=_fetch_reader(u)
        plain=_plain_text_from_markdown(body)
        if len(plain)<80: continue
        ok,ticker,group,score=_social_relevance_and_target(plain,targets)
        if not ok: continue
        # Avoid comments / unrelated page chrome by trimming
        for marker in ['All reactions:','Most relevant replies','Post your reply','View more comments']:
            if marker in plain: plain=plain.split(marker,1)[0].strip()
        title=_social_title(plain,label)
        dt=_social_post_time(plain)
        out.append({
            'ticker':ticker,'group':group,'tag':'專欄' if stype=='facebook' else '快訊',
            'title':title,'summary':plain[:900],'originalTitle':title,'originalSummary':plain[:2200],
            'url':u,'ts':dt.isoformat(),'sourceType':'column_'+source.get('slug','') if stype=='facebook' else 'wallstengine_x',
            'analystPriority':is_broker_action_text(plain.lower()),'socialSource':label,'socialScore':score
        })
    return out

def market_news_search():
    """
    Broad U.S. market scan for the daily Top 10.
    This intentionally goes beyond the watchlist so macro/Fed/index/mega-cap
    stories can compete with stock-specific catalysts.
    """
    queries = [
        '("Wall Street" OR "U.S. stocks" OR "US stocks" OR "S&P 500" OR Nasdaq) '
        '(Fed OR "Federal Reserve" OR CPI OR inflation OR jobs OR payrolls OR Treasury OR yields OR tariffs OR oil OR recession) when:1d',
        '(Nvidia OR Microsoft OR Apple OR Amazon OR Alphabet OR Meta OR Tesla OR Broadcom) '
        '(earnings OR guidance OR outlook OR AI OR antitrust OR acquisition OR deal OR launch) when:1d',
        '("S&P 500" OR Nasdaq OR Dow) (futures OR rally OR selloff OR surge OR plunge OR record OR volatility) when:1d'
    ]

    now = datetime.now(timezone.utc)
    out = []
    seen = set()

    for q in queries:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
            "q": q,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en"
        })
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                root = ET.fromstring(r.read())
        except Exception:
            continue

        for item in root.findall(".//item")[:10]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = parse_rss_date(item.findtext("pubDate"))

            if pub and now - pub > timedelta(hours=30):
                continue

            clean_title = re.sub(r"\s+-\s+[^-]{2,80}$", "", title).strip() or title
            key = re.sub(r"\W+", "", clean_title.lower())
            if not key or key in seen:
                continue
            seen.add(key)

            blob = clean_title.lower()
            tag = "題材"
            if any(k in blob for k in NEG) or any(k in blob for k in [
                "selloff","plunge","slump","tariff","recession","hawkish","yields jump","inflation accelerates"
            ]):
                tag = "利空"
            elif any(k in blob for k in POS) or any(k in blob for k in [
                "rally","surge","record high","rate cut","dovish","inflation cools","jobs beat"
            ]):
                tag = "利多"

            out.append({
                "ticker": "MARKET",
                "group": "市場重點",
                "tag": tag,
                "title": clean_title,
                "summary": "",
                "originalTitle": clean_title,
                "originalSummary": "",
                "url": link,
                "ts": pub.isoformat() if pub else "",
                "sourceType": "market_search",
                "marketWide": True,
                "analystPriority": False
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

        analyst_priority = is_broker_action_text(blob)

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
        quote = get_quote(symbol)
        chg = quote.get("changePct")
        broker_scan_candidates.append((ticker, name, symbol, group))
        history_scan_candidates.append((ticker, name, symbol, group))
        stocks.append({
            "ticker": ticker,
            "name": name,
            "symbol": symbol,
            "changePct": chg,
            "regularPrice": quote.get("regularPrice"),
            "previousClose": quote.get("previousClose"),
            "preMarketChangePct": quote.get("preMarketChangePct"),
            "preMarketPrice": quote.get("preMarketPrice"),
            "postMarketChangePct": quote.get("postMarketChangePct"),
            "postMarketPrice": quote.get("postMarketPrice"),
            "marketState": quote.get("marketState"),
            "marketCap": quote.get("marketCap"),
            "marketCapSource": quote.get("marketCapSource")
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

# Broad U.S. market scan for the daily Top 10.
# Includes macro/Fed/index/mega-cap stories that may not map neatly to one watchlist ticker.
all_news.extend(market_news_search())

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

# TipRanks is an important analyst-rating data pool; the EVENT (rating/PT change) remains first priority.
tipranks_items=[]
with ThreadPoolExecutor(max_workers=12) as ex:
    futs=[ex.submit(tipranks_broker_search,ticker,name,symbol,group) for ticker,name,symbol,group in broker_scan_candidates]
    for fut in as_completed(futs):
        try: tipranks_items.extend(fut.result())
        except Exception: pass
all_news.extend(tipranks_items)

# One global major-broker pass per cycle.
# This catches stories that can be missed by the per-ticker RSS ranking,
# without issuing another request for every stock.
all_news.extend(major_broker_market_scan(broker_scan_candidates))

# Extra high-recall scan for major broker rating / target-price actions.
all_news.extend(major_broker_priority_scans(broker_scan_candidates))

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
    all_news.extend(moomoo_news_search(ticker, name, symbol, group, chg))

# Selected public columns / fast feeds. Not every post is included: the same watchlist/theme relevance filter applies.
for _src in (cfg.get("social_sources") or {}).values():
    try:
        all_news.extend(_discover_social_posts(_src, broker_scan_candidates))
    except Exception:
        pass

# Enrich broker/analyst stories so the detail modal has useful context and
# normalize_analyst_title can see exact old/new target values when available.
all_news = enrich_analyst_details(all_news)

# Re-evaluate analyst classification on every run, including retained old news.
for x in all_news:
    blob = " ".join([
        x.get("originalTitle") or "",
        x.get("originalSummary") or "",
    ]).lower()
    x["analystPriority"] = is_broker_action_text(blob)

# Score + deduplicate news.
# Goal: "latest + important", rather than simply newest.
def importance_score(x):
    blob = ((x.get("originalTitle") or "") + " " + (x.get("originalSummary") or "")).lower()

    score = 1.0

    # Broad market-moving events get extra weight for the daily Top 10.
    if x.get("marketWide") or x.get("sourceType") == "market_search":
        score += 2.5
    if any(k in blob for k in [
        "federal reserve","fed ","interest rate","rate cut","rate hike","cpi","inflation",
        "nonfarm payroll","payrolls","jobs report","treasury yield","treasury yields",
        "tariff","recession","government shutdown"
    ]):
        score += 7
    if any(k in blob for k in [
        "nvidia","microsoft","apple","amazon","alphabet","google","meta","tesla","broadcom"
    ]) and any(k in blob for k in ["earnings","guidance","outlook","acquisition","antitrust","ai"]):
        score += 4
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
        score += 130
    if x.get("sourceType") == "tipranks_broker":
        score += 35
    if x.get("sourceType") == "wallstengine_x":
        score += 14
    if str(x.get("sourceType") or "").startswith("column_"):
        score += 8

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
        if (x.get("featured") or x.get("analystPriority")) and raw_summary:
            x["summary"] = (cached_summary or raw_summary)[:520]
        else:
            x["summary"] = ""
        continue

    translated_title = uncached_titles.get(raw_title) or raw_title
    x["title"] = translated_title
    x["translated"] = has_cjk(x["title"])

    # Only featured homepage stories get translated summaries.
    if (x.get("featured") or x.get("analystPriority")) and raw_summary:
        translated_summary = zh(raw_summary[:420])
        x["summary"] = (translated_summary or raw_summary)[:520]
    else:
        x["summary"] = ""

# Normalize broker/analyst headlines after translation so key investment facts are visible at a glance.
for x in news:
    if x.get("analystPriority"):
        x["title"] = normalize_analyst_title(x)


def _first_sentences(text, n=2, max_chars=260):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？.!?])\s+", text)
    parts = [p.strip() for p in parts if p.strip()]
    out = " ".join(parts[:n]) if parts else text
    return out[:max_chars].rstrip()

def _extract_key_numbers(text):
    text = text or ""
    patterns = [
        r"\$[\d,.]+\s*(?:billion|million|B|M)?",
        r"\b\d+(?:\.\d+)?\s*%",
        r"\b\d+(?:\.\d+)?\s*(?:MW|GW|TB|GB|Gbps|Tbps)\b",
        r"\b\d+(?:\.\d+)?\s*(?:billion|million)\b",
    ]
    vals = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            v = m.group(0).strip()
            if v not in vals:
                vals.append(v)
            if len(vals) >= 4:
                return vals
    return vals


def _split_sentences(text):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[。！？.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]

def _best_summary_sentences(text, max_sentences=4, max_chars=520):
    """
    Select the most information-dense sentences from the available source snippet.
    This is not a full-article copy: it keeps a short set of sentences only.
    """
    sents = _split_sentences(text)
    if not sents:
        return ""

    keywords = [
        "revenue","eps","margin","guidance","outlook","price target","rating",
        "customer","order","contract","capacity","demand","supply","backlog",
        "ai","data center","datacenter","cloud","launch","approval",
        "partnership","investment","capex","mw","gw","billion","million","%"
    ]

    scored = []
    for i, s in enumerate(sents):
        low = s.lower()
        score = sum(1 for k in keywords if k in low)
        if re.search(r"\$[\d,.]+|\b\d+(?:\.\d+)?%", s):
            score += 2
        # Earlier sentences are often more important.
        score += max(0, 2 - i * 0.25)
        scored.append((score, i, s))

    chosen = sorted(scored, reverse=True)[:max_sentences]
    chosen = [x[2] for x in sorted(chosen, key=lambda z: z[1])]

    out = " ".join(chosen)
    return out[:max_chars].rstrip()

def _analyst_specific_reason(x):
    """
    Return the analyst's actual stated rationale from the available translated
    summary when possible, rather than a generic explanation.
    """
    summary = (x.get("summary") or "").strip()
    original = (x.get("originalSummary") or "").strip()

    # Prefer the translated summary because this text is shown to the user.
    source = summary or (zh(original[:700]) if original else "")
    if not source:
        return ""

    sents = _split_sentences(source)
    if not sents:
        return source[:420]

    reason_words = [
        "因為","由於","理由","看好","認為","預期","受惠","需求","成長",
        "毛利","營收","訂單","定價","市占","雲端","AI","人工智慧",
        "because","citing","expects","growth","demand","margin","revenue",
        "pricing","orders","cloud","artificial intelligence"
    ]

    picked = []
    for s in sents:
        low = s.lower()
        if any(k.lower() in low for k in reason_words):
            picked.append(s)
        if len(picked) >= 3:
            break

    if not picked:
        picked = sents[:2]

    return " ".join(picked)[:520].strip()


def _news_why_it_matters(x, blob):
    if x.get("analystPriority"):
        specific = _analyst_specific_reason(x)
        if specific:
            return "券商調整理由：" + specific
        return "目前來源只確認券商評級／目標價有變動，但沒有提供足夠理由；不要把缺少依據的評級變動直接解讀成基本面改善。"

    if any(k in blob for k in ["guidance","outlook","forecast","earnings","revenue","eps"]):
        return "這會直接影響市場對未來營收、EPS 與估值的預期。投資上比已公布的單季數字更重要的是公司接下來的財測方向、毛利與需求能見度。"

    if any(k in blob for k in ["order","contract","customer","agreement","deal","partnership"]):
        return "重點在於這筆合作／訂單能否轉成可量化營收，以及客戶是否具有延續性。如果能提高未來數季的訂單能見度，投資意義會高於單純題材。"

    if any(k in blob for k in ["shortage","undersupply","capacity","supply constraint","tight supply","pricing"]):
        return "供需緊張可能帶來漲價與毛利改善，但也可能限制出貨量。要區分公司是受惠於價格提升，還是反而因缺料而無法滿足需求。"

    if any(k in blob for k in ["nuclear","electricity","power plant","megawatt"," mw","gigawatt"," gw"]) and any(
        k in blob for k in ["ai","data center","datacenter","google","microsoft","amazon","meta"]
    ):
        return "真正的投資重點是 AI 資料中心的電力需求正在變成算力擴張瓶頸。大型科技公司若開始直接參與電力供給，通常對核電、電網、電力設備與資料中心基礎建設需求偏正面。"

    if any(k in blob for k in ["launch","unveil","approval","approved","product"]):
        return "先看新產品／核准是否會形成新的收入來源，再看量產時間、客戶採用與市場規模。只有產品發布本身，不一定等於短期 EPS 貢獻。"

    if any(k in blob for k in ["artificial intelligence"," ai ","data center","datacenter"]):
        return "核心要判斷這件事是否真的增加 AI 算力需求、使用量、資本支出或新收入來源，而不是只停留在 AI 題材層面。"

    return "投資上先確認這則消息是否會影響營收、毛利、訂單能見度、需求或估值。如果來源沒有量化財務資訊，就先把它視為題材訊號，而不是直接等同獲利成長。"


def _news_stated_view(x):
    """
    Build the '公司／券商怎麼說' field from what the source actually says.

    Important:
    - Do NOT output generic boilerplate such as "這是券商觀點..."
    - Prefer a concise summary of the analyst/company's actual thesis.
    - If the source doesn't contain enough information, say so explicitly.
    """
    summary = (x.get("summary") or "").strip()
    original = (x.get("originalSummary") or "").strip()
    source = summary or (zh(original[:900]) if original else "")

    if not source:
        if x.get("analystPriority"):
            return "目前來源只確認券商有評級／目標價動作，但沒有提供足夠的分析師論點。"
        return "目前來源沒有提供足夠的公司／管理層說法。"

    sents = _split_sentences(source)
    if not sents:
        return source[:520]

    if x.get("analystPriority"):
        # Analyst thesis / rationale.
        keys = [
            "認為","看好","預期","指出","表示","理由","因為","由於","受惠",
            "成長","需求","毛利","營收","訂單","定價","市占","AI","人工智慧",
            "cloud","雲端","because","citing","expects","believes","sees",
            "growth","demand","margin","revenue","pricing","orders"
        ]
    else:
        # Company / management commentary.
        keys = [
            "公司表示","公司指出","管理層","執行長","財務長","CEO","CFO",
            "預期","預計","展望","guidance","expects","said","management",
            "demand","需求","capacity","產能","orders","訂單","margin","毛利"
        ]

    picked = []
    for s in sents:
        low = s.lower()
        if any(k.lower() in low for k in keys):
            picked.append(s)
        if len(picked) >= 3:
            break

    # If nothing specific is found, use the first 1-2 source sentences rather
    # than a generic template.
    if not picked:
        picked = sents[:2]

    out = " ".join(picked).strip()
    return out[:560]


def _news_watch_items(blob):
    items = []
    if "price target" in blob or "rating" in blob or "upgrade" in blob or "downgrade" in blob:
        items.append("評級是否持續，以及後續是否有其他大型券商跟進")
    if any(k in blob for k in ["guidance","outlook","forecast"]):
        items.append("下一季／全年財測是否再次上修或下修")
    if any(k in blob for k in ["customer","contract","order","partnership"]):
        items.append("合作金額、出貨時程與實際營收貢獻")
    if any(k in blob for k in ["capacity","shortage","supply","demand"]):
        items.append("供需是否持續，以及價格／毛利是否同步改善")
    if any(k in blob for k in ["ai","data center","datacenter"]):
        items.append("AI 需求是否能轉成實際訂單、使用量或資本支出")
    if not items:
        items.append("後續是否出現可量化的營收、毛利或訂單數據")
    return items[:3]

def build_investor_brief(x):
    title = (x.get("title") or "").strip()
    zh_summary = (x.get("summary") or "").strip()
    original_title = (x.get("originalTitle") or "").strip()
    original_summary = (x.get("originalSummary") or "").strip()

    source_blob = " ".join([original_title, original_summary]).strip()
    blob = source_blob.lower()

    # Use the translated summary if available; otherwise translate a compact,
    # information-dense source excerpt.
    event = _best_summary_sentences(zh_summary, max_sentences=4, max_chars=520)
    if not event and original_summary:
        compact_source = _best_summary_sentences(original_summary, max_sentences=4, max_chars=520)
        translated = zh(compact_source) if compact_source else ""
        event = translated or title
    if not event:
        event = title

    numbers = _extract_key_numbers(source_blob)
    why = _news_why_it_matters(x, blob)
    watch = _news_watch_items(blob)

    # What the company / analyst actually says.
    # Never use generic boilerplate here.
    stance = _news_stated_view(x)

    return {
        "event": event,
        "numbers": numbers,
        "why": why,
        "stance": stance,
        "watch": watch,
        # Keep old key for backward compatibility with push/UI.
        "takeaway": why,
    }

for x in news:
    x["investorBrief"] = build_investor_brief(x)



# ---------- Daily U.S. market Top 10 ----------
# "Today" follows New York calendar date, so Taiwan morning still maps to the
# just-finished U.S. trading day. This naturally covers pre-market, regular
# session, and after-hours news within the same U.S. date.
market_day_et = datetime.now(ZoneInfo("America/New_York")).date()

for x in news:
    x["top10"] = False
    x["top10Rank"] = None

top10_candidates = []
for x in news:
    try:
        dt = datetime.fromisoformat((x.get("ts") or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.astimezone(ZoneInfo("America/New_York")).date() != market_day_et:
            continue
    except Exception:
        continue
    top10_candidates.append(x)

# Rank by our importance score, then freshness. Analyst changes remain high
# priority, but market-wide macro events can also rank at the top.
top10_candidates.sort(
    key=lambda z: (
        1 if z.get("analystPriority") else 0,
        z.get("score", 0),
        z.get("ts", "")
    ),
    reverse=True
)

# Prevent one ticker from monopolizing the Top 10 unless the event flow is very sparse.
top10 = []
ticker_counts = {}
for x in top10_candidates:
    t = (x.get("ticker") or "MARKET").upper()
    if ticker_counts.get(t, 0) >= 2 and len(top10_candidates) > 12:
        continue
    ticker_counts[t] = ticker_counts.get(t, 0) + 1
    top10.append(x)
    if len(top10) >= 10:
        break

for i, x in enumerate(top10, 1):
    x["top10"] = True
    x["top10Rank"] = i



# ---------- Tier 1 iPhone push notifications ----------
# Secrets are supplied by GitHub Actions:
#   PUSHOVER_APP_TOKEN
#   PUSHOVER_USER_KEY
# Never hard-code them in the repository.

def _push_key(x):
    """
    Stable event key for push dedupe.

    IMPORTANT:
    Do NOT include the URL. The same story is often syndicated through Yahoo,
    Google News, Investing.com, etc. with different URLs. We dedupe primarily
    by ticker + normalized original headline.
    """
    ticker = (x.get("ticker") or "MARKET").upper().strip()
    title = (x.get("originalTitle") or x.get("title") or "").lower().strip()

    # Remove source suffixes and punctuation/noise that commonly differ between feeds.
    title = re.sub(r"\s+-\s+(reuters|bloomberg|benzinga|investing\.com|yahoo finance|marketwatch|barron's|thefly|seeking alpha)\s*$", "", title, flags=re.I)
    title = re.sub(r"\b(by investing\.com|via reuters|via bloomberg)\b", "", title, flags=re.I)
    title = re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    return f"{ticker}|{title}"[:900]


def _tier1_reason(x):
    """
    Return (is_tier1, category, reason).

    Tier 1 is event-based, not keyword-label-based.
    A story can be important even if it is NOT a broker story.
    """
    blob = " ".join([
        x.get("originalTitle") or "",
        x.get("originalSummary") or "",
        x.get("title") or "",
        x.get("summary") or "",
    ]).lower()

    # --- A. Broker / analyst actions ---
    broker_terms = [
        "price target", "target price", "price-target", "pt raised", "pt cut",
        "upgrade", "upgraded", "downgrade", "downgraded",
        "initiates coverage", "initiated coverage", "initiates with", "initiated with",
        "reiterates", "reiterated", "rating raised", "rating cut",
        "raises target", "raised target", "cuts target", "cut target",
        "lowers target", "lowered target", "boosts target", "boosted target"
    ]
    if is_broker_action_text(blob):
        positive = any(k in blob for k in [
            "price target raised","raises price target","raised price target",
            "price target increased","boosts price target","target raised",
            "upgrade","upgraded","initiates with buy","initiated with buy",
            "initiates at buy","initiated at buy","outperform","overweight"
        ])
        negative = any(k in blob for k in [
            "price target cut","cuts price target","cut price target",
            "price target lowered","target cut","downgrade","downgraded",
            "underperform","underweight","sell rating"
        ])
        if negative and not positive:
            return True, "券商", "降評／目標價下修"
        if positive and not negative:
            return True, "券商", "升評／目標價上修／初評"
        return True, "券商", "重大券商評級／目標價變動"

    # --- B. Company guidance / financial outlook ---
    if any(k in blob for k in [
        "raises guidance","raise guidance","raised guidance","boosts outlook",
        "raises outlook","increases guidance","guidance raised",
        "raises revenue outlook","raises eps outlook","raises margin outlook",
        "raises capex","increases capex"
    ]):
        return True, "公司財測", "上調財測／展望"

    if any(k in blob for k in [
        "cuts guidance","cut guidance","lowers guidance","lower guidance",
        "guidance cut","cuts outlook","lowers outlook",
        "cuts revenue outlook","cuts eps outlook","cuts margin outlook",
        "cuts capex","reduces capex"
    ]):
        return True, "公司財測", "下調財測／展望"

    # --- C. Supply / demand / capacity statements that can change earnings expectations ---
    # This is where the earlier INTC story belongs.
    supply_terms = [
        "supply shortage","supply shortages","shortage could last","shortages could last",
        "supply constraint","supply constraints","constrained supply",
        "capacity shortage","capacity constraint","capacity constraints",
        "sold out through","supply sold out","undersupply","under-supply",
        "demand exceeds supply","supply bottleneck","supply bottlenecks",
        "component shortage","component shortages"
    ]
    if any(k in blob for k in supply_terms):
        return True, "供應鏈／展望", "供給瓶頸／供需變化可能影響出貨與獲利"

    demand_terms = [
        "demand accelerates","demand surges","demand remains strong",
        "weak demand","demand weakens","demand slowdown","demand slows",
        "order slowdown","orders slow","order growth accelerates",
        "bookings surge","backlog jumps","backlog grows"
    ]
    if any(k in blob for k in demand_terms):
        return True, "需求／訂單", "需求、訂單或 backlog 出現重大變化"

    # --- D. Short / activist / significant stake disclosures ---
    if any(k in blob for k in [
        "short report","short seller","short-seller","short thesis",
        "activist stake","activist investor","takes stake","builds stake",
        "discloses stake","13d filing","13g filing"
    ]):
        return True, "重大持倉", "放空報告／重大持股揭露"

    # --- E. Material contracts / customers / M&A / regulatory decisions ---
    if any(k in blob for k in [
        "major contract","wins contract","contract win","multi-year contract",
        "strategic partnership","acquisition","acquire","merger",
        "fda approval","regulatory approval","antitrust approval",
        "government contract","hyperscaler customer","new hyperscaler",
        "new customer","design win","design-win"
    ]):
        return True, "重大事件", "重大訂單／客戶／併購／核准"

    # --- F. Material product/platform launches ---
    product_action = any(k in blob for k in [
        "unveils","launches","announces new","introduces new","debuts"
    ])
    product_object = any(k in blob for k in [
        "chip","gpu","cpu","accelerator","ai model","platform","data center",
        "datacenter","server","optical","transceiver","networking",
        "robot","vehicle","processor","architecture"
    ])
    materiality = any(k in blob for k in [
        "next-generation","next generation","flagship","new architecture",
        "mass production","volume production","commercial launch",
        "major launch","first-of-its-kind","industry first"
    ])
    if product_action and product_object and materiality:
        return True, "新產品", "重大新產品／平台發布"

    # --- G. Very large move + identified catalyst ---
    mv = safe_float(x.get("movePct"))
    if mv is not None and abs(mv) >= 8 and x.get("sourceType") in (
        "active_search","ticker_feed","broker_search","major_broker_global","broker_priority_scan","market_search"
    ):
        return True, "股價異動", f"股價異動 {mv:+.1f}% 且有明確催化劑"

    return False, "", ""


def _push_title(x, category):
    ticker = (x.get("ticker") or "MARKET").upper()
    if ticker == "MARKET":
        ticker = "美股市場"

    headline = (x.get("title") or x.get("originalTitle") or "").strip()
    if category == "券商" and headline:
        compact = headline.replace(ticker, "").strip(" ｜-—")
        return f"{ticker}｜{compact}"[:120]

    return f"{ticker}｜{category}"

def _push_message(x, reason):
    title = (x.get("title") or x.get("originalTitle") or "").strip()
    summary = (x.get("summary") or x.get("originalSummary") or "").strip()

    lines = []
    if title:
        lines.append(f"事件：{title}")

    if summary:
        clean = re.sub(r"\s+", " ", summary).strip()
        lines.append(f"重點：{clean[:320]}")
    elif reason:
        lines.append(f"重點：{reason}")

    brief = x.get("investorBrief") or {}
    takeaway = (brief.get("takeaway") or "").strip() if isinstance(brief, dict) else ""
    if takeaway:
        lines.append(f"投資意義：{takeaway[:240]}")

    return "\n".join(lines)[:900]


def _send_pushover(title, message, url=None):
    token = (os.environ.get("PUSHOVER_APP_TOKEN") or "").strip()
    user = (os.environ.get("PUSHOVER_USER_KEY") or "").strip()
    if not token or not user:
        return False, "missing_secrets"

    payload = {
        "token": token,
        "user": user,
        "title": title,
        "message": message,
        "priority": "0",
    }
    if url:
        payload["url"] = url
        payload["url_title"] = "開啟美股觀察"

    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "us-stock-watch/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = 200 <= getattr(r, "status", 200) < 300
        return ok, "ok" if ok else "http_error"
    except Exception as e:
        return False, type(e).__name__

def process_tier1_pushes(news_items):
    """
    Tier 1 monitor mode:
      - Only push genuinely NEW stories from the latest scan.
      - Old stories remain searchable on the website but are never backfilled to push.
      - Persistent seen-state prevents repeat alerts.
      - Stable event-key dedupe prevents syndicated duplicates.

    Freshness policy:
      - Story timestamp must be within the last 12 minutes.
      - This matches the 5-minute monitoring cadence while allowing for feed delays.
    """
    try:
        state = json.loads(PUSH_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {"initialized": False, "seen": []}

    seen = set(state.get("seen") or [])
    initialized = bool(state.get("initialized"))
    now = datetime.now(timezone.utc)

    # Build one candidate per stable event key.
    by_key = {}

    for x in news_items:
        is_t1, category, reason = _tier1_reason(x)
        if not is_t1:
            continue

        # MUST be genuinely fresh. Historical/recent-but-old stories stay on site only.
        try:
            dt = datetime.fromisoformat((x.get("ts") or "").replace("Z","+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = now - dt
            if age < timedelta(minutes=-2):
                # Ignore bad future timestamps.
                continue
            if age > timedelta(minutes=12):
                continue
        except Exception:
            continue

        key = _push_key(x)
        if not key:
            continue

        rank = (
            1 if x.get("analystPriority") else 0,
            safe_float(x.get("score")) or 0,
            x.get("ts") or "",
            len(x.get("title") or ""),
        )
        prev = by_key.get(key)
        if prev is None or rank > prev[0]:
            by_key[key] = (rank, x, category, reason)

    tier1_now = [
        (x, key, category, reason)
        for key, (_, x, category, reason) in by_key.items()
    ]

    # First deployment / reset:
    # seed all currently-known Tier 1 events and send NOTHING.
    # This guarantees no historical flood.
    if not initialized:
        for x in news_items:
            is_t1, _, _ = _tier1_reason(x)
            if is_t1:
                key = _push_key(x)
                if key:
                    seen.add(key)
        state = {
            "initialized": True,
            "updatedAt": now.isoformat(),
            "seen": list(seen)[-2000:],
            "lastPushes": [],
            "macroReminderDays": state.get("macroReminderDays", []),
            "macroResultKeys": state.get("macroResultKeys", []),
            "macroUpdatedAt": state.get("macroUpdatedAt", ""),
        }
        PUSH_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return

    sent = []
    tier1_now.sort(key=lambda z: z[0].get("ts",""))

    for x, key, category, reason in tier1_now:
        if key in seen:
            continue

        deep_key = _push_key(x)
        deep_url = (
            "https://morris199786.github.io/us-stock-watch/?news="
            + urllib.parse.quote(deep_key, safe="")
        )

        ok, status = _send_pushover(
            _push_title(x, category),
            _push_message(x, reason),
            url=deep_url
        )

        if ok:
            seen.add(key)
            sent.append({
                "ticker": x.get("ticker"),
                "title": x.get("title"),
                "category": category,
                "reason": reason,
                "ts": x.get("ts"),
                "eventKey": key,
            })

        if len(sent) >= 6:
            break

    state = {
        "initialized": True,
        "updatedAt": now.isoformat(),
        "seen": list(seen)[-2000:],
        "lastPushes": sent[-20:],
        "macroReminderDays": state.get("macroReminderDays", []),
        "macroResultKeys": state.get("macroResultKeys", []),
        "macroUpdatedAt": state.get("macroUpdatedAt", ""),
    }
    PUSH_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


# Evaluate Tier 1 only after titles have been translated / normalized.
process_tier1_pushes(news)



# ---------- U.S. Congress trades ----------
# Historical source: Kadoa open Congress Trading Monitor (official House/Senate filings normalized)
# We keep records from 2024-01-01 onward so member searches can reach back to 2024.

EARNINGS_FILE = ROOT / "earnings.json"

def _fmt_money(v):
    try:v=float(v)
    except Exception:return None
    a=abs(v)
    if a>=1e12:return f"${v/1e12:.2f}T"
    if a>=1e9:return f"${v/1e9:.2f}B"
    if a>=1e6:return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"

def _num_with_unit(s):
    if s is None:return None
    s=str(s).replace(',','').strip()
    m=re.match(r'\$?([+-]?\d+(?:\.\d+)?)\s*([TtBbMmKk]?)',s)
    if not m:return None
    v=float(m.group(1)); u=m.group(2).lower()
    return v*({'t':1e12,'b':1e9,'m':1e6,'k':1e3}.get(u,1))

def _earnings_news_context(ticker, name, report_date):
    """
    Cross-source earnings research:
      - result / estimate comparisons
      - guidance / outlook
      - earnings-call / transcript / management comments
      - after-hours / premarket reaction
    """
    queries = [
        f'("{ticker}" OR "{name}") earnings revenue EPS estimate guidance {report_date} when:21d',
        f'("{ticker}" OR "{name}") "earnings call" transcript guidance outlook {report_date} when:21d',
        f'("{ticker}" OR "{name}") earnings after-hours premarket shares {report_date} when:21d',
        f'("{ticker}" OR "{name}") results outlook forecast revenue EPS {report_date} when:21d',
    ]

    seen = set()
    rows = []

    for q in queries:
        url = 'https://news.google.com/rss/search?' + urllib.parse.urlencode({
            'q': q, 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'
        })
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                root = ET.fromstring(r.read())
        except Exception:
            continue

        for item in root.findall('.//item')[:14]:
            title = (item.findtext('title') or '').strip()
            link = (item.findtext('link') or '').strip()
            pub = parse_rss_date(item.findtext('pubDate'))
            if not target_relevance(title, ticker, name):
                continue

            key = re.sub(r'\W+', '', title.lower())
            if not key or key in seen:
                continue
            seen.add(key)

            body, resolved = _fetch_article_context(link)
            body = (body or '').strip()
            resolved = resolved or link

            if _is_polluted_text(body) or (resolved and not _is_valid_article_url(resolved) and "news.google." not in resolved):
                body = ''
                resolved = link

            rows.append({
                'title': title,
                'text': body or title,
                'url': resolved,
                'ts': pub.isoformat() if pub else ''
            })

    # Rank useful pieces first: transcript/guidance and estimate/reaction stories.
    def score(a):
        b = (a.get('title','') + ' ' + a.get('text','')).lower()
        s = 0
        if any(k in b for k in ['earnings call','transcript','prepared remarks']): s += 5
        if any(k in b for k in ['guidance','outlook','forecast','expects','projects']): s += 4
        if any(k in b for k in ['estimate','consensus','expected','vs.','versus']): s += 3
        if any(k in b for k in ['after-hours','after hours','premarket','pre-market']): s += 2
        if len(a.get('text','')) > 500: s += 2
        return s

    rows.sort(key=lambda a: (score(a), a.get('ts','')), reverse=True)
    return rows[:12]

def _extract_estimate(text, metric):
    t=re.sub(r'\s+',' ',text or '')
    if metric=='revenue':
        pats=[
            r'(?:revenue|sales)[^.$]{0,45}\$([\d,.]+\s*[TBMK]?)[^.$]{0,60}(?:est\.?|estimate|expected|consensus)[^.$]{0,20}\$([\d,.]+\s*[TBMK]?)',
            r'\$([\d,.]+\s*[TBMK]?)\s+(?:revenue|sales)[^.$]{0,60}(?:vs\.?|versus)[^.$]{0,20}\$([\d,.]+\s*[TBMK]?)'
        ]
    else:
        pats=[
            r'(?:adjusted\s+)?EPS[^\d$]{0,25}\$?([\d.]+)[^\d$]{0,55}(?:est\.?|estimate|expected|consensus)[^\d$]{0,20}\$?([\d.]+)',
            r'EPS[^\d$]{0,20}\$?([\d.]+)[^\d$]{0,30}(?:vs\.?|versus)[^\d$]{0,15}\$?([\d.]+)'
        ]
    for p in pats:
        m=re.search(p,t,re.I)
        if m:return _num_with_unit(m.group(1)),_num_with_unit(m.group(2))
    return None,None

def _extract_guidance_rows(text):
    out = []
    sents = _split_sentences(text or '')
    guide_terms = ['guidance','outlook','forecast','expects','expect','sees','projects','projects to','guided','guides']
    metrics = ['revenue','sales','eps','earnings per share','gross margin','operating margin','margin','growth','capex','capital expenditure']

    for s in sents:
        low = s.lower()
        if not any(k in low for k in guide_terms):
            continue
        if not any(k in low for k in metrics):
            continue

        # A useful guidance sentence should contain a number/range/percentage.
        if not re.search(r'\$?\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\$?\d+(?:\.\d+)?)?\s*(?:%|[TBMK])?', s, re.I):
            continue

        est = ''
        m = re.search(
            r'(?:est\.?|estimate|consensus|expected by analysts|analysts expected|street expected)'
            r'[^$\d]{0,30}(\$?[\d,.]+\s*[TBMK]?|\d+(?:\.\d+)?%)',
            s, re.I
        )
        if m:
            est = m.group(1)

        metric = '財測'
        if 'revenue' in low or 'sales' in low: metric = 'Revenue'
        elif 'eps' in low or 'earnings per share' in low: metric = 'EPS'
        elif 'gross margin' in low: metric = 'Gross Margin'
        elif 'operating margin' in low: metric = 'Operating Margin'
        elif 'capex' in low or 'capital expenditure' in low: metric = 'CapEx'

        row = {
            'metric': metric,
            'companyGuide': (zh(s[:520]) or s[:520]),
            'estimate': est
        }
        sig = metric + '|' + row['companyGuide']
        if not any((z['metric'] + '|' + z['companyGuide']) == sig for z in out):
            out.append(row)
        if len(out) >= 6:
            break
    return out

def _management_bullets(text):
    keys = [
        'ceo','cfo','management','said','expects','expect','demand','backlog','capacity',
        'margin','pricing','customer','ai','data center','cloud','supply','order','guidance',
        'capex','capital expenditure','shipments','production','bookings','rpo'
    ]
    picks = []
    for s in _split_sentences(text or ''):
        low = s.lower()
        if _is_polluted_text(s):
            continue
        if any(k in low for k in keys) and len(s) > 55:
            z = zh(s[:650]) or s[:650]
            if z not in picks:
                picks.append(z)
        if len(picks) >= 7:
            break
    return picks

def _first_reaction(text):
    for s in _split_sentences(text or ''):
        low=s.lower()
        if not any(k in low for k in ['after hours','after-hours','premarket','pre-market','extended trading']):continue
        m=re.search(r'(rose|gained|jumped|surged|rallied|fell|dropped|slid|declined|tumbled)\s+(?:as much as\s+)?([\d.]+)%',s,re.I)
        if m:
            v=float(m.group(2));
            if m.group(1).lower() in ['fell','dropped','slid','declined','tumbled']:v=-v
            return v
    return None

def _extended_reaction_from_prices(symbol, report_dt):
    """
    Recent-report fallback using Yahoo extended-hours bars.
    Returns the first observable extended-session move after the earnings time.
    """
    try:
        now = datetime.now(timezone.utc)
        if now - report_dt > timedelta(days=55):
            return None

        et = report_dt.astimezone(ZoneInfo("America/New_York"))
        t = yf.Ticker(symbol)
        start = (et.date() - timedelta(days=2)).isoformat()
        end = (et.date() + timedelta(days=3)).isoformat()

        daily = t.history(start=start, end=end, interval="1d", auto_adjust=False)
        intr = t.history(start=start, end=end, interval="5m", prepost=True, auto_adjust=False)

        if daily is None or daily.empty or intr is None or intr.empty:
            return None

        drows = [(idx.date(), safe_float(row.get('Close'))) for idx, row in daily.iterrows()]
        drows = [(d, p) for d, p in drows if p not in (None, 0)]

        if et.hour >= 16:
            bases = [p for d,p in drows if d == et.date()]
            if not bases:
                bases = [p for d,p in drows if d < et.date()]
            base = bases[-1] if bases else None
        else:
            bases = [p for d,p in drows if d < et.date()]
            base = bases[-1] if bases else None

        if not base:
            return None

        candidates = []
        for ix, row in intr.iterrows():
            try:
                ix_et = ix.tz_convert("America/New_York") if getattr(ix, "tzinfo", None) else ix.tz_localize("UTC").tz_convert("America/New_York")
            except Exception:
                continue
            p = safe_float(row.get('Close'))
            if p in (None, 0):
                continue
            if ix_et.to_pydatetime() >= et and ix_et.to_pydatetime() <= et + timedelta(hours=8):
                candidates.append(p)

        if not candidates:
            return None
        return (candidates[0] / base - 1) * 100
    except Exception:
        return None

def _next_close_reaction(symbol, report_dt):
    try:
        et = report_dt.astimezone(ZoneInfo("America/New_York"))
        d = et.date()
        start = (d - timedelta(days=5)).isoformat()
        end = (d + timedelta(days=8)).isoformat()
        h = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False)
        if h is None or len(h) < 2:
            return None

        days = [(idx.date(), safe_float(row.get('Close'))) for idx,row in h.iterrows()]
        days = [(day, p) for day,p in days if p not in (None,0)]

        if et.hour >= 16:
            base_rows = [x for x in days if x[0] == d]
            if not base_rows:
                base_rows = [x for x in days if x[0] < d]
            after = [x for x in days if x[0] > d]
        else:
            base_rows = [x for x in days if x[0] < d]
            after = [x for x in days if x[0] >= d]

        if not base_rows or not after:
            return None

        base = base_rows[-1][1]
        target = after[0][1]
        return (target / base - 1) * 100 if base else None
    except Exception:
        return None

def _quarterly_revenue_actual(t,report_dt):
    try:
        q=t.quarterly_financials
        if q is None or q.empty:return None
        rows=[r for r in ['Total Revenue','Operating Revenue'] if r in q.index]
        if not rows:return None
        candidates=[]
        for col in q.columns:
            cd=col.date() if hasattr(col,'date') else col
            diff=(report_dt.date()-cd).days
            if 15<=diff<=120:
                candidates.append((diff,safe_float(q.loc[rows[0],col])))
        candidates=[x for x in candidates if x[1] is not None]
        if not candidates:return None
        candidates.sort(key=lambda x:x[0]);return candidates[0][1]
    except Exception:return None

def _earnings_dates_one(ticker,name,symbol,group):
    try:
        t=yf.Ticker(symbol); df=t.get_earnings_dates(limit=max(6,int(cfg.get('earnings_history_per_stock',4))+2))
        if df is None or df.empty:return {'ticker':ticker,'name':name,'symbol':symbol,'group':group,'dates':[]}
        out=[]
        now=datetime.now(timezone.utc)
        for idx,row in df.iterrows():
            try:dt=idx.to_pydatetime() if hasattr(idx,'to_pydatetime') else idx
            except Exception:continue
            if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
            else:dt=dt.astimezone(timezone.utc)
            if dt<now-timedelta(days=430) or dt>now+timedelta(days=120):continue
            eps_est=safe_float(row.get('EPS Estimate')); eps_act=safe_float(row.get('Reported EPS')); surprise=safe_float(row.get('Surprise(%)'))
            out.append({'date':dt.isoformat(),'epsEstimate':eps_est,'epsActual':eps_act,'epsSurprisePct':surprise})
        return {'ticker':ticker,'name':name,'symbol':symbol,'group':group,'dates':out}
    except Exception:return {'ticker':ticker,'name':name,'symbol':symbol,'group':group,'dates':[]}

def refresh_earnings_data(targets):
    now=datetime.now(timezone.utc)
    old={}
    try:old=json.loads(EARNINGS_FILE.read_text(encoding='utf-8'))
    except Exception:old={}
    # keep the 5-minute workflow sane; refresh this heavier dataset about every 30 minutes
    try:
        old_dt=datetime.fromisoformat((old.get('updatedAtUtc') or '').replace('Z','+00:00'))
        if old_dt.tzinfo is None:old_dt=old_dt.replace(tzinfo=timezone.utc)
        if now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',30))):return
    except Exception:pass

    calendars=[]
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs=[ex.submit(_earnings_dates_one,*x) for x in targets]
        for fut in as_completed(futs):
            try:calendars.append(fut.result())
            except Exception:pass

    # Remove resolver-polluted cached earnings enrichment before re-use.
    clean_old_reports = []
    for r in (old.get('reports') or []):
        if not isinstance(r, dict):
            continue
        blob = json.dumps({
            'guidance': r.get('guidance'),
            'management': r.get('management'),
            'sources': r.get('sources')
        }, ensure_ascii=False)
        if _is_polluted_text(blob):
            r = dict(r)
            r['guidance'] = []
            r['management'] = []
            r['sources'] = []
            r['sourceCount'] = 0
        clean_old_reports.append(r)
    old['reports'] = clean_old_reports

    old_map={(x.get('symbol'),x.get('reportDate')):x for x in (old.get('reports') or []) if isinstance(x,dict)}
    reports=[]; upcoming=[]
    raw_reports=[]
    for c in calendars:
        ds=sorted(c['dates'],key=lambda x:x['date'],reverse=True)
        past=[x for x in ds if datetime.fromisoformat(x['date'].replace('Z','+00:00'))<=now+timedelta(hours=6)]
        future=[x for x in ds if datetime.fromisoformat(x['date'].replace('Z','+00:00'))>now+timedelta(hours=6)]
        for x in future[:1]:
            upcoming.append({'ticker':c['ticker'],'name':c['name'],'symbol':c['symbol'],'group':c['group'],'date':x['date']})
        for x in past[:int(cfg.get('earnings_history_per_stock',4))]:
            raw_reports.append((datetime.fromisoformat(x['date'].replace('Z','+00:00')),c,x))
    raw_reports.sort(key=lambda z:z[0],reverse=True)
    enrich_keys={(c['symbol'],dt.date().isoformat()) for dt,c,x in raw_reports[:int(cfg.get('earnings_enrich_recent_reports',18))]}

    for dt,c,x in raw_reports:
        rdate=dt.date().isoformat(); key=(c['symbol'],rdate)
        prior=old_map.get(key,{})
        item={
            'ticker':c['ticker'],'name':c['name'],'symbol':c['symbol'],'group':c['group'],
            'reportDate':rdate,'reportDateTime':dt.isoformat(),
            'epsActual':x.get('epsActual'),'epsEstimate':x.get('epsEstimate'),'epsSurprisePct':x.get('epsSurprisePct'),
            'revenueActual':prior.get('revenueActual'),'revenueEstimate':prior.get('revenueEstimate'),
            'guidance':prior.get('guidance') or [],'management':prior.get('management') or [],
            'firstReactionPct':prior.get('firstReactionPct'),'nextClosePct':prior.get('nextClosePct'),
            'sources':prior.get('sources') or [],'sourceCount':prior.get('sourceCount',0)
        }
        if key in enrich_keys:
            arts=_earnings_news_context(c['ticker'],c['name'],rdate)
            joined=' '.join((a.get('title','')+' '+a.get('text','')) for a in arts)
            rev_act,rev_est=_extract_estimate(joined,'revenue')
            eps_act2,eps_est2=_extract_estimate(joined,'eps')
            try:t=yf.Ticker(c['symbol'])
            except Exception:t=None
            if item['revenueActual'] is None and t is not None:item['revenueActual']=_quarterly_revenue_actual(t,dt)
            if rev_act is not None:item['revenueActual']=rev_act
            if rev_est is not None:item['revenueEstimate']=rev_est
            if item['epsActual'] is None and eps_act2 is not None:item['epsActual']=eps_act2
            if item['epsEstimate'] is None and eps_est2 is not None:item['epsEstimate']=eps_est2
            gs=_extract_guidance_rows(joined)
            if gs:item['guidance']=gs
            mb=_management_bullets(joined)
            if mb:item['management']=mb
            fr=_first_reaction(joined)
            if fr is None:
                fr=_extended_reaction_from_prices(c['symbol'],dt)
            if fr is not None:item['firstReactionPct']=fr
            nc=_next_close_reaction(c['symbol'],dt)
            if nc is not None:item['nextClosePct']=nc
            item['sources']=[{'title':a.get('title'),'url':a.get('url')} for a in arts[:5] if a.get('url')]
            item['sourceCount']=len(item['sources'])
        reports.append(item)

    reports.sort(key=lambda x:x.get('reportDateTime',''),reverse=True)
    upcoming.sort(key=lambda x:x.get('date',''))
    payload={'updatedAtUtc':now.isoformat(),'updatedAt':datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d %H:%M 台灣時間'),'reports':reports,'upcoming':upcoming}
    EARNINGS_FILE.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')


# ---------- U.S. macro data: Forex Factory ----------
MACRO_FILE = ROOT / "macro.json"
FF_THIS_WEEK_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT_WEEK_JSON = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

def _ff_fetch(url):
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent":"Mozilla/5.0",
                "Accept":"application/json,text/plain,*/*",
                "Referer":"https://www.forexfactory.com/calendar"
            }
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _ff_dt(row):
    raw = str(row.get("date") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z","+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def _macro_event_key(x):
    return "|".join([
        str(x.get("title") or "").strip().lower(),
        str(x.get("eventTimeUtc") or "").strip(),
        str(x.get("impact") or "").strip().lower(),
    ])

def _impact_zh(impact):
    x = (impact or "").lower()
    if x == "high": return "紅燈"
    if x == "medium": return "橘燈"
    return impact or ""

def _macro_push_state():
    try:
        s = json.loads(PUSH_STATE_FILE.read_text(encoding="utf-8"))
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}

def _save_macro_push_state(state):
    PUSH_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def refresh_macro_data():
    """
    U.S. macro data policy:
      - Start collecting from the moment this version is first run.
      - Do NOT backfill older releases that happened before collection began.
      - Once an eligible USD Medium/High event is collected, keep it permanently
        in macro.json instead of deleting it after the day passes.
      - Upcoming events are added when they become visible in the Forex Factory feed.
      - Actual / Forecast / Previous are refreshed in-place after release.
    """
    now_utc = datetime.now(timezone.utc)
    tw_now = now_utc.astimezone(ZoneInfo("Asia/Taipei"))

    # Load existing persistent macro database.
    old = {}
    try:
        old = json.loads(MACRO_FILE.read_text(encoding="utf-8"))
        if not isinstance(old, dict):
            old = {}
    except Exception:
        old = {}

    collection_started_at = old.get("collectionStartedAtUtc")
    if collection_started_at:
        try:
            start_dt = datetime.fromisoformat(str(collection_started_at).replace("Z","+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            else:
                start_dt = start_dt.astimezone(timezone.utc)
        except Exception:
            start_dt = now_utc
            collection_started_at = now_utc.isoformat()
    else:
        # First run of this version: this is the hard boundary.
        start_dt = now_utc
        collection_started_at = now_utc.isoformat()

    # Keep all previously stored events.
    existing = {}
    for x in (old.get("events") or []):
        if not isinstance(x, dict):
            continue
        k = _macro_event_key(x)
        if k:
            existing[k] = x

    raw = _ff_fetch(FF_THIS_WEEK_JSON)
    raw += _ff_fetch(FF_NEXT_WEEK_JSON)

    for row in raw:
        if str(row.get("country") or row.get("currency") or "").upper() != "USD":
            continue

        impact = str(row.get("impact") or "").strip().title()
        if impact not in ("High", "Medium"):
            continue

        dt = _ff_dt(row)
        if not dt:
            continue

        tw = dt.astimezone(ZoneInfo("Asia/Taipei"))
        event = {
            "title": str(row.get("title") or "").strip(),
            "impact": impact,
            "impactZh": _impact_zh(impact),
            "actual": str(row.get("actual") or "").strip(),
            "forecast": str(row.get("forecast") or "").strip(),
            "previous": str(row.get("previous") or "").strip(),
            "eventTimeUtc": dt.isoformat(),
            "eventTimeTw": tw.isoformat(),
            "dateTw": tw.date().isoformat(),
            "timeTw": tw.strftime("%H:%M"),
            "sourceUrl": "https://www.forexfactory.com/calendar",
        }
        if not event["title"]:
            continue

        k = _macro_event_key(event)

        # Never backfill an event that was already in the past before collection began.
        # Existing events are exempt because they were already collected by this system.
        if k not in existing and dt < start_dt:
            continue

        if k in existing:
            # Refresh released values without losing stored metadata.
            old_event = existing[k]
            old_event.update({
                "title": event["title"],
                "impact": event["impact"],
                "impactZh": event["impactZh"],
                "actual": event["actual"] or old_event.get("actual",""),
                "forecast": event["forecast"] or old_event.get("forecast",""),
                "previous": event["previous"] or old_event.get("previous",""),
                "eventTimeUtc": event["eventTimeUtc"],
                "eventTimeTw": event["eventTimeTw"],
                "dateTw": event["dateTw"],
                "timeTw": event["timeTw"],
                "sourceUrl": event["sourceUrl"],
            })
        else:
            event["capturedAtUtc"] = now_utc.isoformat()
            existing[k] = event

    events = sorted(
        existing.values(),
        key=lambda x: x.get("eventTimeUtc",""),
        reverse=True
    )

    payload = {
        "updatedAt": tw_now.strftime("%Y-%m-%d %H:%M 台灣時間"),
        "collectionStartedAtUtc": collection_started_at,
        "collectionStartedAtTw": start_dt.astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M 台灣時間"),
        "events": events,
        "source": "Forex Factory",
        "sourceUrl": "https://www.forexfactory.com/calendar",
    }
    MACRO_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ----- Pushover -----
    state = _macro_push_state()
    reminder_days = set(state.get("macroReminderDays") or [])
    result_keys = set(state.get("macroResultKeys") or [])

    today = tw_now.date().isoformat()
    today_events = [x for x in events if x.get("dateTw") == today]

    # Taiwan 08:30 reminder, once per day.
    # Only events already captured by the system are included.
    if today_events and tw_now.hour == 8 and tw_now.minute >= 30 and today not in reminder_days:
        lines = []
        for x in sorted(today_events, key=lambda z: z.get("eventTimeUtc","")):
            lines.append(f'{x["timeTw"]}｜{x["impactZh"]}｜{x["title"]}')
        ok, _ = _send_pushover(
            "今日美國重要數據",
            "\n".join(lines[:12]),
            url="https://morris199786.github.io/us-stock-watch/?page=macro"
        )
        if ok:
            reminder_days.add(today)

    # Result push: once when Actual first becomes available.
    for x in today_events:
        if not x.get("actual"):
            continue
        k = _macro_event_key(x) + "|" + x.get("actual","")
        if k in result_keys:
            continue
        msg = (
            f'{x["impactZh"]}｜{x["title"]}\n'
            f'實際 {x.get("actual") or "--"}｜預期 {x.get("forecast") or "--"}'
        )
        if x.get("previous"):
            msg += f'｜前值 {x["previous"]}'
        ok, _ = _send_pushover(
            "美國數據公布",
            msg,
            url="https://morris199786.github.io/us-stock-watch/?page=macro"
        )
        if ok:
            result_keys.add(k)

    state["macroReminderDays"] = sorted(reminder_days)[-90:]
    state["macroResultKeys"] = list(result_keys)[-1000:]
    state["macroUpdatedAt"] = now_utc.isoformat()
    _save_macro_push_state(state)


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


def _fetch_json_url(url, timeout=25):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def _yf_symbol(ticker):
    t = (ticker or "").strip().upper()
    # Yahoo uses dash for common U.S. class-share symbols such as BRK.B -> BRK-B.
    if re.fullmatch(r"[A-Z]{1,6}\.[A-Z]", t):
        t = t.replace(".", "-")
    return t

def _first_px_on_or_after(points, target_date):
    if not points or not target_date:
        return None
    td = target_date.isoformat() if hasattr(target_date, "isoformat") else str(target_date)
    for ds, px in points:
        if ds >= td and px is not None:
            return px
    return None

def _load_full_congress_buys(filers):
    """
    Load each Congress member's member-specific Kadoa history and retain BUY trades
    from 2024 onward. This is the complete-history input for server-side backtests.
    """
    base = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/filer/"
    out = {}
    filers = [f for f in (filers or []) if f.get("id")]

    def one(f):
        fid = f["id"]
        try:
            d = _fetch_json_url(base + urllib.parse.quote(fid) + ".json", timeout=20)
            rows = d.get("trades") or []
            buys = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                td = _parse_date(row.get("transaction_date"))
                fd = _parse_date(row.get("filing_date"))
                if not td or td < CONGRESS_START_DATE:
                    continue
                action, _ = _trade_action(row.get("transaction_type"))
                if action != "buy":
                    continue
                ticker = (row.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                buys.append({
                    "ticker": ticker,
                    "transactionDate": td,
                    "filedDate": fd,
                    "daysToFile": row.get("days_to_file"),
                    "ret30Source": safe_float(row.get("ret_30d")),
                })
            return fid, buys
        except Exception:
            return fid, []

    with ThreadPoolExecutor(max_workers=18) as ex:
        futs = [ex.submit(one, f) for f in filers]
        for fut in as_completed(futs):
            fid, buys = fut.result()
            out[fid] = buys
    return out

def _download_congress_prices(tickers):
    """
    Daily adjusted stock history used only by GitHub Actions.
    Moving this calculation off the browser avoids Safari/CORS failures.
    """
    tickers = sorted({_yf_symbol(t) for t in tickers if _yf_symbol(t)})
    if not tickers:
        return {}

    start = (CONGRESS_START_DATE - timedelta(days=10)).isoformat()
    end = (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()
    out = {}

    def one(t):
        try:
            h = yf.Ticker(t).history(
                start=start,
                end=end,
                interval="1d",
                auto_adjust=True,
                actions=False,
                timeout=18
            )
            pts = []
            if h is not None and len(h):
                for idx, val in h["Close"].items():
                    px = safe_float(val)
                    if px is None or px <= 0:
                        continue
                    try:
                        ds = idx.date().isoformat()
                    except Exception:
                        ds = str(idx)[:10]
                    pts.append((ds, px))
            return t, pts
        except Exception:
            return t, []

    with ThreadPoolExecutor(max_workers=14) as ex:
        futs = [ex.submit(one, t) for t in tickers]
        for fut in as_completed(futs):
            t, pts = fut.result()
            out[t] = pts
    return out

def _avg(vals):
    return (sum(vals) / len(vals)) if vals else None

def _win(vals):
    return (sum(1 for v in vals if v > 0) / len(vals) * 100) if vals else None

def _compute_full_member_backtests(filers):
    """
    Computes two neutral descriptive datasets:
      - filing-date backtest: 7/30/90/180 days after public filing
      - transaction-date performance: 30/60 days after reported purchase

    Returns complete per-member statistics. Results are not ranked.
    """
    buys_by_filer = _load_full_congress_buys(filers)
    all_tickers = set()
    for buys in buys_by_filer.values():
        for x in buys:
            all_tickers.add(x["ticker"])

    price_map = _download_congress_prices(all_tickers)
    filer_map = {f.get("id"): f for f in (filers or [])}

    filing_bt = {}
    performance = []

    for fid, buys in buys_by_filer.items():
        f = filer_map.get(fid) or {}
        name = f.get("name") or fid
        chamber = f.get("chamber") or ""
        party = f.get("party")
        state = f.get("state")

        file_vals = {7: [], 30: [], 90: [], 180: []}
        tx_vals = {30: [], 60: []}
        lags = []
        usable_filing = 0

        for x in buys:
            ticker = x["ticker"]
            pts = price_map.get(_yf_symbol(ticker), [])
            td = x["transactionDate"]
            fd = x.get("filedDate")

            # Transaction-date performance
            tx0 = _first_px_on_or_after(pts, td)
            if tx0 not in (None, 0):
                for h in (30, 60):
                    px = _first_px_on_or_after(pts, td + timedelta(days=h))
                    if px is not None:
                        tx_vals[h].append((px / tx0 - 1) * 100)
            elif x.get("ret30Source") is not None:
                tx_vals[30].append(x["ret30Source"])

            # Filing-date "followable" backtest
            if fd:
                f0 = _first_px_on_or_after(pts, fd)
                if f0 not in (None, 0):
                    usable_filing += 1
                    for h in (7, 30, 90, 180):
                        px = _first_px_on_or_after(pts, fd + timedelta(days=h))
                        if px is not None:
                            file_vals[h].append((px / f0 - 1) * 100)

                lag = x.get("daysToFile")
                try:
                    lag = float(lag)
                    if math.isfinite(lag):
                        lags.append(lag)
                except Exception:
                    if td and fd:
                        lags.append((fd - td).days)

        filing_bt[fid] = {
            "member": name,
            "buyCount": len(buys),
            "usableBuys": usable_filing,
            "avgLagDays": _avg(lags),
            "sample7": len(file_vals[7]),
            "avg7": _avg(file_vals[7]),
            "win7": _win(file_vals[7]),
            "sample30": len(file_vals[30]),
            "avg30": _avg(file_vals[30]),
            "win30": _win(file_vals[30]),
            "sample90": len(file_vals[90]),
            "avg90": _avg(file_vals[90]),
            "win90": _win(file_vals[90]),
            "sample180": len(file_vals[180]),
            "avg180": _avg(file_vals[180]),
            "win180": _win(file_vals[180]),
        }

        performance.append({
            "memberId": fid,
            "member": name,
            "chamber": chamber,
            "party": party,
            "state": state,
            "buyCount": len(buys),
            "sample30": len(tx_vals[30]),
            "avg30": _avg(tx_vals[30]),
            "win30": _win(tx_vals[30]),
            "sample60": len(tx_vals[60]),
            "avg60": _avg(tx_vals[60]),
            "win60": _win(tx_vals[60]),
        })

    performance.sort(key=lambda x: (x.get("member") or "").lower())
    return filing_bt, performance


def _congress_event_key(x):
    """
    Stable key for one disclosed trade.
    Used to detect newly published records between 5-minute scans.
    """
    return "|".join([
        (x.get("memberId") or x.get("member") or "").strip().lower(),
        (x.get("ticker") or "").strip().upper(),
        (x.get("action") or "").strip().lower(),
        (x.get("transactionDate") or "").strip(),
        (x.get("amountRange") or "").strip(),
        (x.get("owner") or "").strip(),
        (x.get("filingId") or "").strip(),
    ])

def _push_new_congress_trades(new_items):
    """
    Push only genuinely NEW public disclosures discovered in this scan.
    No historical backfill. Very old/corrected records are kept on the website
    but not pushed to the phone.
    """
    if not new_items:
        return []

    today = datetime.now(timezone.utc).date()
    fresh = []
    for x in new_items:
        fd = _parse_date(x.get("filedDate"))
        # Public-disclosure monitor: allow a small source-ingestion delay.
        if fd and (today - fd).days <= 3:
            fresh.append(x)

    # Oldest -> newest so phone notifications read naturally.
    fresh.sort(key=lambda x: (
        x.get("filedDate") or "",
        x.get("transactionDate") or "",
        x.get("member") or ""
    ))

    sent = []
    for x in fresh[:8]:
        member = x.get("member") or "國會議員"
        ticker = x.get("ticker") or "N/A"
        action = x.get("actionZh") or x.get("action") or "交易"
        amount = x.get("amountRange") or "金額未標示"
        td = x.get("transactionDate") or "--"
        fd = x.get("filedDate") or "--"
        lag = x.get("disclosureLagDays")
        lag_text = f"{lag} 天" if lag is not None else "--"

        title = f"{member}｜國會新申報"
        message = (
            f"{action} {ticker}｜{amount}\n"
            f"交易日 {td}｜申報日 {fd}｜延遲 {lag_text}"
        )

        congress_key = _congress_event_key(x)
        deep_url = (
            "https://morris199786.github.io/us-stock-watch/?congress="
            + urllib.parse.quote(congress_key, safe="")
        )

        ok, _ = _send_pushover(
            title,
            message,
            url=deep_url
        )
        if ok:
            sent.append({
                "member": member,
                "ticker": ticker,
                "action": x.get("action"),
                "filedDate": fd,
                "transactionDate": td,
            })

    return sent

def refresh_congress_data():
    today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
    try:
        previous = json.loads(CONGRESS_FILE.read_text(encoding="utf-8"))
        # Always poll the latest public Congress disclosures every run.
        # Heavy historical backtests are reused unless genuinely new trades appear.
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

    previous_items = previous.get("items", []) if isinstance(previous, dict) else []
    previous_keys = {
        _congress_event_key(x)
        for x in previous_items
        if isinstance(x, dict)
    }
    current_keys = {_congress_event_key(x) for x in items}

    previous_is_live_schema = (
        isinstance(previous, dict)
        and previous.get("schemaVersion") in (4, 5)
        and bool(previous_items)
    )

    # First migration only seeds the state; it must NOT backfill old Congress alerts.
    new_disclosures = []
    if previous_is_live_schema:
        new_disclosures = [
            x for x in items
            if _congress_event_key(x) not in previous_keys
        ]

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

    # Complete-history backtests are expensive.
    # Recompute only when:
    #   1) migrating/initializing this schema, or
    #   2) a genuinely new disclosed trade appears.
    should_recompute_backtests = (
        previous.get("schemaVersion") != 5
        or not isinstance(previous.get("filerBacktests"), dict)
        or not isinstance(previous.get("memberPerformance"), list)
        or bool(new_disclosures)
    )

    if should_recompute_backtests:
        try:
            filer_backtests, member_performance = _compute_full_member_backtests(filers)
        except Exception:
            filer_backtests = previous.get("filerBacktests", {}) if isinstance(previous, dict) else {}
            member_performance = previous.get("memberPerformance", []) if isinstance(previous, dict) else []
    else:
        filer_backtests = previous.get("filerBacktests", {}) if isinstance(previous, dict) else {}
        member_performance = previous.get("memberPerformance", []) if isinstance(previous, dict) else []

    # Congress alerts are independent from news Tier 1 alerts.
    congress_pushes = _push_new_congress_trades(new_disclosures) if previous_is_live_schema else []

    payload = {
        "schemaVersion": 5,
        "updatedAt": datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M 台灣時間"),
        "updatedDate": today,
        "historyStart": "2024-01-01",
        "source": "Kadoa Congress Trading Monitor open dataset",
        "sourceUrl": "https://github.com/kadoa-org/congress-trading-monitor",
        "sourceNote": "Normalized from official House Clerk and Senate financial disclosure filings",
        "backtestBasis": "server_side_transaction_and_filing_date",
        "backtestNote": "GitHub Actions 端計算：申報公開日後 7/30/90/180 日可跟單回測，以及交易日後 30/60 日績效統計。期權交易使用標的股票報酬，不代表期權本身損益",
        "monitor": {
            "mode": "every_run_new_disclosure_check",
            "newDisclosuresThisRun": len(new_disclosures),
            "pushesSentThisRun": len(congress_pushes),
            "backtestsRecomputedThisRun": bool(should_recompute_backtests)
        },
        "summary": {
            "recent7": recent7,
            "buys7": buys7,
            "sells7": sells7,
            "members": len(filers) if filers else len(members),
            "total": len(items)
        },
        "filers": filers,
        "memberBacktests": member_backtests,
        "filerBacktests": filer_backtests,
        "memberPerformance": member_performance,
        "items": items
    }
    CONGRESS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

refresh_earnings_data(broker_scan_candidates)
refresh_macro_data()
refresh_congress_data()

tw = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
(ROOT / "data.json").write_text(
    json.dumps({"updatedAt": tw + " 台灣時間", "mode": "auto", "groups": groups},
               ensure_ascii=False, indent=2),
    encoding="utf-8"
)
news = _clean_cached_news(news)
history_db = _clean_cached_news(history_db)

(ROOT / "news.json").write_text(
    json.dumps({"updatedAt": tw + " 台灣時間", "historyUpdatedDate": old_history_date, "historyLastAttemptAt": old_history_last_attempt, "historyItemCount": len(history_db), "top10Date": str(market_day_et), "top10Count": len(top10), "items": news, "historyItems": history_db},
               ensure_ascii=False, indent=2),
    encoding="utf-8"
)
print(f"updated {len(groups)} groups, {len(news)} news items + earnings")
