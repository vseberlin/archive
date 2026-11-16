"""Small documentation review attached to the existing AEB workbench."""
import json
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
from prepare_591 import ROOT,guard,read_csv

def grouped():
    groups=defaultdict(list)
    for r in read_csv('documentation-review.csv'):groups[r['content_sha256']].append(r)
    return groups

def resolved(rows):return all(r['decision']!='needs_review' and r['decided_at'] for r in rows)

def status():
    groups=grouped()
    for group in groups.values():
        assert sum(r['decision']=='included' for r in group)<=1,'More than one original selected in a document group'
        assert all(r['deletion_authorized']=='false' and r['decision'] in {'included','redundant','excluded','needs_review'} for r in group)
    return {'documentation_groups':len(groups),'documentation_pending_groups':sum(not resolved(g) for g in groups.values()),'documentation_selected_files':sum(r['decision']=='included' for g in groups.values() for r in g)}

def save(core,identifier,action,source,note):
    assert action in {'included','redundant','unrelated','needs_review'} and len(note)<=2000
    with core.LOCK:
        guard();rows=read_csv('documentation-review.csv');item=next(r for r in rows if r['document_id']==identifier);group=[r for r in rows if r['content_sha256']==item['content_sha256']]
        if action=='included':assert source in {r['source_path'] for r in group if r['condition']=='readable'},'Select one readable original from this document group'
        for r in group:
            st=Path(r['source_path']).stat();assert(st.st_size,st.st_mtime_ns)==(int(r['size_bytes']),int(r['mtime_ns'])),'Source changed since inspection'
            decision='excluded' if action=='unrelated' else ('included' if r['source_path']==source else 'redundant') if action=='included' else action
            prefix='Unrelated to Collection 591: ' if action=='unrelated' else 'Noise: ' if action=='redundant' else ''
            r.update(decision=decision,review_note=prefix+note.strip(),actor='Vladimir',decided_at=datetime.now(timezone.utc).isoformat(),deletion_authorized='false')
        core.atomic(ROOT/'documentation-review.csv',core.csv_bytes(rows))

def visible_groups(view):
    groups=list(grouped().values())
    if view!='all':
        groups=[g for g in groups if not resolved(g) and (g[0]['relationship'] in {'broader','unrelated'})==(view=='broader')]
    return sorted(groups,key=lambda g:({'include':0,'review_context':1,'unrelated_to_unit':2}[g[0]['recommendation']],g[0]['description'],g[0]['document_id']))

def next_location(core,identifier,view):
    all_groups=visible_groups('all');index=next(i for i,g in enumerate(all_groups) if any(r['document_id']==identifier for r in g))
    allowed={g[0]['content_sha256'] for g in visible_groups(view)}
    for g in all_groups[index+1:]+all_groups[:index]:
        if g[0]['content_sha256'] in allowed and not resolved(g):
            return core.url('/document',id=g[0]['document_id'],view=view,saved=identifier)
    return core.url('/documents',view=view,saved=identifier)

def evidence():
    return {r['document_id']:r for r in json.loads((ROOT/'documentation-internal-evidence.json').read_text())}

def excerpt(text,limit=700):
    import re
    match=re.search(r'\bAEB\b|Applikationszentrum|Applikationslabor|Krause|Reissmüller',text,re.I)
    start=max(0,match.start()-100) if match else 0
    return ('…' if start else '')+text[start:start+limit]+('…' if len(text)>start+limit else '')

def highlighted(core,text):
    import re
    return re.sub(r'(\bAEB\b|Applikationszentrum|Applikationslabor|Krause|Reissmüller)',r'<mark>\1</mark>',core.esc(text),flags=re.I)

def related_groups(row):
    # Navigation aid only: same named inventory/timesheet family is not identity proof.
    name=Path(row['source_path']).name
    prefix='Archiv Bücher und Zeitungen' if name.startswith('Archiv Bücher und Zeitungen') else 'Stundenabrechnung_Steffen-Wedepohl' if name.startswith('Stundenabrechnung_Steffen-Wedepohl') else ''
    return [g for g in visible_groups('all') if prefix and Path(g[0]['source_path']).name.startswith(prefix) and g[0]['content_sha256']!=row['content_sha256']]

def recommendation(row):
    return {'include':'Accept as supporting documentation','review_context':'Decide whether this context belongs in the unit','unrelated_to_unit':'Keep outside the AEB unit'}[row['recommendation']]

def saved_label(group):
    if not resolved(group):return 'Awaiting your decision'
    if any(r['decision']=='included' for r in group):return 'Accepted original'
    return 'Not relevant' if group[0]['decision']=='excluded' else 'Ignored as noise / redundant'

def tabs(core,view):
    return '<nav class="tabs">'+''.join('<a class="'+('active' if v==view else '')+'" href="'+core.esc(core.url('/documents',view=v))+'">'+label+'</a>' for v,label in [('aeb','AEB candidates'),('broader','Broader references'),('all','All / saved choices')])+'</nav>'

def render(core,args):
    esc,url=core.esc,core.url;view=args.get('view',['aeb'])[0];s=status();ev=evidence()
    body='<p class="eyebrow">Documentation review</p><h1>Which documents belong with Sammlung AEB?</h1><p>'+str(s['documentation_pending_groups'])+' groups awaiting a decision · '+str(s['documentation_selected_files'])+' originals accepted</p><section><h2>Preview → evidence → scope decision</h2><p>Each card explains why the document was found. Open it to inspect pages or Office contents, compare related versions, then accept, ignore as noise, mark not relevant, or decide later.</p><p class="muted">Identical stored hashes share one review card; their original paths remain visible. Documents with different bytes keep separate decisions.</p></section>'+tabs(core,view)
    if args.get('saved'):body+='<p class="good" role="status">Decision saved. No other pending document remains in this view.</p>'
    groups=visible_groups(view);body+='<div class="cards">'
    for g in groups:
        r=next((x for x in g if x['decision']=='included'),g[0]);link=url('/document',id=r['document_id'],view=view);name=Path(r['source_path']).name
        body+='<article class="card">'
        if r['preview_path']:body+='<a href="'+esc(link)+'"><img class="thumb" loading="lazy" src="'+esc(url('/file',path=r['preview_path']))+'" alt="First page of '+esc(name)+'"></a>'
        else:body+='<div class="text-preview excerpt"><b>Extracted '+('slide text' if name.endswith('.pptx') else 'document contents')+'</b><pre>'+highlighted(core,excerpt(ev[r['document_id']]['text'],420))+'</pre></div>'
        body+='<p><span class="badge">'+esc(r['relationship'])+'</span> <span class="badge">'+saved_label(g)+'</span></p><h2><a href="'+esc(link)+'">'+esc(name)+'</a></h2><p class="reason"><b>Why this is here</b><br>'+esc(r['description'])+'</p><p class="muted">'+esc(r['document_date'] or 'Date unconfirmed')+' · '+str(len(g))+' source path(s)</p><p><a class="button" href="'+esc(link)+'">Preview and decide</a></p></article>'
    body+='</div>'
    if not groups:body+='<section class="empty"><h2>No pending documents in this view</h2><a href="/documents?view=all">Review saved choices</a></section>'
    return core.page(body)

def content_preview(core,review,row,item):
    esc,url=core.esc,core.url;name=Path(row['source_path']).name
    if row['preview_path']:
        source=review.preview_source('document:'+row['document_id'])
        return core.viewer('document:'+row['document_id'],name,source['pages'],row['source_path'])
    body='<section><h2>'+esc(name)+'</h2><p class="muted">Extracted original contents; original formatting is not reproduced.</p>'
    if item['structure'].get('slides'):
        for slide in item['structure']['slides']:
            body+='<div class="slide excerpt"><b>Slide '+str(slide['slide'])+'</b><pre>'+highlighted(core,slide['text'] or '[No extracted text on this slide]')+'</pre></div>'
    elif name.endswith('.xls'):
        import csv,io,re
        body+='<h3>Rows around the search evidence</h3>'
        sheets=item['text'].split('\f');shown=0
        for i,sheet in enumerate(sheets,1):
            rows=list(csv.reader(io.StringIO(sheet)));hits=[j for j,r in enumerate(rows) if re.search(r'\bAEB\b|Applikationszentrum|Krause|Reissmüller',' '.join(r),re.I)]
            indices=sorted({k for j in hits for k in range(max(0,j-1),min(len(rows),j+2))})
            if not indices:continue
            body+='<p><b>Extracted sheet '+str(i)+'</b></p><div class="table-scroll"><table>'
            for j in indices[:30]:
                body+='<tr><th>Row '+str(j+1)+'</th>'+''.join('<td class="excerpt">'+highlighted(core,v)+'</td>' for v in rows[j])+'</tr>';shown+=1
            body+='</table></div>'
        if not shown:body+='<pre>'+esc(item['text'][:2500])+'</pre>'
    else:body+='<pre class="excerpt">'+highlighted(core,excerpt(item['text'],4500))+'</pre>'
    return body+'<p><a target="_blank" href="'+esc(url('/file',path=row['source_path']))+'">Open original ↗</a> · <a href="'+esc(url('/document-evidence',id=row['document_id']))+'">Full extracted evidence</a></p></section>'

def detail(core,review,args):
    esc,url=core.esc,core.url;identifier=args.get('id',[''])[0];view=args.get('view',['aeb'])[0];groups=grouped();ev=evidence()
    group=next(g for g in groups.values() if any(r['document_id']==identifier for r in g));r=next((x for x in group if x['decision']=='included'),next(x for x in group if x['document_id']==identifier));name=Path(r['source_path']).name
    visible=visible_groups(view);pos=next((i for i,g in enumerate(visible) if g[0]['content_sha256']==r['content_sha256']),None)
    nav=''
    if pos is not None:nav=' · '.join('<a href="'+esc(url('/document',id=visible[i][0]['document_id'],view=view))+'">'+label+'</a>' for i,label in [(pos-1,'← Previous'),(pos+1,'Next →')] if 0<=i<len(visible))
    body='<div class="review-heading"><div><p class="eyebrow">Documentation · '+esc(r['relationship'])+'</p><h1>'+esc(name)+'</h1></div><nav>'+nav+'</nav></div><p><a href="'+esc(url('/documents',view=view))+'">Back to documentation</a> · <span class="badge">'+saved_label(group)+'</span></p>'
    if args.get('saved'):body+='<p class="good" role="status">Previous decision saved. Showing the next pending document.</p>'
    body+='<section class="reason"><h2>Why this is here</h2><p>'+esc(r['description'])+'</p><p><b>Recommendation:</b> '+recommendation(r)+'.</p><p><b>Decision needed:</b> '+('Does this direct evidence belong in the AEB unit documentation?' if r['relationship']=='direct' else 'Is this contextual or mixed material useful enough to include with AEB, or should it remain outside this unit?')+'</p></section>'
    evidence_html='<details><summary>Relevant internal evidence and inspection details</summary><section class="excerpt"><h2>Relevant internal evidence</h2><pre>'+highlighted(core,excerpt(ev[r['document_id']]['text'],1400))+'</pre><p class="muted">'+esc(r['inspection_method'])+' · '+esc(r['condition'])+'</p><p class="muted">Date: '+esc(r['document_date'] or 'Unconfirmed')+' · '+esc(r['date_basis'])+'</p></section></details>'
    related=related_groups(r);other=next((g[0] for g in groups.values() if g[0]['document_id']==args.get('compare',[''])[0] and g[0]['content_sha256']!=r['content_sha256']),None)
    if other is None and related:other=related[0][0]
    if related or other:
        body+='<form class="controls" method="get"><input type="hidden" name="id" value="'+esc(identifier)+'"><input type="hidden" name="view" value="'+esc(view)+'"><label>Compare another recorded version <select name="compare">'+''.join('<option value="'+esc(g[0]['document_id'])+'"'+(' selected' if other and g[0]['document_id']==other['document_id'] else '')+'>'+esc(Path(g[0]['source_path']).name)+' · '+g[0]['content_sha256'][:10]+'</option>' for g in related or [groups[other['content_sha256']]])+'</select></label><button>Compare</button></form><p class="muted">A shared filename is a comparison lead. Different file hashes remain separate documentation groups.</p>'
    if other:
        body+='<div class="compare-grid">'+content_preview(core,review,r,ev[r['document_id']])+content_preview(core,review,other,ev[other['document_id']])+'</div><section><h2>Differences between these versions</h2><table><tr><th>Evidence</th><th>This document</th><th>Comparison document</th></tr>'
        for label,a,b in [('Filename',name,Path(other['source_path']).name),('Size (bytes)',r['size_bytes'],other['size_bytes']),('Stored SHA-256',r['content_sha256'],other['content_sha256']),('Saved choice',saved_label(group),saved_label(groups[other['content_sha256']]))]:
            body+='<tr class="'+('changed' if a!=b else '')+'"><th>'+label+'</th><td>'+esc(a)+'</td><td>'+esc(b)+'</td></tr>'
        body+='</table><h3>Extracted content differences</h3><p class="muted">Office comparison covers the stored extraction. PDF evidence contains only the inspected opening pages. Equal extracted text does not prove equal files, formatting or complete content.</p>'+core.text_difference(ev[r['document_id']]['text'],ev[other['document_id']]['text'],name,Path(other['source_path']).name)+'<p><a href="'+esc(url('/document',id=other['document_id'],view=view))+'">Review the comparison document separately</a></p></section>'
    else:body+=content_preview(core,review,r,ev[r['document_id']])
    body+=evidence_html
    body+='<form method="post" action="/document-save"><input type="hidden" name="id" value="'+esc(r['document_id'])+'"><input type="hidden" name="view" value="'+esc(view)+'"><section class="source-list"><h2>Original source'+('s · identical stored hashes' if len(group)>1 else '')+'</h2><p>'+('Accept selects one original below and records the other exact copies as redundant.' if len(group)>1 else 'Accept retains this original as supporting documentation for later accession.')+'</p>'
    for x in group:body+='<label><input type="radio" name="source" value="'+esc(x['source_path'])+'"'+(' checked' if x['document_id']==r['document_id'] else '')+'> <a target="_blank" href="'+esc(url('/file',path=x['source_path']))+'"><code>'+esc(x['source_path'])+'</code></a></label>'
    body+='<p class="muted">Stored SHA-256: <code>'+esc(r['content_sha256'])+'</code></p><p><a href="'+esc(url('/document-evidence',id=r['document_id']))+'">Full extracted text and inspection details</a></p></section><section class="decision-panel"><h2>Your decision for '+esc(name)+'</h2><details'+(' open' if r['review_note'] else '')+'><summary>Add or edit a short note</summary><textarea name="note" maxlength="2000" aria-label="Documentation decision note">'+esc(r['review_note'])+'</textarea></details><div class="controls"><button name="action" value="included">Accept original</button><button class="secondary" name="action" value="redundant">Ignore as noise</button><button class="secondary" name="action" value="unrelated">Not relevant</button><button class="secondary" name="action" value="needs_review">Decide later</button></div><p class="muted">Accept = include one original. Ignore = record as noise / redundant. Not relevant = exclude from AEB. Decide later = keep unresolved. Saved decisions advance to the next pending document; originals stay in place.</p></section></form>'
    return core.page(body)

def evidence_page(core,identifier):
    r=evidence()[identifier]
    body='<h1>'+core.esc(Path(r['source_path']).name)+'</h1><p>'+core.esc(r['inspection'])+'</p><p><a href="'+core.esc(core.url('/document',id=identifier,view='all'))+'">Back to document review</a> · <a href="'+core.esc(core.url('/file',path=r['source_path']))+'">Open original</a></p>'
    if r['structure'].get('slides'):
        body+='<p>Ordered internal slide text; original slide layout is not reproduced.</p>'
        for slide in r['structure']['slides']:body+='<section><h2>Slide '+str(slide['slide'])+'</h2><pre>'+core.esc(slide['text'])+'</pre></section>'
    else:body+='<section><pre>'+core.esc(r['text'])+'</pre></section>'
    return core.page(body)
