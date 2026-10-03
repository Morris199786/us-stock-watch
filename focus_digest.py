"""Source-bound company catalyst briefs. No model probabilities or invented rationale."""
import re
from difflib import SequenceMatcher
VERSION='focus-catalysts-20261004'
NOISE=re.compile(r"before you buy|our ai model|stock picks are beating|subscribe|sign up|premium|discount|cookie|privacy policy|top analyst stocks|research tools|stock screener|read more|標題：|URL 來源|發佈時間|立即升級|頂級分析師股票|智慧投資者|折扣|免責聲明|訂閱|隱私",re.I)
GENERIC=re.compile(r"beyond simple chatbots|investors must now decide|whether to bet|best .* stocks to buy|artificial intelligence boom|is .* a good investment|機會來了|投資者必須|最佳.*股票",re.I)
ACTIONS=re.compile(r"price target|target price|upgrade[ds]? .*\b(buy|hold|sell|overweight|outperform|rating)|downgrade[ds]?|initiates? coverage|評級|目標價|上調.*評|下調.*評",re.I)
FACTS=re.compile(r"\$\s?[\d,.]+|\d[\d,.]*\s?(?:%|billion|million|mw|gw|億|萬|億元)|guidance|backlog|contract|agreement|orders?|capacity|revenue|eps|margin|expects?|forecast|launch|approval|raises|cuts|because|citing|demand|供應|財測|需求|訂單|產能|營收|毛利|合作|核准|上調|下調|目標价|目標價",re.I)
BROKER=re.compile(r"\b(Needham|UBS|BofA|Morgan Stanley|Goldman Sachs|JPMorgan|JP Morgan|Barclays|Citi|Jefferies|Wells Fargo|TD Cowen|Freedom Capital|Deutsche Bank|Baird|RBC|Mizuho|Piper Sandler|Bernstein)\b|券商|分析師|瑞銀|摩根|高盛|美銀",re.I)

def clean(text):
    text=re.sub(r'<[^>]*>',' ',str(text or ''))
    return re.sub(r'\s+',' ',text).strip()

def sentences(text):
    return [clean(s) for s in re.split(r'(?<=[.!?。！？])\s+|[\r\n]+',str(text or '')) if clean(s)]

def company_terms(ticker,name):
    terms=[ticker,clean(name)]
    terms += {'HPE':['Hewlett Packard Enterprise','慧與'],'ON':['ON Semiconductor','onsemi','安森美'],'COHR':['Coherent'],'CBRS':['Cerebras'],'GOOGL':['Google','Alphabet'],'GOOG':['Google','Alphabet'],'META':['Meta Platforms'],'SPCX':['SpaceX'],'AMD':['Advanced Micro Devices'],'MU':['Micron','美光'],'NVDA':['Nvidia','輝達']}.get(ticker,[])
    return [x for x in terms if len(x)>1]

def anchored(text,terms):
    return any(re.search(r'(?<![\w])'+re.escape(t)+r'(?![\w])',text,re.I) if t.isascii() else t in text for t in terms)

def classify(x):
    title=clean(x.get('originalTitle') or x.get('title'))
    details=x.get('analystDetails') or []
    # A generic "upgrade" (products/outlook) is not a brokerage rating.
    if ACTIONS.search(title) and (BROKER.search(title) or any(d.get('firm') or d.get('broker') for d in details)):
        return 1,'券商調整'
    if re.search(r'earnings|quarter.*results|eps|財報|財測|guidance',title,re.I):return 2,'財報／財測'
    if re.search(r'contract|orders?|agreement|customer|partnership|deal|訂單|合作|客戶',title,re.I):return 3,'訂單／客戶／合作'
    if re.search(r'outlook|forecast|capacity|demand|pricing|展望|產能|需求|定價',title,re.I):return 4,'公司展望'
    if re.search(r'launch|unveil|introduce|approve|regulatory|acquisition|merger|新產品|核准|監管|收購',title,re.I):return 5,'產品／監管／併購'
    return 99,''

def selected_facts(x,ticker,name):
    terms=company_terms(ticker,name)
    body=clean(x.get('originalSummary') or '')
    title=clean(x.get('originalTitle') or x.get('title'))
    result=[]
    # Rationale must contain company-specific evidence, not copied boilerplate.
    for d in x.get('analystDetails') or []:
        reason=clean(d.get('reason'))
        if reason and not NOISE.search(reason) and not GENERIC.search(reason) and FACTS.search(reason):
            if anchored(reason,terms):
                result.extend(sentences(reason))
    body_sents=sentences(x.get('originalSummary') or '')
    previous_anchor=False
    for s in body_sents:
        match=anchored(s,terms)
        continuation=previous_anchor and bool(re.match(r'(The company|It |Its |Management|The analyst|He |公司|管理層)',s,re.I))
        previous_anchor=match
        if NOISE.search(s) or GENERIC.search(s) or len(s)>900:continue
        if (match or continuation) and FACTS.search(s):result.append(s)
    # Best numerical/company-specific facts first; preserve source ordering on ties.
    result=sorted(enumerate(result),key=lambda z:(-len(re.findall(r'\d',z[1])),-bool(re.search(r'because|citing|因為|由於|受惠',z[1],re.I)),z[0]))
    dedup=[]
    for _,s in result:
        if any(SequenceMatcher(None,s.lower(),v.lower()).ratio()>.78 for v in dedup):continue
        dedup.append(s)
        if len(dedup)==3:break
    return dedup

def chinese(text,translate):
    s=clean(text)
    if not s:return ''
    cjk=len(re.findall(r'[\u4e00-\u9fff]',s))
    if cjk>=8 and cjk>len(re.findall(r'[A-Za-z]',s))*.25:return s[:260]
    try:out=clean(translate(s[:900]))
    except Exception:return ''
    # Translation failure must not dump a paragraph of English into the UI.
    if len(re.findall(r'[\u4e00-\u9fff]',out))<6:return ''
    return out[:260]

def digest(x,ticker,name,translate):
    priority,category=classify(x)
    if priority==99:return None
    if not anchored(clean(x.get('originalTitle') or x.get('title')), company_terms(ticker,name)):
        return None
    title=clean(x.get('title') or '')
    if len(re.findall(r'[\u4e00-\u9fff]',title))<4:title=chinese(x.get('originalTitle') or title,translate)
    if not title:return None
    facts=selected_facts(x,ticker,name)
    bullets=[]
    for f in facts:
        b=chinese(f,translate)
        if b and not NOISE.search(b) and not GENERIC.search(b) and b not in bullets:bullets.append(b)
    # Named rating/target-price headline remains useful even without full text.
    if not bullets and priority!=1:return None
    return {'headline':title[:180],'priority':priority,'category':category,'bullets':bullets[:3],
            'detailStatus':'body' if bullets else 'headline_only',
            'limitation':'' if bullets else '已確認評級／目標價動作，來源未揭露具體調整理由',
            'reason':'','judgement':'','highlights':[], 'digestVersion':VERSION}

def equivalent(a,b):
    def normalized(s):return re.sub(r'\W+','',s.lower())
    if a.get('url') and a.get('url')==b.get('url'):return True
    return SequenceMatcher(None,normalized(a.get('originalTitle') or a.get('title') or ''),normalized(b.get('originalTitle') or b.get('title') or '')).ratio()>.78
