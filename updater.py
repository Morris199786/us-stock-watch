from pathlib import Path
import json
import re
import urllib.request

ROOT = Path(__file__).resolve().parent
UPDATER = ROOT / "updater.py"
CONFIG = ROOT / "config.json"

BASE_COMMIT = "8efa8819f87eb0ec7026be27c6ad420667e53625"
BASE_URL = f"https://raw.githubusercontent.com/Morris199786/us-stock-watch/{BASE_COMMIT}/updater.py"


def fetch_base():
    req = urllib.request.Request(
        BASE_URL,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read().decode("utf-8")
    if len(data) < 100_000:
        raise RuntimeError(f"base updater too small: {len(data)} bytes")
    return data


def must_replace(s, old, new, label):
    if old not in s:
        raise RuntimeError(f"{label}: old block not found")
    return s.replace(old, new, 1)


def patch_updater(s):
    # Add dedicated MarketBeat revenue-consensus fallback before earnings-date loader.
    anchor = "def _earnings_dates_one(ticker,name,symbol,group):"
    if anchor not in s:
        raise RuntimeError("earnings anchor not found")

    helper = r'''
def _marketbeat_revenue_pair(ticker, report_dt):
    """
    Structured revenue-consensus fallback from MarketBeat earnings history.

    We locate the exact report-date row and only read scaled revenue amounts
    (million/billion/trillion), so EPS values such as $2.00 cannot be mistaken
    for revenue consensus.
    """
    try:
        et = report_dt.astimezone(ZoneInfo("America/New_York"))
        d = et.date()
        date_tokens = [
            f"{d.month}/{d.day}/{d.year}",
            f"{d.month:02d}/{d.day:02d}/{d.year}",
        ]
        ticker_path = urllib.parse.quote((ticker or "").upper(), safe=".-")

        for exch in ["NYSE", "NASDAQ", "AMEX"]:
            url = f"https://www.marketbeat.com/stocks/{exch}/{ticker_path}/earnings/"
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    raw = r.read().decode("utf-8", "ignore")
                if len(raw) < 1000:
                    continue
            except Exception:
                continue

            # Convert HTML to compact text while preserving row order.
            txt = html_lib.unescape(raw)
            txt = re.sub(r"<script\\b[^>]*>.*?</script>", " ", txt, flags=re.I|re.S)
            txt = re.sub(r"<style\\b[^>]*>.*?</style>", " ", txt, flags=re.I|re.S)
            txt = re.sub(r"<[^>]+>", " ", txt)
            txt = re.sub(r"\\s+", " ", txt).strip()

            for token in date_tokens:
                pos = 0
                while True:
                    idx = txt.find(token, pos)
                    if idx < 0:
                        break
                    pos = idx + len(token)

                    # Exact row neighborhood. Revenue Estimate and Actual Revenue
                    # are the first two scaled money values after the EPS fields.
                    seg = txt[idx:idx + 900]
                    vals = []
                    for m in re.finditer(
                        r"\\$?\\s*([\\d,.]+)\\s*(trillion|billion|million|[TBM])\\b",
                        seg,
                        re.I,
                    ):
                        raw_num = m.group(1)
                        unit = m.group(2).lower()
                        mult = {
                            "trillion": 1e12, "t": 1e12,
                            "billion": 1e9, "b": 1e9,
                            "million": 1e6, "m": 1e6,
                        }.get(unit)
                        if not mult:
                            continue
                        try:
                            v = float(raw_num.replace(",", "")) * mult
                        except Exception:
                            continue
                        if v >= 1_000_000:
                            vals.append(v)

                    # The row can occasionally include duplicate rendered values;
                    # test adjacent pairs and accept only a plausible earnings pair.
                    for i in range(len(vals) - 1):
                        est, act = vals[i], vals[i + 1]
                        if not est or not act:
                            continue
                        surprise = (act / est - 1) * 100
                        if abs(surprise) <= 50:
                            return act, est

        return None, None
    except Exception:
        return None, None

'''
    s = s.replace(anchor, helper + anchor, 1)

    # Force earnings rebuild by bumping parser version.
    s = s.replace(
        "if old.get('parserVersion') == 4 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',30))):",
        "if old.get('parserVersion') == 5 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',30))):",
        1,
    )
    s = s.replace(
        "same_parser = old.get('parserVersion') == 4",
        "same_parser = old.get('parserVersion') == 5",
        1,
    )
    s = s.replace(
        "payload={'parserVersion':4,'updatedAtUtc':now.isoformat()",
        "payload={'parserVersion':5,'updatedAtUtc':now.isoformat()",
        1,
    )

    old = """            yf_rev = _quarterly_revenue_actual(t,dt) if t is not None else None
            if yf_rev is not None:item['revenueActual']=yf_rev
            if rev_act is not None:
                if item['revenueActual'] is None:item['revenueActual']=rev_act
                elif item['revenueActual'] and abs(rev_act/item['revenueActual']-1)<=0.15:item['revenueActual']=rev_act
            if rev_est is not None:
                anchor=item.get('revenueActual')
                if anchor is None or (anchor and abs(rev_est/anchor-1)<=0.50):item['revenueEstimate']=rev_est
            if item['epsActual'] is None and eps_act2 is not None:item['epsActual']=eps_act2"""

    new = """            yf_rev = _quarterly_revenue_actual(t,dt) if t is not None else None
            if yf_rev is not None:item['revenueActual']=yf_rev

            # Structured MarketBeat consensus wins over generic news parsing.
            mb_act, mb_est = _marketbeat_revenue_pair(c['ticker'], dt)
            if mb_act is not None:
                if item['revenueActual'] is None:
                    item['revenueActual'] = mb_act
                elif item['revenueActual'] and abs(mb_act/item['revenueActual']-1) <= 0.15:
                    item['revenueActual'] = mb_act
            if mb_est is not None:
                anchor_rev = item.get('revenueActual')
                if anchor_rev is None or (anchor_rev and abs(mb_est/anchor_rev-1) <= 0.50):
                    item['revenueEstimate'] = mb_est

            if rev_act is not None:
                if item['revenueActual'] is None:item['revenueActual']=rev_act
                elif item['revenueActual'] and abs(rev_act/item['revenueActual']-1)<=0.15:item['revenueActual']=rev_act

            # News regex is backup only.
            if item.get('revenueEstimate') is None and rev_est is not None:
                anchor_rev=item.get('revenueActual')
                if anchor_rev is None or (anchor_rev and abs(rev_est/anchor_rev-1)<=0.50):
                    item['revenueEstimate']=rev_est

            if item['epsActual'] is None and eps_act2 is not None:item['epsActual']=eps_act2"""

    s = must_replace(s, old, new, "marketbeat integration")
    return s


def patch_config():
    try:
        d = json.loads(CONFIG.read_text(encoding="utf-8"))
        d["earnings_parser_version"] = 5
        CONFIG.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def self_test():
    act = 19.35e9
    est = 19.13e9
    assert round((act / est - 1) * 100, 1) == 1.2
    assert abs((act / 2.0 - 1) * 100) > 50


def main():
    self_test()
    base = fetch_base()
    patched = patch_updater(base)
    compile(patched, "updater.py", "exec")

    # Permanently restore full updater.py before executing it.
    UPDATER.write_text(patched, encoding="utf-8")
    patch_config()

    print(f"restored full updater.py: {len(patched)} bytes")
    print("earnings parser version: 5")

    code = compile(patched, str(UPDATER), "exec")
    g = {
        "__name__": "__main__",
        "__file__": str(UPDATER),
        "__package__": None,
    }
    exec(code, g, g)


if __name__ == "__main__":
    main()
