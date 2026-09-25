from pathlib import Path
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parent
UPDATER = ROOT / "updater.py"
CONFIG = ROOT / "config.json"

BASE_COMMIT = "8be0b0942f7769ca76af03f9bd40913eb2e8a3e5"
TARGET_VERSION = 6


def fetch_verified_full_updater():
    subprocess.run(
        ["git", "fetch", "origin", BASE_COMMIT, "--depth=1"],
        cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    r = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:updater.py"],
        cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    src = r.stdout
    if len(src) < 150_000:
        raise RuntimeError(f"verified updater unexpectedly small: {len(src)} bytes")
    return src


def must_replace(src, old, new, label):
    if old not in src:
        raise RuntimeError(f"{label}: expected block not found; aborting safely")
    return src.replace(old, new, 1)


def patch_money_parser(src):
    old = '''def _num_with_unit(s):
    if s is None:return None
    s=str(s).replace(',','').strip()
    m=re.match(r'\\$?([+-]?\\d+(?:\\.\\d+)?)\\s*([TtBbMmKk]?)',s)
    if not m:return None
    v=float(m.group(1)); u=m.group(2).lower()
    return v*({'t':1e12,'b':1e9,'m':1e6,'k':1e3}.get(u,1))'''

    new = '''def _num_with_unit(s):
    if s is None:return None
    s=html_lib.unescape(str(s)).replace(',','').strip()
    m=re.match(
        r'\\$?\\s*([+-]?\\d+(?:\\.\\d+)?)\\s*'
        r'(trillion|billion|million|thousand|[TtBbMmKk])?',
        s, re.I
    )
    if not m:return None
    v=float(m.group(1))
    u=(m.group(2) or '').lower()
    mult={
        'trillion':1e12,'t':1e12,
        'billion':1e9,'b':1e9,
        'million':1e6,'m':1e6,
        'thousand':1e3,'k':1e3,
    }.get(u,1)
    return v*mult'''

    return must_replace(src, old, new, "money parser")


def patch_estimate_parser(src):
    start = src.find("def _extract_estimate(text, metric):")
    end = src.find("\ndef _extract_guidance_rows(text):", start)
    if start < 0 or end < 0:
        raise RuntimeError("estimate parser block not found")

    new_func = r'''def _extract_estimate(text, metric):
    """
    Extract actual vs consensus from ONE source at a time.

    Revenue accepts $19.35B / $19.35 billion / $582.3 million and rejects
    unscaled values such as $2.00, so EPS cannot leak into revenue consensus.
    """
    t = html_lib.unescape(re.sub(r'\s+', ' ', text or ''))
    money = r'\$?\s*[\d,.]+(?:\.\d+)?\s*(?:trillion|billion|million|thousand|[TBMK])'

    if metric == 'revenue':
        patterns = [
            rf'(?:revenue|sales).{{0,90}}?({money}).{{0,160}}?'
            rf'(?:analysts?[’\' ]*(?:average\s+)?(?:estimate|expectation)s?|'
            rf'wall street[’\' ]*(?:estimate|expectation)s?|'
            rf'consensus|estimate|expected).{{0,70}}?({money})',

            rf'({money}).{{0,35}}?(?:revenue|sales).{{0,160}}?'
            rf'(?:estimate|consensus|expected|expectation).{{0,70}}?({money})',

            rf'(?:analysts?[’\' ]*(?:average\s+)?(?:estimate|expectation)s?|'
            rf'wall street[’\' ]*(?:estimate|expectation)s?|'
            rf'consensus|estimate|expected).{{0,70}}?({money}).{{0,160}}?'
            rf'(?:revenue|sales).{{0,90}}?({money})',
        ]

        for idx,p in enumerate(patterns):
            m=re.search(p,t,re.I)
            if not m:
                continue
            if idx < 2:
                raw_a,raw_e=m.group(1),m.group(2)
            else:
                raw_e,raw_a=m.group(1),m.group(2)

            a,e=_num_with_unit(raw_a),_num_with_unit(raw_e)
            if a is None or e is None or a<=0 or e<=0:
                continue
            if a < 1_000_000 or e < 1_000_000:
                continue
            if abs(a/e-1) > 0.50:
                continue
            return a,e
        return None,None

    patterns = [
        r'(?:adjusted\s+)?EPS\s+(?:of\s+)?\$?([+-]?\d+(?:\.\d+)?).{0,100}?'
        r'(?:estimate|est\.?|consensus|expected|analysts expected)\s+(?:of\s+)?'
        r'\$?([+-]?\d+(?:\.\d+)?)',
        r'(?:adjusted\s+)?EPS.{0,45}?\$?([+-]?\d+(?:\.\d+)?).{0,60}?'
        r'(?:vs\.?|versus)\s+\$?([+-]?\d+(?:\.\d+)?)',
    ]
    for p in patterns:
        m=re.search(p,t,re.I)
        if not m:
            continue
        try:
            a=float(m.group(1));e=float(m.group(2))
        except Exception:
            continue
        if abs(a)>1000 or abs(e)>1000:
            continue
        return a,e
    return None,None
'''
    return src[:start] + new_func + src[end:]


def patch_marketbeat_parser(src):
    start = src.find("def _marketbeat_revenue_pair(ticker, report_dt):")
    end = src.find("\ndef _earnings_dates_one(ticker,name,symbol,group):", start)
    if start < 0 or end < 0:
        raise RuntimeError("MarketBeat parser block not found")

    new_func = r'''def _marketbeat_revenue_pair(ticker, report_dt):
    """
    Historical revenue estimate + actual from the exact MarketBeat report-date row.

    Failure is non-fatal because Reuters/news single-source parsing is the next
    fallback.
    """
    try:
        et=report_dt.astimezone(ZoneInfo("America/New_York"))
        d=et.date()
        date_tokens=[
            f"{d.month}/{d.day}/{d.year}",
            f"{d.month:02d}/{d.day:02d}/{d.year}",
        ]
        ticker_path=urllib.parse.quote((ticker or "").upper(),safe=".-")

        def parse_money(x):
            v=_num_with_unit(x)
            return v if v is not None and v>=1_000_000 else None

        for exch in ["NYSE","NASDAQ","AMEX"]:
            url=f"https://www.marketbeat.com/stocks/{exch}/{ticker_path}/earnings/"
            try:
                req=urllib.request.Request(
                    url,
                    headers={
                        "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                     "(KHTML, like Gecko) Chrome/126 Safari/537.36",
                        "Accept":"text/html,application/xhtml+xml",
                        "Accept-Language":"en-US,en;q=0.9",
                        "Cache-Control":"no-cache",
                    },
                )
                with urllib.request.urlopen(req,timeout=12) as r:
                    raw=r.read().decode("utf-8","ignore")
                if len(raw)<3000:
                    continue
            except Exception:
                continue

            txt=html_lib.unescape(raw)
            txt=re.sub(r"<script\b[^>]*>.*?</script>"," ",txt,flags=re.I|re.S)
            txt=re.sub(r"<style\b[^>]*>.*?</style>"," ",txt,flags=re.I|re.S)
            txt=re.sub(r"</(?:td|th|tr|li|div|p)>"," | ",txt,flags=re.I)
            txt=re.sub(r"<[^>]+>"," ",txt)
            txt=re.sub(r"\s+"," ",txt).strip()

            for token in date_tokens:
                pos=0
                while True:
                    idx=txt.find(token,pos)
                    if idx<0:
                        break
                    pos=idx+len(token)
                    seg=txt[idx:idx+750]

                    scaled=[]
                    for m in re.finditer(
                        r'\$?\s*[\d,.]+\s*(?:trillion|billion|million|thousand|[TBMK])\b',
                        seg,re.I
                    ):
                        v=parse_money(m.group(0))
                        if v:
                            scaled.append(v)

                    if len(scaled)>=2:
                        for i in range(len(scaled)-1):
                            est,act=scaled[i],scaled[i+1]
                            if est and act and abs(act/est-1)<=0.50:
                                return act,est

        return None,None
    except Exception:
        return None,None
'''
    return src[:start] + new_func + src[end:]


def patch_revenue_consensus_search(src):
    old = '''        f'("{ticker}" OR "{name}") earnings {report_date} site:bloomberg.com',
    ]'''
    new = '''        f'("{ticker}" OR "{name}") earnings {report_date} site:bloomberg.com',
        f'("{ticker}" OR "{name}") revenue "analysts expected" {report_date}',
        f'("{ticker}" OR "{name}") revenue "consensus" {report_date}',
    ]'''
    return must_replace(src, old, new, "earnings search queries")


def patch_guidance_safety(src):
    old = '''            gs=_extract_guidance_rows(body_joined or joined)
            # Safety: only keep short, single-thesis guidance rows.
            gs=[z for z in gs if len((z.get('companyGuide') or '')) <= 430 and (z.get('companyGuide') or '').count(' - ') < 2]
            if gs:item['guidance']=gs'''

    new = '''            gs=_extract_guidance_rows(body_joined or joined)
            clean_gs=[]
            for z in gs:
                g=(z.get('companyGuide') or '').strip()
                gl=g.lower()
                if not g or len(g)>360:
                    continue
                if g.count(' - ')>=1:
                    continue
                if re.search(r'\\b(?:finance\\.)?[a-z0-9-]+\\.(?:com|net|org|io)\\b',gl):
                    continue
                if '[' in g or ']' in g:
                    continue
                if sum(gl.count(k) for k in ['reuters','bloomberg','yahoo','biggo','seeking alpha'])>=2:
                    continue
                clean_gs.append(z)
            if clean_gs:item['guidance']=clean_gs
            else:item['guidance']=[]'''
    return must_replace(src, old, new, "guidance safety")


def patch_versions(src):
    src = must_replace(
        src,
        "if old.get('parserVersion') == 5 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',30))):",
        "if old.get('parserVersion') == 6 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',30))):",
        "refresh parser version",
    )
    src = must_replace(
        src,
        "same_parser = old.get('parserVersion') == 5",
        "same_parser = old.get('parserVersion') == 6",
        "cache parser version",
    )
    src = must_replace(
        src,
        "payload={'parserVersion':5,'updatedAtUtc':now.isoformat()",
        "payload={'parserVersion':6,'updatedAtUtc':now.isoformat()",
        "payload parser version",
    )
    return src


def patch_config():
    if not CONFIG.exists():
        return
    try:
        d=json.loads(CONFIG.read_text(encoding="utf-8"))
        d["earnings_parser_version"]=TARGET_VERSION
        CONFIG.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    except Exception:
        pass


def self_test():
    def n(s):
        s=str(s).replace(",","").strip()
        m=re.match(
            r'\$?\s*([+-]?\d+(?:\.\d+)?)\s*'
            r'(trillion|billion|million|thousand|[TBMK])?',
            s,re.I
        )
        if not m:return None
        v=float(m.group(1));u=(m.group(2) or '').lower()
        return v*{
            'trillion':1e12,'t':1e12,
            'billion':1e9,'b':1e9,
            'million':1e6,'m':1e6,
            'thousand':1e3,'k':1e3,
        }.get(u,1)

    for raw,want in [
        ("$19.35 billion",19.35e9),
        ("$19.13B",19.13e9),
        ("$582.3 million",582.3e6),
        ("$92.27 billion",92.27e9),
    ]:
        got=n(raw)
        assert abs(got/want-1)<1e-9,(raw,got,want)

    assert n("$2.00") < 1_000_000

    for act,est in [
        (19.35e9,19.13e9),
        (96.22e9,92.27e9),
        (60.80e9,60.22e9),
        (90.01e9,87.62e9),
        (11.54e9,11.31e9),
    ]:
        assert abs(act/est-1)<=0.50


def main():
    self_test()

    src=fetch_verified_full_updater()
    src=patch_money_parser(src)
    src=patch_estimate_parser(src)
    src=patch_marketbeat_parser(src)
    src=patch_revenue_consensus_search(src)
    src=patch_guidance_safety(src)
    src=patch_versions(src)

    compile(src,"updater.py","exec")
    if len(src)<150_000:
        raise RuntimeError(f"patched updater unexpectedly small: {len(src)} bytes")

    UPDATER.write_text(src,encoding="utf-8")
    patch_config()

    print(f"Final updater installed: {len(src)} bytes, parserVersion={TARGET_VERSION}")

    code=compile(src,str(UPDATER),"exec")
    g={"__name__":"__main__","__file__":str(UPDATER),"__package__":None}
    exec(code,g,g)


if __name__=="__main__":
    main()
