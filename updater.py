from pathlib import Path
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parent
UPDATER = ROOT / "updater.py"
INDEX = ROOT / "index.html"
CONFIG = ROOT / "config.json"
BASE_COMMIT = "4f47374be404d3ab33726e52de47d85085cb1b91"

def fetch_base():
    subprocess.run(
        ["git", "fetch", "origin", BASE_COMMIT, "--depth=1"],
        cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    r = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:updater.py"],
        cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return r.stdout

def replace_once(s, old, new, label):
    if old not in s:
        raise RuntimeError(f"{label}: old block not found")
    return s.replace(old, new, 1)

def patch_updater(s):
    s = replace_once(
        s,
        "    # Exact company phrase.\n    if company and company in low:\n        return True",
        "    # Exact company phrase with token boundaries; Meta must not match Metallurgical.\n"
        "    if company and re.search(rf\"(?<![a-z0-9]){re.escape(company)}(?![a-z0-9])\", low):\n"
        "        return True",
        "ticker-boundary"
    )

    old_action = (
        '        action = ""\n'
        '        if any(k in low for k in ["raised","raises","boosted","lifted","increased","hiked"]):\n'
        '            action = "上修"\n'
        '        elif any(k in low for k in ["cut","cuts","lowered","reduced"]):\n'
        '            action = "下修"\n'
        '        elif any(k in low for k in ["upgraded","upgrade"]):\n'
        '            action = "升評"\n'
        '        elif any(k in low for k in ["downgraded","downgrade"]):\n'
        '            action = "降評"\n'
        '        elif any(k in low for k in ["maintained","maintains","reiterated","reiterates"]):\n'
        '            action = "重申"'
    )
    new_action = (
        '        action = ""\n'
        '        target_up = bool(re.search(\n'
        '            r"(?:price target|target price|target).{0,35}\\\\b(?:raised|raises|increased|boosted|lifted|hiked)\\\\b|"\n'
        '            r"\\\\b(?:raised|raises|increased|boosted|lifted|hiked)\\\\b.{0,35}(?:price target|target price|target)",\n'
        '            low, re.I\n'
        '        ))\n'
        '        target_down = bool(re.search(\n'
        '            r"(?:price target|target price|target).{0,35}\\\\b(?:cut|cuts|lowered|reduced)\\\\b|"\n'
        '            r"\\\\b(?:cut|cuts|lowered|reduced)\\\\b.{0,35}(?:price target|target price|target)",\n'
        '            low, re.I\n'
        '        ))\n'
        '        if target_down:\n'
        '            action = "下修"\n'
        '        elif target_up:\n'
        '            action = "上修"\n'
        '        elif old_target and new_target:\n'
        '            try:\n'
        '                ov = float(str(old_target).replace(",",""))\n'
        '                nv = float(str(new_target).replace(",",""))\n'
        '                if nv > ov:\n'
        '                    action = "上修"\n'
        '                elif nv < ov:\n'
        '                    action = "下修"\n'
        '            except Exception:\n'
        '                pass\n'
        '        if not action:\n'
        '            if re.search(r"\\\\bupgrad(?:e|ed|es|ing)\\\\b", low):\n'
        '                action = "升評"\n'
        '            elif re.search(r"\\\\bdowngrad(?:e|ed|es|ing)\\\\b", low):\n'
        '                action = "降評"\n'
        '            elif any(k in low for k in ["maintained","maintains","reiterated","reiterates"]):\n'
        '                action = "重申"'
    )
    s = replace_once(s, old_action, new_action, "analyst-action")

    start = s.find("def _extract_estimate(text, metric):")
    end = s.find("\ndef _extract_guidance_rows(text):", start)
    if start < 0 or end < 0:
        raise RuntimeError("earnings parser block not found")

    new_extract = "\n".join([
        "def _extract_estimate(text, metric):",
        "    t = re.sub(r'\\\\s+', ' ', text or '')",
        "    if metric == 'revenue':",
        "        pats = [",
        r"            r'(?:revenue|sales)\s+(?:of\s+)?\$?([\d,.]+\s*[TBMK]?).{0,90}?(?:estimate|est\.?|consensus|expected|analysts expected|wall street expected)\s+(?:of\s+)?\$?([\d,.]+\s*[TBMK]?)',",
        r"            r'(?:revenue|sales).{0,55}?\$?([\d,.]+\s*[TBMK]?).{0,55}?(?:vs\.?|versus)\s+\$?([\d,.]+\s*[TBMK]?)',",
        r"            r'\$?([\d,.]+\s*[TBMK]?)\s+(?:in\s+)?(?:revenue|sales).{0,80}?(?:estimate|consensus|expected).{0,25}?\$?([\d,.]+\s*[TBMK]?)',",
        "        ]",
        "    else:",
        "        pats = [",
        r"            r'(?:adjusted\s+)?EPS\s+(?:of\s+)?\$?([\d.]+).{0,80}?(?:estimate|est\.?|consensus|expected|analysts expected)\s+(?:of\s+)?\$?([\d.]+)',",
        r"            r'(?:adjusted\s+)?EPS.{0,35}?\$?([\d.]+).{0,40}?(?:vs\.?|versus)\s+\$?([\d.]+)',",
        "        ]",
        "    for p in pats:",
        "        m = re.search(p, t, re.I)",
        "        if not m:",
        "            continue",
        "        raw_a, raw_e = m.group(1).strip(), m.group(2).strip()",
        "        a, e = _num_with_unit(raw_a), _num_with_unit(raw_e)",
        "        if a is None or e is None or e == 0:",
        "            continue",
        "        if metric == 'revenue':",
        r"            scaled_a = bool(re.search(r'[TBMK]\b', raw_a, re.I)) or abs(a) >= 1_000_000",
        r"            scaled_e = bool(re.search(r'[TBMK]\b', raw_e, re.I)) or abs(e) >= 1_000_000",
        "            if not scaled_a or not scaled_e:",
        "                continue",
        "            if abs(a) < 1_000_000 or abs(e) < 1_000_000:",
        "                continue",
        "            if abs((a / e - 1) * 100) > 50:",
        "                continue",
        "        return a, e",
        "    return None, None",
        ""
    ])
    s = s[:start] + new_extract + s[end:]

    s = replace_once(
        s,
        "            if 15<=diff<=120:\n                candidates.append((diff,safe_float(q.loc[rows[0],col])))",
        "            if 0<=diff<=120:\n                candidates.append((diff,safe_float(q.loc[rows[0],col])))",
        "quarter-date-window"
    )

    s = s.replace(
        "if old.get('parserVersion') == 3 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',30))):",
        "if old.get('parserVersion') == 4 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',30))):",
        1
    )

    s = replace_once(
        s,
        "    old['reports'] = clean_old_reports\n\n    old_map={(x.get('symbol'),x.get('reportDate')):x for x in (old.get('reports') or []) if isinstance(x,dict)}",
        "    old['reports'] = clean_old_reports\n    same_parser = old.get('parserVersion') == 4\n\n"
        "    old_map={(x.get('symbol'),x.get('reportDate')):x for x in (old.get('reports') or []) if isinstance(x,dict)}",
        "parser-cache-version"
    )

    s = replace_once(
        s,
        "            'revenueActual':prior.get('revenueActual'),'revenueEstimate':prior.get('revenueEstimate'),",
        "            'revenueActual':prior.get('revenueActual') if same_parser else None,\n"
        "            'revenueEstimate':prior.get('revenueEstimate') if same_parser else None,",
        "drop-stale-revenue"
    )

    old_join = (
        "            joined=' '.join((a.get('title','')+' '+a.get('text','')) for a in arts)\n"
        "            body_joined=' '.join(a.get('text','') for a in arts if a.get('hasBody') and a.get('text'))\n"
        "            rev_act,rev_est=_extract_estimate(joined,'revenue')\n"
        "            eps_act2,eps_est2=_extract_estimate(joined,'eps')\n"
        "            try:t=yf.Ticker(c['symbol'])\n"
        "            except Exception:t=None\n"
        "            if item['revenueActual'] is None and t is not None:item['revenueActual']=_quarterly_revenue_actual(t,dt)\n"
        "            if rev_act is not None:item['revenueActual']=rev_act\n"
        "            if rev_est is not None:item['revenueEstimate']=rev_est\n"
        "            if item['epsActual'] is None and eps_act2 is not None:item['epsActual']=eps_act2\n"
        "            if item['epsEstimate'] is None and eps_est2 is not None:item['epsEstimate']=eps_est2"
    )
    new_join = (
        "            joined=' '.join((a.get('title','')+' '+a.get('text','')) for a in arts)\n"
        "            body_joined=' '.join(a.get('text','') for a in arts if a.get('hasBody') and a.get('text'))\n"
        "            rev_act = rev_est = None\n"
        "            eps_act2 = eps_est2 = None\n"
        "            for a in arts:\n"
        "                one = (a.get('title','') + ' ' + a.get('text','')).strip()\n"
        "                if rev_act is None or rev_est is None:\n"
        "                    ra, re_ = _extract_estimate(one, 'revenue')\n"
        "                    if ra is not None and re_ is not None:\n"
        "                        rev_act, rev_est = ra, re_\n"
        "                if eps_act2 is None or eps_est2 is None:\n"
        "                    ea, ee = _extract_estimate(one, 'eps')\n"
        "                    if ea is not None and ee is not None:\n"
        "                        eps_act2, eps_est2 = ea, ee\n"
        "                if rev_act is not None and rev_est is not None and eps_act2 is not None and eps_est2 is not None:\n"
        "                    break\n"
        "            try:t=yf.Ticker(c['symbol'])\n"
        "            except Exception:t=None\n"
        "            yf_rev = _quarterly_revenue_actual(t,dt) if t is not None else None\n"
        "            if yf_rev is not None:item['revenueActual']=yf_rev\n"
        "            if rev_act is not None:\n"
        "                if item['revenueActual'] is None:item['revenueActual']=rev_act\n"
        "                elif item['revenueActual'] and abs(rev_act/item['revenueActual']-1)<=0.15:item['revenueActual']=rev_act\n"
        "            if rev_est is not None:\n"
        "                anchor=item.get('revenueActual')\n"
        "                if anchor is None or (anchor and abs(rev_est/anchor-1)<=0.50):item['revenueEstimate']=rev_est\n"
        "            if item['epsActual'] is None and eps_act2 is not None:item['epsActual']=eps_act2\n"
        "            if item['epsEstimate'] is None and eps_est2 is not None:item['epsEstimate']=eps_est2"
    )
    s = replace_once(s, old_join, new_join, "per-source-parsing")

    s = s.replace(
        "payload={'parserVersion':3,'updatedAtUtc':now.isoformat()",
        "payload={'parserVersion':4,'updatedAtUtc':now.isoformat()",
        1
    )

    return s

def patch_index():
    if not INDEX.exists():
        return
    s = INDEX.read_text(encoding="utf-8")
    s = s.replace(
        'function metricStatus(a,e){if(a==null||e==null||Number(e)===0)return{txt:"--",cl:"inline"};const d=(Number(a)/Number(e)-1)*100;return{txt:(d>=0?"Beat +":"Miss ")+d.toFixed(1)+"%",cl:d>=0?"beat":"miss"}}',
        'function metricStatus(a,e,maxAbs=500){if(a==null||e==null||Number(e)===0)return{txt:"--",cl:"inline",valid:false};const d=(Number(a)/Number(e)-1)*100;if(!Number.isFinite(d)||Math.abs(d)>maxAbs)return{txt:"--",cl:"inline",valid:false};return{txt:(d>=0?"Beat +":"Miss ")+d.toFixed(1)+"%",cl:d>=0?"beat":"miss",valid:true}}',
        1
    )
    s = s.replace(
        '   const rev=metricStatus(x.revenueActual,x.revenueEstimate),eps=metricStatus(x.epsActual,x.epsEstimate);',
        '   const rev=metricStatus(x.revenueActual,x.revenueEstimate,50),eps=metricStatus(x.epsActual,x.epsEstimate,500);',
        1
    )
    s = s.replace(
        "     ['本季營收',moneyFmt(x.revenueActual),moneyFmt(x.revenueEstimate),rev],",
        "     ['本季營收',moneyFmt(x.revenueActual),rev.valid?moneyFmt(x.revenueEstimate):'--',rev],",
        1
    )
    INDEX.write_text(s, encoding="utf-8")

def patch_config():
    if not CONFIG.exists():
        return
    d = json.loads(CONFIG.read_text(encoding="utf-8"))
    d["earnings_parser_version"] = 4
    CONFIG.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def main():
    base = fetch_base()
    patched = patch_updater(base)
    compile(patched, "updater.py", "exec")
    UPDATER.write_text(patched, encoding="utf-8")
    patch_index()
    patch_config()
    code = compile(patched, str(UPDATER), "exec")
    g = {"__name__":"__main__", "__file__":str(UPDATER), "__package__":None}
    exec(code, g, g)

if __name__ == "__main__":
    main()
