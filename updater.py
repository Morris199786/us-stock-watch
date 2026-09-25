from pathlib import Path
import json
import os
import re
import subprocess

ROOT = Path(__file__).resolve().parent
UPDATER = ROOT / "updater.py"
CONFIG = ROOT / "config.json"

BASE_COMMIT = "eda356d757ef63fb86cc4ed000d4f0beb9c26287"
TARGET_VERSION = 7


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
        raise RuntimeError(f"{label}: expected block not found")
    return src.replace(old, new, 1)


def patch_versions(src):
    src = must_replace(
        src,
        "if old.get('parserVersion') == 6 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',30))):",
        "if old.get('parserVersion') == 7 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',0))):",
        "earnings refresh version",
    )
    src = must_replace(
        src,
        "same_parser = old.get('parserVersion') == 6",
        "same_parser = old.get('parserVersion') == 7",
        "earnings cache version",
    )
    src = must_replace(
        src,
        "payload={'parserVersion':6,'updatedAtUtc':now.isoformat()",
        "payload={'parserVersion':7,'updatedAtUtc':now.isoformat()",
        "earnings payload version",
    )
    return src


def patch_latest_quarter_only(src):
    pat = r"for x in past\[:int\(cfg\.get\('earnings_history_per_stock',4\)\)\]:"
    src2, n = re.subn(pat, "for x in past[:1]:", src, count=1)
    if n != 1:
        raise RuntimeError("latest-quarter loop not found")
    src = src2

    old = "enrich_keys={(c['symbol'],dt.date().isoformat()) for dt,c,x in raw_reports[:int(cfg.get('earnings_enrich_recent_reports',18))]}"
    new = "enrich_keys={(c['symbol'],dt.date().isoformat()) for dt,c,x in raw_reports}"
    return must_replace(src, old, new, "latest-quarter enrichment set")


def patch_enrichment_cache(src):
    anchor = "def refresh_earnings_data(targets):"
    helper = r'''def _should_enrich_latest_earnings(prior, report_dt, same_parser):
    """
    Latest-quarter cache policy:
      - parser migration / new quarter: enrich once
      - first 7 days after report: retry if core fields are still missing
      - older same-quarter result: keep checked blanks instead of scraping forever
    """
    if not same_parser or not prior:
        return True
    try:
        age_days = (datetime.now(timezone.utc) - report_dt.astimezone(timezone.utc)).days
    except Exception:
        age_days = 999

    if age_days <= 7:
        return bool(
            prior.get('revenueActual') is None
            or prior.get('revenueEstimate') is None
            or not prior.get('management')
        )

    return not bool(prior.get('earningsCheckedV7'))


'''
    if anchor not in src:
        raise RuntimeError("refresh_earnings_data anchor not found")
    src = src.replace(anchor, helper + anchor, 1)

    src = must_replace(
        src,
        "        if key in enrich_keys:",
        "        if key in enrich_keys and _should_enrich_latest_earnings(prior, dt, same_parser):",
        "earnings cache gate",
    )

    old = '''            item['sources']=[{'title':a.get('title'),'url':a.get('url')} for a in arts[:5] if a.get('url')]
            item['sourceCount']=len(item['sources'])
        reports.append(item)'''
    new = '''            item['sources']=[{'title':a.get('title'),'url':a.get('url')} for a in arts[:5] if a.get('url')]
            item['sourceCount']=len(item['sources'])
            item['earningsCheckedV7']=True
            item['earningsCheckedAtUtc']=datetime.now(timezone.utc).isoformat()
        elif same_parser and prior:
            item['earningsCheckedV7']=prior.get('earningsCheckedV7', False)
            item['earningsCheckedAtUtc']=prior.get('earningsCheckedAtUtc')
        reports.append(item)'''
    return must_replace(src, old, new, "earnings checked marker")


def patch_management_sources(src):
    old = '''            mb=_management_bullets(body_joined)
            if mb:item['management']=mb'''
    new = '''            trusted_mgmt_parts=[]
            for a in arts:
                blob=((a.get('title') or '')+' '+(a.get('url') or '')).lower()
                if any(k in blob for k in [
                    'earnings call','conference call','transcript',
                    'investor relation','investors.','sec.gov',
                    'reuters','bloomberg','investing.com','seekingalpha'
                ]):
                    txt=(a.get('text') or '').strip()
                    if txt:
                        trusted_mgmt_parts.append(txt)
            trusted_mgmt_text=' '.join(trusted_mgmt_parts)
            mb=_management_bullets(trusted_mgmt_text)
            if mb:item['management']=mb
            elif not trusted_mgmt_text:item['management']=[]'''
    return must_replace(src, old, new, "trusted management sources")


def patch_skip_fast_earnings(src):
    old = '''refresh_earnings_data(broker_scan_candidates)
refresh_macro_data()'''
    new = '''if os.getenv("SKIP_EARNINGS", "0") != "1":
    refresh_earnings_data(broker_scan_candidates)
refresh_macro_data()'''
    return must_replace(src, old, new, "fast-run earnings skip")


def patch_fiscal_quarter_label(src):
    anchor = "def refresh_earnings_data(targets):"
    helper = ""
    helper += "def _fiscal_quarter_label(t, report_dt):\n"
    helper += "    try:\n"
    helper += "        q=t.quarterly_financials\n"
    helper += "        if q is None or q.empty:return ''\n"
    helper += "        candidates=[]\n"
    helper += "        for col in q.columns:\n"
    helper += "            try:\n"
    helper += "                qd=col.date() if hasattr(col,'date') else col\n"
    helper += "                diff=(report_dt.date()-qd).days\n"
    helper += "                if 0<=diff<=120:candidates.append((diff,qd))\n"
    helper += "            except Exception:pass\n"
    helper += "        if not candidates:return ''\n"
    helper += "        candidates.sort(key=lambda x:x[0]); qd=candidates[0][1]\n"
    helper += "        a=t.financials\n"
    helper += "        if a is None or a.empty or len(a.columns)==0:return ''\n"
    helper += "        fyes=[]\n"
    helper += "        for col in a.columns:\n"
    helper += "            try:fyes.append(col.date() if hasattr(col,'date') else col)\n"
    helper += "            except Exception:pass\n"
    helper += "        if not fyes:return ''\n"
    helper += "        latest=max(fyes); fm=latest.month; fd=latest.day\n"
    helper += "        from datetime import date as _date\n"
    helper += "        fy_end=_date(qd.year,fm,min(fd,28))\n"
    helper += "        if fy_end<qd:fy_end=_date(qd.year+1,fm,min(fd,28))\n"
    helper += "        months=(fy_end.year-qd.year)*12+(fy_end.month-qd.month)\n"
    helper += "        qnum={9:1,6:2,3:3,0:4}.get(months)\n"
    helper += "        if qnum is None:\n"
    helper += "            nearest=min([0,3,6,9],key=lambda x:abs(x-months))\n"
    helper += "            if abs(nearest-months)>1:return ''\n"
    helper += "            qnum={9:1,6:2,3:3,0:4}[nearest]\n"
    helper += "        return f'Q{qnum} FY{str(fy_end.year)[-2:]}'\n"
    helper += "    except Exception:return ''\n\n"
    if anchor not in src: raise RuntimeError('refresh_earnings_data anchor missing')
    src=src.replace(anchor,helper+anchor,1)
    old="            'sources':prior.get('sources') or [],'sourceCount':prior.get('sourceCount',0)\n        }"
    new="            'sources':prior.get('sources') or [],'sourceCount':prior.get('sourceCount',0),\n            'fiscalQuarterLabel':prior.get('fiscalQuarterLabel') or ''\n        }"
    src=must_replace(src,old,new,'fiscal label field')
    old2="            yf_rev = _quarterly_revenue_actual(t,dt) if t is not None else None\n            if yf_rev is not None:item['revenueActual']=yf_rev"
    new2="            yf_rev = _quarterly_revenue_actual(t,dt) if t is not None else None\n            if yf_rev is not None:item['revenueActual']=yf_rev\n            if t is not None:\n                fq=_fiscal_quarter_label(t,dt)\n                if fq:item['fiscalQuarterLabel']=fq"
    src=must_replace(src,old2,new2,'fiscal label compute')
    return src

def patch_index_for_quarter_label():
    path=ROOT/'index.html'
    if not path.exists():return
    html=path.read_text(encoding='utf-8')
    old="<div class=\"earnMeta\">${escapeHtml(x.group||'')}｜${escapeHtml(x.reportDate||'')}</div>"
    new="<div class=\"earnMeta\">${escapeHtml(x.group||'')}｜${x.fiscalQuarterLabel?escapeHtml(x.fiscalQuarterLabel)+'｜':''}${escapeHtml(x.reportDate||'')}</div>"
    if old in html: html=html.replace(old,new,1)
    elif 'x.fiscalQuarterLabel' not in html: raise RuntimeError('index earnings meta block missing')
    path.write_text(html,encoding='utf-8')


def patch_config():
    d = json.loads(CONFIG.read_text(encoding="utf-8"))
    d["earnings_history_per_stock"] = 1
    d["earnings_enrich_recent_reports"] = 999
    d["earnings_refresh_minutes"] = 0
    d["earnings_parser_version"] = TARGET_VERSION
    CONFIG.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def self_test(src):
    compile(src, "updater.py", "exec")
    checks = [
        "past[:1]",
        "earningsCheckedV7",
        'os.getenv("SKIP_EARNINGS"',
        "trusted_mgmt_parts",
        "payload={'parserVersion':7",
    ]
    for token in checks:
        if token not in src:
            raise RuntimeError(f"self-test missing: {token}")
    if len(src) < 150_000:
        raise RuntimeError(f"patched updater unexpectedly small: {len(src)} bytes")


def main():
    src = fetch_verified_full_updater()
    src = patch_versions(src)
    src = patch_latest_quarter_only(src)
    src = patch_enrichment_cache(src)
    src = patch_management_sources(src)
    src = patch_fiscal_quarter_label(src)
    src = patch_skip_fast_earnings(src)
    self_test(src)

    UPDATER.write_text(src, encoding="utf-8")
    patch_config()
    patch_index_for_quarter_label()

    print(f"Final updater installed: {len(src)} bytes, parserVersion={TARGET_VERSION}")

    code = compile(src, str(UPDATER), "exec")
    g = {"__name__": "__main__", "__file__": str(UPDATER), "__package__": None}
    exec(code, g, g)


if __name__ == "__main__":
    main()
