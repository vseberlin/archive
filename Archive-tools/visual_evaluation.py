"""Held-out catalogue agreement for the existing visual research embeddings.

Evaluation-only predictions; never writes metadata or review decisions.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import re
import time

from archive_common import atomic_write, require
from visual_research import digest, load, output, save

FIELDS = ('tags', 'places', 'object_type', 'date')
MONTHS = 'Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember'
DATE = re.compile(rf'(?:(?:\d{{1,2}}\.\s*)?(?:{MONTHS})\s+|\d{{1,2}}\.\d{{1,2}}\.)?(1[6-9]\d{{2}}|20[0-2]\d)', re.I)


def exact_year(value):
    match = DATE.fullmatch(str(value or '').strip())
    return int(match[1]) if match else None


def labels(row, field):
    # Current catalogue fields only: never combine dates from unrelated events.
    value = row['catalogue_record'].get(field, '') or ''
    if field == 'object_type':
        return {str(value).strip().casefold()} if str(value).strip() else set()
    return {v.strip().casefold() for v in str(value).split(';') if v.strip()}


def series(row):
    inv = row['catalogue_record'].get('inventory', '').strip()
    # Keep KS roll/prefix and main number; -1/-2 and letter suffixes are frames.
    match = re.fullmatch(r'(.+?[-_])(\d+)(?:-\d+|[A-Za-z])?', inv)
    return (row['id'].split('#')[0], match[1].casefold(), int(match[2])) if match else None


def title_series(row):
    title = row.get('title', '').casefold()
    return re.sub(r'\b(?:bild|aufnahme|foto)\s*\d+\b', 'bild #', title) if re.search(r'\b(?:bild|aufnahme|foto)\s*\d+\b',title) else ''


def identities(row):
    c = row['catalogue_record']
    result = {('id',row['id'])}
    md = str(c.get('md_object_id') or '')
    if md:
        result.add(('md',md.removeprefix('md:')))
    if row.get('preview_sha256'):
        result.add(('preview',row['preview_sha256']))
    for field in ('final_sha256','source_sha256'):
        if c.get(field):
            result.add(('payload',c[field].removeprefix('sha256:')))
    return result


def exclusion_mask(index, rows, identity_sets, series_keys, title_keys, hidden, window):
    import numpy as np
    own = series_keys[index]
    return np.array([bool(identity_sets[index] & ids) or bool(hidden and (
        own and key and own[:2] == key[:2] and abs(own[2]-key[2]) <= window or
        title_keys[index] and title_keys[index] == title_keys[i] and rows[index]['id'].split('#')[0] == rows[i]['id'].split('#')[0]))
        for i,(ids,key) in enumerate(zip(identity_sets,series_keys))])


def band(support, voters):
    return 'strong' if support >= .75 and voters >= 3 else 'medium' if support >= .5 and voters >= 2 else 'weak'


def weighted_median(values, weights):
    pairs = sorted(zip(values,weights))
    threshold = sum(weights)/2
    total = 0
    for value,weight in pairs:
        total += weight
        if total >= threshold:
            return value


def predict(rows, ids, similarities, args, uniform=False):
    usable = [(int(i),float(s)) for i,s in zip(ids,similarities) if uniform or s >= args.min_similarity]
    if not usable:
        return {f: [] for f in FIELDS}
    top = max(s for _,s in usable)
    weights = {i:1. if uniform else math.exp((s-top)/args.temperature) for i,s in usable}
    total = sum(weights.values())
    result = {}
    for field in FIELDS:
        if field == 'date':
            dated = [(i,exact_year(rows[i]['catalogue_record'].get('date_text'))) for i,_ in usable]
            dated = [(i,y) for i,y in dated if y is not None]
            if len(dated)<2:
                result[field]=[];continue
            year=weighted_median([y for i,y in dated],[weights[i] for i,y in dated])
            voters=[i for i,y in dated if abs(y-year)<=5]
            support=sum(weights[i] for i in voters)/total
            result[field]=[{'value':year,'support':support,'voters':len(voters),'band':band(support,len(voters))}] if support>=args.min_support and len(voters)>=2 else []
            continue
        votes=defaultdict(list)
        for i,_ in usable:
            for value in labels(rows[i],field):votes[value].append(i)
        ranked=sorted(votes.items(),key=lambda p:(-sum(weights[i] for i in p[1]),p[0]))
        if field=='object_type':ranked=ranked[:1]
        result[field]=[]
        for value,voters in ranked:
            support=sum(weights[i] for i in voters)/total
            if support>=args.min_support and len(voters)>=2:
                result[field].append({'value':value,'support':support,'voters':len(voters),'band':band(support,len(voters))})
    return result


def summarize(records, method, field):
    scorable=covered=predicted=correct=truth_count=unscorable_predictions=0
    errors=[];jaccards=[];bands=defaultdict(lambda:{'predicted':0,'correct':0})
    per_label=defaultdict(lambda:Counter())
    for r in records:
        truth=r['truth'][field];pred=r[method][field]
        if truth is None or truth==[]:
            unscorable_predictions+=len(pred);continue
        scorable+=1;covered+=bool(pred)
        if field=='date':
            if pred:
                error=abs(pred[0]['value']-truth);errors.append(error)
                predicted+=1;correct+=error<=5
                b=bands[pred[0]['band']];b['predicted']+=1;b['correct']+=error<=5
        else:
            actual=set(truth);values={p['value'] for p in pred};truth_count+=len(actual)
            predicted+=len(values);correct+=len(actual & values)
            for value in actual:per_label[value]['truth']+=1
            for p in pred:
                b=bands[p['band']];b['predicted']+=1;b['correct']+=p['value'] in actual
                per_label[p['value']]['predicted']+=1;per_label[p['value']]['correct']+=p['value'] in actual
            if pred:jaccards.append(len(actual & values)/len(actual|values))
    for b in bands.values():b['agreement']=b['correct']/b['predicted'] if b['predicted'] else None
    result={'queries':len(records),'scorable':scorable,'covered':covered,
            'coverage':covered/scorable if scorable else None,'predicted_values':predicted,'matching_values':correct,
            'agreement':correct/predicted if predicted else None,'unscorable_predictions':unscorable_predictions,
            'support_bands':dict(bands)}
    if field=='date':
        import numpy as np
        result.update(mae_years=float(np.mean(errors)) if errors else None,
                      median_error_years=float(np.median(errors)) if errors else None,
                      p90_error_years=float(np.quantile(errors,.9)) if errors else None)
    else:
        result.update(recall=correct/truth_count if truth_count else None,
                      mean_jaccard=sum(jaccards)/len(jaccards) if jaccards else None,
                      macro_recall=sum(v['correct']/v['truth'] for v in per_label.values() if v['truth'])/sum(bool(v['truth']) for v in per_label.values()) if truth_count else None)
    return result


MODES = ('all_neighbours', 'series_hidden', 'unit_hidden')
BANDS = {'all': {'strong','medium','weak'}, 'high': {'strong'},
         'medium': {'medium'}, 'high_medium': {'strong','medium'}}


def pool_counts(metadata, indices):
    result={'n':len(indices),'fields':{f:Counter() for f in FIELDS}}
    for i in indices:
        for f in FIELDS:
            value=metadata[i][f]
            result['fields'][f].update([value] if f=='date' and value is not None else [] if f=='date' else value)
    return result


def subtract_counts(total, removed):
    return {'n':total['n']-removed['n'],
            'fields':{f:total['fields'][f]-removed['fields'][f] for f in FIELDS}}


def median_counts(counts):
    if not counts:return None
    threshold=sum(counts.values())/2; cumulative=0
    for value,n in sorted(counts.items()):
        cumulative+=n
        if cumulative>=threshold:return value


def prevalence(counts, args):
    result={f:[] for f in FIELDS}
    if not counts['n']:return result
    for f in FIELDS:
        values=counts['fields'][f]
        if f=='date':
            year=median_counts(values)
            if year is None:continue
            voters=sum(n for y,n in values.items() if abs(y-year)<=5)
            support=voters/counts['n']
            if sum(values.values())>=2 and voters>=2 and support>=args.min_support:
                result[f]=[{'value':year,'voters':voters,'support':support,'band':band(support,voters)}]
        else:
            ranked=sorted(values.items(),key=lambda p:(-p[1],p[0]))
            if f=='object_type':ranked=ranked[:1]
            result[f]=[{'value':v,'voters':n,'support':n/counts['n'],'band':band(n/counts['n'],n)}
                       for v,n in ranked if n>=2 and n/counts['n']>=args.min_support]
    return result


def matched_prior(counts, prediction):
    """Same cases and output budget as the model; no query truth is consulted."""
    result={f:[] for f in FIELDS}
    for f in FIELDS:
        n=len(prediction[f])
        if not n:continue
        if f=='date':
            year=median_counts(counts['fields'][f])
            if year is not None:result[f]=[{'value':year,'band':'prior'}]
        else:
            ranked=sorted(counts['fields'][f].items(),key=lambda p:(-p[1],p[0]))
            result[f]=[{'value':v,'band':'prior'} for v,_ in ranked[:n]]
    return result


def paired_gain(records, field, baseline, seed):
    """Query-resampled exploratory interval; positive gain favours images."""
    import numpy as np
    pairs=[]
    for r in records:
        truth=r['truth'][field];p=r['prediction'][field];b=r[baseline][field]
        if truth is None or truth==[] or not p or len(p)!=len(b):continue
        if field=='date':
            pairs.append((abs(b[0]['value']-truth)-abs(p[0]['value']-truth),1))
        else:
            actual=set(truth)
            pairs.append((sum(x['value'] in actual for x in p)-sum(x['value'] in actual for x in b),len(p)))
    if not pairs:return {'queries':0,'gain':None,'bootstrap_95':None}
    a=np.asarray(pairs,float);rng=np.random.default_rng(seed)
    indices=rng.integers(0,len(a),size=(1000,len(a)));draw=a[indices].sum(axis=1)
    return {'queries':len(a),'gain':float(a[:,0].sum()/a[:,1].sum()),
            'bootstrap_95':np.quantile(draw[:,0]/draw[:,1],[.025,.975]).tolist()}


def compare(records, field, band_name, seed):
    filtered=[]
    for r in records:
        cache=r.setdefault('_tier_cache',{})
        if band_name not in cache:
            pred={f:[p for p in r['prediction'][f] if p['band'] in BANDS[band_name]] for f in FIELDS}
            pool=matched_prior(r['allowed_counts'],pred)
            unit=matched_prior(r['unit_counts'],pred) if r['unit_counts']['n'] else {f:[] for f in FIELDS}
            cache[band_name]={'truth':r['truth'],'prediction':pred,'pool_matched':pool,'unit_matched':unit,
                             'baseline':r['baseline'],'unit_baseline':r['unit_baseline']}
        filtered.append(cache[band_name])
    metrics={method:summarize(filtered,method,field) for method in
             ('prediction','baseline','unit_baseline','pool_matched','unit_matched')}
    metrics['gain_vs_pool']=paired_gain(filtered,field,'pool_matched',seed)
    metrics['gain_vs_unit']=paired_gain(filtered,field,'unit_matched',seed)
    truth_total=sum(len(r['truth'][field]) for r in records if field!='date' and r['truth'][field]) if field!='date' else 0
    metrics['available_label_fraction']=sum(sum(v in r['allowed_counts']['fields'][field] for v in r['truth'][field])
                                          for r in records if r['truth'][field]) / truth_total if truth_total else None
    return metrics


def make_policy(comparisons):
    """User ceilings plus conservative pilot gates; this is not metadata authority."""
    result={'format':'visual-field-policy-1','metadata_authority':False,'automatic_adoption':False,
            'confidence_meaning':'high/medium are neighbour-support tiers, not calibrated probabilities',
            'gate':'series-hidden gain > 0 against both output-matched pool and known-unit priors; exploratory 95% lower bound > 0; >=20 paired queries',
            'unknown_unit':'research leads only; never infer the unit from neighbours to bypass the policy',
            'units':{},'heterogeneous_date_workflow':{
                'visual_knn':False,'evidence_sources':['verso_ocr','documented_konvolut_context'],
                'unit_median_fallback':{'requires_known_unit':True,'requires_explicit_choice':True,
                                        'source_kind':'unit_median_fallback','observed_date':False,
                                        'requires_human_review':True,'label':'Unit median fallback, not photograph date'},
                'preserve':['raw_text_or_context','exact_source','date_role','uncertainty','reviewer'],
                'implemented':'routing policy only; no OCR/date extraction or assignments performed'}}
    for unit in ('tfa','kurt-schwarz'):
        if unit not in comparisons:continue
        fields={}
        for f in FIELDS:
            allowed=f in ('tags','date','places') if unit=='tfa' else f=='tags'
            tiers=['high_medium','high'] if unit=='tfa' else ['high']
            passes=False;chosen=tiers[0];gains=[]
            for tier in tiers:
                if tier not in comparisons[unit]['series_hidden']:continue
                evidence=comparisons[unit]['series_hidden'][tier][f]
                gains=[evidence[k] for k in ('gain_vs_pool','gain_vs_unit')]
                chosen=tier
                passes=allowed and all(g['queries']>=20 and g['gain'] is not None and g['gain']>0 and g['bootstrap_95'][0]>0 for g in gains)
                if passes:break
            fields[f]={'enabled_for_review_proposals':passes,'allowed_tiers':['high','medium'] if chosen=='high_medium' and passes else ['high'] if passes else [],
                       'candidate_tiers':['high','medium'] if unit=='tfa' and allowed else ['high'] if allowed else [],
                       'scope':'known unit only','reason':'passes pilot gates; human review required' if passes else 'disabled by user scope' if not allowed else 'baseline gain or evidence-size gate not met',
                       'evidence_tier':chosen,'evidence':{'gain_vs_pool':gains[0],'gain_vs_unit':gains[1]}}
        result['units'][unit]=fields
    return result


def evaluate(args):
    import numpy as np
    require(args.k>0 and args.temperature>0 and args.series_window>=0 and 0<=args.min_support<=1 and -1<=args.min_similarity<=1,'Invalid evaluation settings')
    root=args.work_root;m=load(root);e=json.loads((root/'embedding-manifest.json').read_text())
    require(e['manifest_digest']==digest(m),'Embedding manifest is stale')
    name=args.name or 'evaluation-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    require(re.fullmatch(r'[A-Za-z0-9_-]+',name) is not None,'Invalid output name')
    for suffix in ('.json','-cases.jsonl','.html','-field-policy.json'):
        require(not output(root,name+suffix).exists(),'Evaluation receipt already exists')
    rows=sorted(m['references'],key=lambda r:r['id']);keys={r['id']:r['key'] for r in e['items']};vectors=[]
    for r in rows:
        require(r['status']=='ok','Reference preview failed');key=keys[r['id']]
        require(key==digest({'config':e['config'],'image':r['preview_sha256']}),'Embedding key mismatch')
        vectors.append(np.load(output(root,f'embeddings/{key}.npy'),allow_pickle=False))
    matrix=np.stack(vectors);norms=np.linalg.norm(matrix,axis=1,keepdims=True)
    require(np.isfinite(matrix).all() and bool((norms>0).all()),'Invalid embeddings');matrix/=norms
    metadata=[{f:exact_year(r['catalogue_record'].get('date_text')) if f=='date' else sorted(labels(r,f)) for f in FIELDS} for r in rows]
    groups=defaultdict(list)
    for i,r in enumerate(rows):groups[r['id'].split('#')[0]].append(i)
    units=sorted(groups);require(len(units)>=2,'Whole-unit holdout needs at least two units')
    require(0<args.sample<=len(rows),'Invalid sample size');sample=[];rng=np.random.default_rng(args.seed)
    for pos,u in enumerate(units):
        n=round(args.sample*len(groups[u])/len(rows)) if pos<len(units)-1 else args.sample-len(sample)
        sample.extend(int(i) for i in rng.choice(groups[u],size=n,replace=False))
    sample.sort();identity_sets=[identities(r) for r in rows];series_keys=[series(r) for r in rows];title_keys=[title_series(r) for r in rows]
    all_counts=pool_counts(metadata,list(range(len(rows))));unit_counts={u:pool_counts(metadata,indices) for u,indices in groups.items()}
    membership={u:np.array([r['id'].split('#')[0]==u for r in rows]) for u in units}
    outside_counts={u:subtract_counts(all_counts,unit_counts[u]) for u in units};empty=pool_counts(metadata,[])
    records=[];started=time.monotonic()
    for position,index in enumerate(sample):
        sims=matrix@matrix[index];u=rows[index]['id'].split('#')[0]
        for mode in MODES:
            mask=exclusion_mask(index,rows,identity_sets,series_keys,title_keys,mode=='series_hidden',args.series_window)
            if mode=='unit_hidden':
                mask |= membership[u]
                extra=np.flatnonzero(mask & ~membership[u]).tolist()
                allowed=subtract_counts(outside_counts[u],pool_counts(metadata,extra));known=empty
            else:
                allowed=subtract_counts(all_counts,pool_counts(metadata,np.flatnonzero(mask).tolist()))
                known=subtract_counts(unit_counts[u],pool_counts(metadata,np.flatnonzero(mask & membership[u]).tolist()))
            remaining=np.flatnonzero(~mask);ranked=remaining[np.argsort(-sims[remaining],kind='stable')[:args.k]]
            require(mode!='unit_hidden' or all(not membership[u][i] for i in ranked),'Unit holdout leaked')
            records.append({'query_id':rows[index]['id'],'unit':u,'mode':mode,'excluded':int(mask.sum()),
                            'truth':metadata[index],'prediction':predict(rows,ranked,sims[ranked],args),
                            'baseline':prevalence(allowed,args),'unit_baseline':prevalence(known,args),
                            'allowed_counts':allowed,'unit_counts':known,
                            'neighbours':[{'id':rows[i]['id'],'similarity':float(sims[i])} for i in ranked]})
        if (position+1)%25==0:print(json.dumps({'evaluated':position+1,'sample':len(sample),'seconds':round(time.monotonic()-started,1)}),flush=True)
    subsets={'all':records,**{Path(u).name:[r for r in records if r['unit']==u] for u in units}}
    summary={subset:{mode:{method:{f:summarize([r for r in rs if r['mode']==mode],method,f) for f in FIELDS}
                                  for method in ('prediction','baseline','unit_baseline')} for mode in MODES} for subset,rs in subsets.items()}
    comparisons={subset:{mode:{b:{f:compare([r for r in rs if r['mode']==mode],f,b,args.seed) for f in FIELDS} for b in BANDS} for mode in MODES} for subset,rs in subsets.items()}
    policy=make_policy(comparisons)
    limitations=['Catalogue agreement is not independently verified truth or visual relevance; missing labels are not negative evidence.',
                 'All three modes use the same fixed held-out sample. This is exploratory policy selection, not independent confirmation after threshold tuning.',
                 'Whole-unit holdout excludes that unit from neighbours and every permitted prior. Known-unit baselines are unavailable in this mode.',
                 'Only two units are available: whole-unit holdout is two directional domain-transfer tests, not a general guarantee for unknown photographs.',
                 'Output-matched priors use model-selected cases and label counts, but never their truth; they isolate ranking value from abstention/output volume.',
                 'High/medium name support tiers, not calibrated probabilities. Bootstrap intervals resample queries; residual series correlation can make them optimistic.',
                 'Date tests use unambiguous recorded years; approximate dates and ranges are excluded. No individual date is established by a unit median.',
                 'People/collections are not inferred; object_type is near-constant. No metadata or review decisions are changed.']
    result={'format':'visual-research-evaluation-2','created_at':time.time(),'manifest_digest':digest(m),'model':e['config'],
            'reference_count':len(rows),'sample_count':len(sample),'sample_ids':[rows[i]['id'] for i in sample],
            'sample_by_unit':dict(Counter(Path(rows[i]['id'].split('#')[0]).name for i in sample)),
            'settings':{k:getattr(args,k) for k in ('seed','k','series_window','min_similarity','min_support','temperature')},
            'reference_field_coverage':{f:sum(metadata[i][f] is not None if f=='date' else bool(metadata[i][f]) for i in range(len(rows))) for f in FIELDS},
            'summary':summary,'comparisons':comparisons,'field_policy':policy,'limitations':limitations,
            'excluded_by_mode':{mode:{'min':min(r['excluded'] for r in records if r['mode']==mode),'max':max(r['excluded'] for r in records if r['mode']==mode)} for mode in MODES}}
    save(root,name+'.json',result);save(root,name+'-field-policy.json',policy)
    # Avoid repeating the full vocabulary counts 1200 times; preserve predictions and exact matched priors for audit.
    compact=[]
    for r in records:
        item={k:v for k,v in r.items() if k not in ('allowed_counts','unit_counts','_tier_cache')}
        item['by_tier']={}
        for b,allowed_bands in BANDS.items():
            pred={f:[p for p in r['prediction'][f] if p['band'] in allowed_bands] for f in FIELDS}
            item['by_tier'][b]={'prediction':pred,'pool_matched':matched_prior(r['allowed_counts'],pred),'unit_matched':matched_prior(r['unit_counts'],pred)}
        compact.append(item)
    atomic_write(output(root,name+'-cases.jsonl'),b''.join(json.dumps(r,ensure_ascii=False,sort_keys=True).encode()+b'\n' for r in compact))
    write_report(root,name,result)
    save(root,'evaluation-status.json',{'phase':'complete','report':str(root/(name+'.html')),'sample':len(sample),'completed_at':time.time()})
    print(json.dumps({'evaluation':str(root/(name+'.html')),'policies':{u:{f:p['enabled_for_review_proposals'] for f,p in fs.items()} for u,fs in policy['units'].items()}}),flush=True)


def write_report(root,name,result):
    def pct(x):return 'N/A' if x is None else f'{x:.1%}'
    def number(x):return 'N/A' if x is None else f'{x:.2f}'
    def esc(x):return html.escape(str(x))
    parts=['<!doctype html><meta charset="utf-8"><title>Visual field evaluation</title><style>body{font:15px system-ui;margin:24px}table{border-collapse:collapse;margin-bottom:24px}td,th{padding:7px;border:1px solid #bbb;text-align:left}details{margin:20px 0}pre{white-space:pre-wrap}li{margin:8px}</style><h1>Visual field evaluation against baselines</h1>',
           f'<p>{result["sample_count"]} held-out queries across {result["reference_count"]} references. Every field is paired with baselines and coverage. No values are adopted.</p>',
           '<p>High = support ≥75% with ≥3 voters; medium = support ≥50% with ≥2 voters. These are support tiers, not calibrated confidence.</p>',
           '<p>Pool matched and unit matched emit the same number of labels on the same cases as the visual method, using frequency ranks or median years. Unit matched is unavailable when the whole unit is hidden. Prevalence is the original fixed-support baseline; its coverage is shown separately.</p>',
           '<h2>Field decisions for known units</h2><p>Review proposals only. Unknown-unit images remain research leads; neighbour similarity does not establish their unit.</p><table><tr><th>Unit</th><th>Field</th><th>Enabled</th><th>Tiers</th><th>Reason</th></tr>']
    for unit,fields in result['field_policy']['units'].items():
        for field,decision in fields.items():
            parts.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in
                         (unit,field,decision['enabled_for_review_proposals'],', '.join(decision['allowed_tiers']) or 'none',decision['reason']))+'</tr>')
    parts.append('</table><p>Heterogeneous-unit dates: verso OCR or documented Konvolut context; an explicitly chosen unit-median fallback must be labelled as such, never as an observed photograph date. OCR and date extraction have not been run.</p>')
    for subset,modes in result['comparisons'].items():
        for mode,tiers in modes.items():
            parts.append('<h2>'+esc(subset+' — '+mode)+'</h2>')
            for tier,fields in tiers.items():
                parts.append('<details'+(' open' if tier in ('high_medium','high') and subset!='all' else '')+'><summary>'+esc(tier)+'</summary><table><tr><th>Field</th><th>Visual agreement / ±5 years</th><th>Pool matched</th><th>Unit matched</th><th>Visual queries covered / scorable</th><th>Visual / pool / unit year MAE</th><th>Pool prevalence agreement (coverage)</th><th>Unit prevalence agreement (coverage)</th><th>Visual recall / macro recall</th><th>Label vocabulary available</th></tr>')
                for f,c in fields.items():
                    p=c['prediction'];bp=c['baseline'];bu=c['unit_baseline'];pm=c['pool_matched'];um=c['unit_matched']
                    values=(f,pct(p['agreement']),pct(pm['agreement']),pct(um['agreement']),f'{p["covered"]}/{p["scorable"]} ({pct(p["coverage"])})',
                            ' / '.join(number(x.get('mae_years')) for x in (p,pm,um)) if f=='date' else '—',
                            f'{pct(bp["agreement"])} ({pct(bp["coverage"])})',f'{pct(bu["agreement"])} ({pct(bu["coverage"])})',
                            f'{pct(p.get("recall"))} / {pct(p.get("macro_recall"))}',pct(c['available_label_fraction']))
                    parts.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in values)+'</tr>')
                parts.append('</table></details>')
    parts.append('<h2>Limitations</h2><ul>'+''.join('<li>'+esc(x)+'</li>' for x in result['limitations'])+'</ul>')
    parts.append(f'<p><a href="{name}.json">Metrics, paired gains and exploratory intervals</a> · <a href="{name}-cases.jsonl">All cases</a> · <a href="{name}-field-policy.json">Field policy</a></p>')
    atomic_write(output(root,name+'.html'),''.join(parts).encode())
