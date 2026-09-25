from pathlib import Path
import json, os, re, subprocess, textwrap

ROOT = Path(__file__).resolve().parent
UPDATER = ROOT / "updater.py"
CONFIG = ROOT / "config.json"
BASE_COMMIT = "88edee9f5a50df4a2419a937cdb6753d084c6ebd"
TARGET_VERSION = 9

def fetch_full():
    subprocess.run(
        ["git","fetch","origin",BASE_COMMIT,"--depth=1"],
        cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    r=subprocess.run(
        ["git","show",f"{BASE_COMMIT}:updater.py"],
        cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    src=r.stdout
    if len(src)<150000:
        raise RuntimeError(f"verified updater too small: {len(src)}")
    return src

def must_replace(src, old, new, label):
    if old not in src:
        raise RuntimeError(f"{label}: expected block not found")
    return src.replace(old,new,1)

def main():
    src=fetch_full()

    # v8 parser/cache
    src=must_replace(
        src,
        "if old.get('parserVersion') == 7 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',0))):",
        "if old.get('parserVersion') == 9 and now-old_dt<timedelta(minutes=int(cfg.get('earnings_refresh_minutes',0))):",
        "refresh version"
    )
    src=must_replace(src,"same_parser = old.get('parserVersion') == 7","same_parser = old.get('parserVersion') == 9","same parser")
    src=must_replace(src,"payload={'parserVersion':7,'updatedAtUtc':now.isoformat()","payload={'parserVersion':9,'updatedAtUtc':now.isoformat()","payload version")
    src=src.replace("earningsCheckedV7","earningsCheckedV9")

    # More estimate/transcript search coverage.
    src=must_replace(
        src,
        """        f'("{ticker}" OR "{name}") revenue "consensus" {report_date}',
    ]""",
        """        f'("{ticker}" OR "{name}") revenue "consensus" {report_date}',
        f'("{ticker}" OR "{name}") earnings revenue estimate {report_date} site:nasdaq.com',
        f'("{ticker}" OR "{name}") earnings revenue estimate {report_date} site:stockanalysis.com',
        f'("{ticker}" OR "{name}") "earnings call transcript" {report_date} site:seekingalpha.com',
        f'("{ticker}" OR "{name}") "earnings call transcript" {report_date} site:investing.com',
        f'("{ticker}" OR "{name}") "earnings call" {report_date} "investor relations"',
    ]""",
        "extra queries"
    )

    # Insert fiscal-quarter fallbacks before refresh_earnings_data.
    anchor="def refresh_earnings_data(targets):"
    helper=textwrap.dedent(r"""
    def _quarter_from_sources(arts):
        for a in arts or []:
            blob=((a.get('title') or '')+' '+(a.get('text') or ''))
            m=re.search(r'\bQ([1-4])\s*(?:FY|FISCAL\s*YEAR\s*)?([12]\d{3}|\d{2})\b',blob,re.I)
            if m:
                fy=m.group(2)
                if len(fy)==4:
                    fy=fy[-2:]
                return f"Q{m.group(1)} FY{fy}", "source title"
            m=re.search(r'\b(FIRST|SECOND|THIRD|FOURTH)\s+QUARTER\s+(?:FISCAL\s+)?([12]\d{3})\b',blob,re.I)
            if m:
                qn={'FIRST':1,'SECOND':2,'THIRD':3,'FOURTH':4}[m.group(1).upper()]
                return f"Q{qn} FY{m.group(2)[-2:]}", "source title"
        return "", ""

    def _quarter_fallback_by_ticker(ticker, report_dt):
        # Secondary fallback only when Yahoo and report-title inference both fail.
        fye_month={
            'MSFT':6,'AEHR':5,'LITE':6,'NVDA':1,
            'ORCL':5,'AVGO':10,'CRWD':1,'MU':8
        }.get((ticker or '').upper())
        if not fye_month:
            return ""
        rd=report_dt.date()
        qend_month=((rd.month-2-1)%12)+1
        dist=(fye_month-qend_month)%12
        nearest=min([0,3,6,9],key=lambda x:abs(x-dist))
        if abs(nearest-dist)>1:
            return ""
        qnum={9:1,6:2,3:3,0:4}[nearest]
        fy=rd.year+(1 if qend_month>fye_month else 0)
        return f"Q{qnum} FY{str(fy)[-2:]}"


    """)
    if anchor not in src:
        raise RuntimeError("refresh anchor missing")
    src=src.replace(anchor,helper+anchor,1)

    # Carry provenance fields.
    src=must_replace(
        src,
        """            'fiscalQuarterLabel':prior.get('fiscalQuarterLabel') or ''
        }""",
        """            'fiscalQuarterLabel':prior.get('fiscalQuarterLabel') or '',
            'quarterSource':prior.get('quarterSource') or '',
            'actualSource':prior.get('actualSource') or '',
            'estimateSource':prior.get('estimateSource') or '',
            'transcriptSource':prior.get('transcriptSource') or ''
        }""",
        "provenance fields"
    )

    # Revenue actual source.
    src=must_replace(
        src,
        "            if yf_rev is not None:item['revenueActual']=yf_rev",
        "            if yf_rev is not None:\n                item['revenueActual']=yf_rev\n                item['actualSource']='Yahoo quarterly financials'",
        "Yahoo source"
    )

    # Fiscal quarter source + fallbacks.
    src=must_replace(
        src,
        """            if t is not None:
                fq=_fiscal_quarter_label(t,dt)
                if fq:item['fiscalQuarterLabel']=fq""",
        """            if t is not None:
                fq=_fiscal_quarter_label(t,dt)
                if fq:
                    item['fiscalQuarterLabel']=fq
                    item['quarterSource']='Yahoo financial statements'
            if not item.get('fiscalQuarterLabel'):
                fq,qs=_quarter_from_sources(arts)
                if fq:
                    item['fiscalQuarterLabel']=fq
                    item['quarterSource']=qs
            if not item.get('fiscalQuarterLabel'):
                fq=_quarter_fallback_by_ticker(c['ticker'],dt)
                if fq:
                    item['fiscalQuarterLabel']=fq
                    item['quarterSource']='fiscal calendar fallback'""",
        "quarter fallback apply"
    )

    # MarketBeat revenue provenance.
    src=must_replace(
        src,
        """                if item['revenueActual'] is None:
                    item['revenueActual'] = mb_act
                elif item['revenueActual'] and abs(mb_act/item['revenueActual']-1) <= 0.15:
                    item['revenueActual'] = mb_act""",
        """                if item['revenueActual'] is None:
                    item['revenueActual'] = mb_act
                    item['actualSource']='MarketBeat'
                elif item['revenueActual'] and abs(mb_act/item['revenueActual']-1) <= 0.15:
                    item['revenueActual'] = mb_act""",
        "MarketBeat actual provenance"
    )
    src=must_replace(
        src,
        """                if anchor_rev is None or (anchor_rev and abs(mb_est/anchor_rev-1) <= 0.50):
                    item['revenueEstimate'] = mb_est""",
        """                if anchor_rev is None or (anchor_rev and abs(mb_est/anchor_rev-1) <= 0.50):
                    item['revenueEstimate'] = mb_est
                    item['estimateSource']='MarketBeat'""",
        "MarketBeat estimate provenance"
    )

    # Article fallback provenance.
    src=must_replace(
        src,
        """                if item['revenueActual'] is None:item['revenueActual']=rev_act
                elif item['revenueActual'] and abs(rev_act/item['revenueActual']-1)<=0.15:item['revenueActual']=rev_act""",
        """                if item['revenueActual'] is None:
                    item['revenueActual']=rev_act
                    item['actualSource']='earnings article'
                elif item['revenueActual'] and abs(rev_act/item['revenueActual']-1)<=0.15:
                    item['revenueActual']=rev_act""",
        "article actual provenance"
    )
    src=must_replace(
        src,
        """                if anchor_rev is None or (anchor_rev and abs(rev_est/anchor_rev-1)<=0.50):
                    item['revenueEstimate']=rev_est""",
        """                if anchor_rev is None or (anchor_rev and abs(rev_est/anchor_rev-1)<=0.50):
                    item['revenueEstimate']=rev_est
                    item['estimateSource']='earnings article'""",
        "article estimate provenance"
    )

    # Transcript provenance.
    src=must_replace(
        src,
        """            trusted_mgmt_text=' '.join(trusted_mgmt_parts)
            mb=_management_bullets(trusted_mgmt_text)
            if mb:item['management']=mb
            elif not trusted_mgmt_text:item['management']=[]""",
        """            trusted_mgmt_text=' '.join(trusted_mgmt_parts)
            mb=_management_bullets(trusted_mgmt_text)
            if mb:
                item['management']=mb
                for a in arts:
                    blob=((a.get('title') or '')+' '+(a.get('url') or '')).lower()
                    if a.get('text') and any(k in blob for k in [
                        'earnings call','conference call','transcript',
                        'investors.','investor relation','seekingalpha','investing.com'
                    ]):
                        item['transcriptSource']=a.get('url') or a.get('title') or ''
                        break
            elif not trusted_mgmt_text:
                item['management']=[]
                item['transcriptSource']=''""",
        "transcript provenance"
    )

    # Extended-hours market reaction: use the actual pre-market / after-hours move.
    anchor="def refresh_earnings_data(targets):"
    reaction_helper=textwrap.dedent(r"""
    def _earnings_session_reaction(symbol, report_dt):
        try:
            et=report_dt.astimezone(ZoneInfo("America/New_York"))
            t=yf.Ticker(symbol)
            d=et.date()
            start=(d-timedelta(days=3)).isoformat()
            end=(d+timedelta(days=3)).isoformat()

            daily=t.history(start=start,end=end,interval="1d",auto_adjust=False)
            intr=t.history(start=start,end=end,interval="5m",prepost=True,auto_adjust=False)
            if daily is None or daily.empty or intr is None or intr.empty:
                return None,"",""

            drows=[]
            for ix,row in daily.iterrows():
                px=safe_float(row.get('Close'))
                if px not in (None,0):
                    drows.append((ix.date(),px))

            session=""
            base=None
            start_dt=None
            end_dt=None

            if et.hour>=16:
                session="盤後"
                same=[p for day,p in drows if day==d]
                if not same:
                    same=[p for day,p in drows if day<d]
                base=same[-1] if same else None
                start_dt=et
                end_dt=et.replace(hour=20,minute=0,second=0,microsecond=0)
            elif 4<=et.hour<10:
                session="盤前"
                prev=[p for day,p in drows if day<d]
                base=prev[-1] if prev else None
                start_dt=et
                end_dt=et.replace(hour=9,minute=30,second=0,microsecond=0)
            else:
                return None,"",""

            if not base or end_dt<=start_dt:
                return None,"",""

            vals=[]
            for ix,row in intr.iterrows():
                try:
                    ix_et=ix.tz_convert("America/New_York") if getattr(ix,"tzinfo",None) else ix.tz_localize("UTC").tz_convert("America/New_York")
                    dt=ix_et.to_pydatetime()
                except Exception:
                    continue
                if start_dt<=dt<=end_dt:
                    px=safe_float(row.get('Close'))
                    if px not in (None,0):
                        vals.append((dt,px))

            if not vals:
                return None,session,""

            vals.sort(key=lambda x:x[0])
            dt,px=vals[-1]
            return (px/base-1)*100,session,dt.isoformat()
        except Exception:
            return None,"",""


    def _push_new_earnings_reports(reports, old_reports):
        old_latest={}
        for x in old_reports or []:
            if not isinstance(x,dict):
                continue
            sym=x.get('symbol')
            rd=x.get('reportDate')
            if sym and rd and (sym not in old_latest or rd>old_latest[sym]):
                old_latest[sym]=rd

        now_date=datetime.now(timezone.utc).date()
        pushed=[]
        for x in reports or []:
            sym=x.get('symbol')
            rd=x.get('reportDate')
            if not sym or not rd:
                continue
            # No migration backfill. Push only when this run discovers a newer quarter.
            if not old_latest.get(sym) or rd<=old_latest.get(sym):
                continue
            try:
                rdate=datetime.fromisoformat(rd).date()
                if abs((now_date-rdate).days)>2:
                    continue
            except Exception:
                continue

            ticker=x.get('ticker') or sym
            quarter=x.get('fiscalQuarterLabel') or rd
            epsa=x.get('epsActual'); epse=x.get('epsEstimate')
            reva=x.get('revenueActual'); reve=x.get('revenueEstimate')
            reaction=x.get('firstReactionPct')
            session=x.get('reactionSession') or ''

            parts=[f"{quarter} 財報已公布"]
            if epsa is not None:
                parts.append(f"EPS {epsa:.2f}" + (f" vs {epse:.2f}" if epse is not None else ""))
            if reva is not None:
                ra=_fmt_money(reva) or str(reva)
                rb=_fmt_money(reve) if reve is not None else None
                parts.append(f"營收 {ra}" + (f" vs {rb}" if rb else ""))
            if reaction is not None:
                parts.append(f"{session or '延長盤'} {reaction:+.2f}%")

            ok,_=_send_pushover(
                f"{ticker}｜財報公布",
                "\n".join(parts),
                url=f"https://morris199786.github.io/us-stock-watch/?earnings={urllib.parse.quote(ticker)}"
            )
            if ok:
                pushed.append(ticker)
        return pushed


    """)
    if anchor not in src:
        raise RuntimeError("reaction helper anchor missing")
    src=src.replace(anchor,reaction_helper+anchor,1)

    # Replace first-reaction fallback with session-aware extended-hours move first.
    src=must_replace(
        src,
        """            fr=_first_reaction(joined)
            if fr is None:
                fr=_extended_reaction_from_prices(c['symbol'],dt)
            if fr is not None:item['firstReactionPct']=fr""",
        """            fr,session,observed=_earnings_session_reaction(c['symbol'],dt)
            if fr is None:
                fr=_first_reaction(joined)
            if fr is None:
                fr=_extended_reaction_from_prices(c['symbol'],dt)
            if fr is not None:
                item['firstReactionPct']=fr
            item['reactionSession']=session or item.get('reactionSession') or ''
            item['reactionObservedAt']=observed or item.get('reactionObservedAt') or ''""",
        "session-aware reaction"
    )

    # Persist reaction metadata.
    src=must_replace(
        src,
        """            'transcriptSource':prior.get('transcriptSource') or ''
        }""",
        """            'transcriptSource':prior.get('transcriptSource') or '',
            'reactionSession':prior.get('reactionSession') or '',
            'reactionObservedAt':prior.get('reactionObservedAt') or ''
        }""",
        "reaction metadata"
    )

    # Push newly discovered reports once, without backfilling old quarters.
    src=must_replace(
        src,
        """    reports.sort(key=lambda x:x.get('reportDateTime',''),reverse=True)
    upcoming.sort(key=lambda x:x.get('date',''))
    payload={'parserVersion':9""",
        """    reports.sort(key=lambda x:x.get('reportDateTime',''),reverse=True)
    upcoming.sort(key=lambda x:x.get('date',''))
    earnings_pushes=_push_new_earnings_reports(reports, old.get('reports') or [])
    payload={'parserVersion':9""",
        "earnings push hook"
    )

    # Validate full parser before replacing bootstrap.
    compile(src,"updater.py","exec")
    checks=[
        "payload={'parserVersion':9",
        "_quarter_from_sources",
        "_quarter_fallback_by_ticker",
        "actualSource",
        "estimateSource",
        "transcriptSource",
        "site:seekingalpha.com",
        "site:stockanalysis.com",
        "_earnings_session_reaction",
        "_push_new_earnings_reports",
        "reactionSession",
    ]
    for token in checks:
        if token not in src:
            raise RuntimeError(f"self-test missing {token}")
    if len(src)<150000:
        raise RuntimeError(f"final updater too small: {len(src)}")

    UPDATER.write_text(src,encoding="utf-8")

    cfg=json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["earnings_parser_version"]=TARGET_VERSION
    CONFIG.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    print(f"Final updater installed: {len(src)} bytes, parserVersion={TARGET_VERSION}")

    code=compile(src,str(UPDATER),"exec")
    g={"__name__":"__main__","__file__":str(UPDATER),"__package__":None}
    exec(code,g,g)

if __name__=="__main__":
    main()
