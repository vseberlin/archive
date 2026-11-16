"""Documentation views for the existing Collection 551 workbench."""
import csv
import difflib
import html
import json
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode
from zipfile import ZipFile


STYLE = """
.doc-toolbar{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}.doc-toolbar input{flex:1;min-width:180px}.doc-toolbar select,select.doc-select{max-width:100%;padding:.55rem}
.doc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(285px,1fr));gap:1rem}.doc-card{display:flex;flex-direction:column;min-width:0}.doc-card h2{font-size:1.08rem;margin:.7rem 0 .3rem;overflow-wrap:anywhere}.doc-card p{margin:.35rem 0}.doc-card .doc-open{margin-top:auto;padding-top:.6rem}
.doc-thumb{height:180px;overflow:hidden;background:#f6f5ef;border:1px solid #d8d5ca;padding:.6rem;font-size:.75rem;color:#444}.doc-thumb img{width:100%;height:100%;object-fit:contain}.doc-thumb .doc-sheet{max-height:none;padding:.3rem;box-shadow:none}.doc-thumb table{font-size:.6rem}.doc-thumb .preview-label{font-size:.65rem}
.doc-columns{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:1rem}.doc-columns>section{min-width:0}.doc-columns h2{font-size:1.05rem;overflow-wrap:anywhere}.doc-sheet{background:#fff;padding:1.1rem;border:1px solid #ccc;max-height:650px;overflow:auto;box-shadow:0 2px 7px #0001;line-height:1.55}.doc-sheet p{white-space:pre-wrap;overflow-wrap:anywhere}.doc-sheet table{min-width:500px;table-layout:fixed}.doc-sheet td{font-size:.8rem}.doc-page{width:100%;height:650px;object-fit:contain;background:#e7e5df;display:block}.preview-label{font-size:.8rem;color:#666;margin:.4rem 0}.doc-diff{white-space:pre-wrap;font-size:.82rem;max-height:400px;overflow:auto;background:#f7f6f0;padding:1rem}.doc-state{font-weight:600}.doc-facts{font-size:.84rem}.doc-save{margin-top:1rem;border-top:1px solid #ddd;padding-top:.7rem}.doc-save label{display:block;margin:.4rem 0}.doc-group-nav{display:flex;gap:1rem;margin-bottom:1rem}.doc-short{min-height:3rem}.doc-paths{font-size:.8rem;overflow-wrap:anywhere}.doc-warning{background:#fff0d7;border-left:4px solid #b36b00;padding:.7rem;margin:.5rem 0}.doc-date{overflow-wrap:anywhere}
@media(max-width:800px){.doc-columns{grid-template-columns:1fr}.doc-page{height:520px}.doc-sheet{max-height:520px}}
"""


def esc(value):
    return html.escape(str(value))


def url(route, **args):
    return route + '?' + urlencode(args)


class Documents:
    def __init__(self, workbench):
        self.workbench = workbench
        self.core = sys.modules[type(workbench).__module__]
        self.root = self.core.ROOT
        self.csv_path = self.root / 'accession-document-review.csv'
        self.evidence = {r['source_sha256']: r for r in (json.loads(line) for line in (self.root / 'accession-document-internal-evidence.jsonl').read_text().splitlines())}
        self.previews = {}
        self.preview_lock = threading.Lock()
        self.reload()

    def reload(self):
        self.rows = self.core.read_csv(self.csv_path)
        self.by_id = {r['document_id']: r for r in self.rows}
        self.groups = defaultdict(list)
        for r in self.rows:
            if r['deletion_authorized'] != 'false':
                raise ValueError('Documentation must not carry deletion authority.')
            self.groups[r['content_sha256']].append(r)
        if any(sum(r['decision'] == 'included' for r in group) > 1 for group in self.groups.values()):
            raise ValueError('A document has more than one selected original.')

    def representative(self, group):
        return next((r for r in group if r['decision'] == 'included'), group[0])

    def items(self):
        return sorted((self.representative(g) for g in self.groups.values()), key=lambda r: (r['decision'] != 'needs_review', self.topic(r), Path(r['source_path']).name.casefold(), r['document_id']))

    def get(self, identifier):
        if identifier not in self.by_id:
            raise ValueError('Unknown documentation record.')
        return self.by_id[identifier]

    def state(self, row):
        group = self.groups[row['content_sha256']]
        if row['source_path'] == str(self.root / 'FEB-Import-konsolidiert.csv') and row['decision'] == 'included': return 'Final document'
        if all(self.core.document_is_unrelated(r) for r in group): return 'Unrelated'
        if all(r['decision'] == 'redundant' and r.get('review_note', '').startswith('Noise:') for r in group): return 'Noise'
        if any(r['decision'] == 'included' for r in group): return 'Selected original'
        if any(r['decision'] == 'needs_review' for r in group): return 'Unresolved'
        return 'Not selected'

    def resolved(self, row):
        group = self.groups[row['content_sha256']]
        return all(r.get('decided_at') and r['decision'] in {'included', 'redundant', 'excluded'} for r in group)

    def visible_items(self, query='', status='pending'):
        return [r for r in self.items()
                if (not query or query.casefold() in (' '.join([r['source_path'], self.short(r), self.topic(r)])).casefold())
                and ((not self.resolved(r) and self.state(r) != 'Noise') if status == 'pending'
                     else status == 'all' or self.state(r) == status)]

    def topic(self, row):
        name = Path(row['source_path']).name.casefold()
        if 'stunden' in name: return 'Time and budget calculations'
        if 'arbeitspaket' in name: return 'Work plans'
        if 'autoren' in name: return 'Authors'
        if 'jahrgang' in name: return 'Chronological inventories'
        if Path(name).suffix in {'.xlsx', '.csv'}: return 'Inventories and MD imports'
        if any(s in name for s in ['geschichte', 'bestand', 'tektonik', 'vorgehensweise']): return 'Archive context'
        return 'FEB digitisation applications'

    def short(self, row):
        name = Path(row['source_path']).name
        topic = self.topic(row)
        if self.state(row) == 'Final document': return 'Selected consolidated FEB import and inventory document: 1,929 records, with provenance and review notes.'
        if self.state(row) == 'Unrelated': return 'Outside Collection 551; may support the wider Industriesalon collections. Excluded from this unit\'s drain list; source retained.'
        if self.state(row) == 'Noise': return row.get('review_note', 'Noise: redundant documentation.')
        if row['decision'] == 'needs_review' and row['condition'].startswith(('damaged', 'unreadable')): return 'Damaged candidate. Its identity could not be confirmed from readable contents; compare with the related readable version.'
        if row['content_sha256'] == 'c93eba333b4be52c54adef2aa4a132222de19538269bd072f3eb339506720e58':
            return 'Signed budget page dated 18 September 2019. The identical scan is already on page 15 of the complete application.'
        descriptions = {
            'Time and budget calculations': 'Workload calculation for scanning reports, transcribing patent books and preparing museum-digital records.',
            'Work plans': 'Quarterly work packages for the proposed FEB digitisation project, with tasks, staff and estimated hours.',
            'Authors': 'Author reference list supporting identification of contributors to the WF research reports.',
            'Chronological inventories': 'Year-by-year report inventory, with titles, dates and illustration information.',
            'Inventories and MD imports': 'Source inventory or museum-digital import table containing FEB identifiers and descriptive metadata.',
            'Archive context': 'Context for the FEB collection: archive structure, provenance, history or earlier cataloguing work.',
            'FEB digitisation applications': 'Funding application for presenting the WF research and development reports online in 2020.',
        }
        text = descriptions[topic]
        if name == '20190919124114.pdf': text = 'Scanned FEB funding application identified from OCR of its contents, despite the timestamp-only filename.'
        if row['condition'].startswith('damaged'): text += ' Damaged original; surviving internal text confirms its relevance.'
        return text

    def source(self, row):
        self.core.source_guard()
        path = Path(row['source_path'])
        if path != self.root / 'FEB-Import-konsolidiert.csv' and not self.core.inside(path, self.core.SOURCE_ROOT / 'restore/archive'):
            raise ValueError('Document is outside the protected source scope.')
        stat = path.stat()
        if stat.st_size != int(row['size_bytes']) or stat.st_mtime_ns != int(row['mtime_ns']):
            raise ValueError('Document changed since the recorded inspection.')
        return path

    def image_path(self, identifier, page):
        row = self.get(identifier); path = self.source(row)
        if path.suffix.lower() != '.pdf': raise ValueError('Page images are available for PDFs only.')
        props = self.evidence[row['content_sha256']].get('properties', {})
        count = int(props.get('Pages', 1))
        if not 1 <= page <= count: raise ValueError('Page is outside this document.')
        target = self.root / 'preview-cache' / f"document-{row['content_sha256']}-page-{page}.png"
        with self.preview_lock:
            if not target.exists():
                rendered = subprocess.run(['pdftoppm', '-f', str(page), '-l', str(page), '-singlefile', '-scale-to', '1500', '-png', str(path)], capture_output=True, timeout=90)
                if rendered.returncode or not rendered.stdout.startswith(b'\x89PNG'):
                    raise ValueError('PDF preview could not be rendered; original remains unchanged.')
                self.core.atomic_replace(target, rendered.stdout)
        return target

    def content_preview(self, row):
        digest = row['content_sha256']
        if digest in self.previews: return self.previews[digest]
        path = self.source(row); evidence = self.evidence[digest]
        text = evidence.get('extracted_text', '')
        label = 'Content preview — original page layout is not reproduced'
        inner = ''
        try:
            if row['condition'].startswith(('damaged', 'unreadable')):
                label = 'Surviving internal text only — original cannot be rendered reliably'
                excerpts = evidence.get('relevance_excerpts') or evidence.get('errors') or [row['condition']]
                inner = ''.join('<p>' + esc(s) + '</p>' for s in excerpts[:5])
            elif path.suffix.lower() in {'.xlsx', '.csv'}:
                from collection_551_prepare import xlsx_sheets, csv_rows
                gen = xlsx_sheets(path) if path.suffix.lower() == '.xlsx' else csv_rows(path)
                sheet, rows = next(gen); gen.close()
                header = next((i for i,r in enumerate(rows[:12]) if any('invent' in v.casefold() or 'inventory' in v.casefold() for k,v in r.items() if k >= 0)), 0)
                sample = rows[header:header+9]
                columns = sorted({k for r in sample for k in r if k >= 0})[:6]
                label = f'Table sample — {sheet}, first {len(sample)} rows, up to six columns'
                inner = '<table>' + ''.join('<tr>' + ''.join('<td>' + esc(r.get(k, '')[:260]) + '</td>' for k in columns) + '</tr>' for r in sample) + '</table>'
            elif path.suffix.lower() == '.docx':
                with ZipFile(path) as z: node = ET.fromstring(z.read('word/document.xml'))
                ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                paragraphs = [''.join(t.text or '' for t in p.iter(ns+'t')).strip() for p in node.iter(ns+'p')]
                inner = ''.join('<p>'+esc(t)+'</p>' for t in [t for t in paragraphs if t][:24])
            else:
                label = 'Partial text preview — legacy Office layout is not reproduced'
                excerpts = evidence.get('relevance_excerpts') or [text[:3500]]
                inner = ''.join('<p>'+esc(t)+'</p>' for t in excerpts[:5])
        except Exception as error:
            label = 'Partial evidence preview — original rendering unavailable'
            inner = '<p>'+esc(row['condition'])+'</p><p>'+esc(row['content_evidence'][:3500])+'</p>'
        result = '<p class="preview-label">'+esc(label)+'</p><div class="doc-sheet">'+inner+'</div>'
        self.previews[digest] = result
        return result

    def preview(self, row, page=1, small=False):
        if Path(row['source_path']).suffix.lower() == '.pdf':
            return '<p class="preview-label">Original PDF · page '+str(page)+'</p><img loading="lazy" class="doc-page" alt="PDF page preview" src="'+esc(url('/document-preview',id=row['document_id'],page=page))+'">'
        if small:
            return '<p class="preview-label">'+esc(Path(row['source_path']).suffix.upper().lstrip('.'))+' · content excerpt</p><p>'+esc(row['content_evidence'][:450])+'</p>'
        return self.content_preview(row)

    def related(self, row):
        items = [r for r in self.items() if r['content_sha256'] != row['content_sha256'] and self.state(r) != 'Noise']
        name = Path(row['source_path']).name.casefold()
        def score(other):
            other_name = Path(other['source_path']).name.casefold()
            return (other_name == name, self.topic(other) == self.topic(row), not other['condition'].startswith(('damaged','unreadable')), difflib.SequenceMatcher(None,name,other_name).ratio())
        return sorted(items,key=score,reverse=True)

    def index(self, query='', status='pending'):
        self.reload(); all_items = self.items()
        items = self.visible_items(query, status)
        unresolved = sum(self.state(r) == 'Unresolved' for r in all_items)
        body = '<section class="summary"><h1>Documentation</h1><p>'+f'{len(all_items)} distinct documents · {len(self.rows)} source paths · {unresolved} unresolved'+'</p><p>Compare document contents and related versions. Byte-identical copies are listed together.</p><form class="doc-toolbar"><input aria-label="Search documentation" type="text" name="q" placeholder="Search documents" value="'+esc(query)+'"><select aria-label="Document status" name="status">'
        body += ''.join('<option'+(' selected' if status == s else '')+' value="'+esc(s)+'">'+esc({'all': 'All documents (including resolved)', 'pending': 'Pending review'}.get(s, s))+'</option>' for s in ['pending','all','Unresolved','Selected original','Final document','Not selected','Noise','Unrelated'])
        body += '</select><button>Filter</button></form>'
        consolidated = self.root / 'FEB-Import-Vergleich.html'
        if consolidated.exists():
            final = next((r for r in all_items if self.state(r) == 'Final document'), None)
            if final:
                body += '<section class="panel final-document"><h2>Final FEB import and inventory document</h2><p><strong>FEB-Import-konsolidiert.csv</strong> · 1,929 records · selected by Vladimir</p><p>Superseded import and inventory documents are recorded as noise in the drain list. The source evidence and 13 identifier review notes remain available.</p><a href="'+esc(url('/file', path=str(consolidated)))+'">Preview final document →</a> · <a href="'+esc(url('/file',path=final['source_path']))+'">Download final CSV</a></section>'
            else:
                body += '<p><a href="'+esc(url('/file', path=str(consolidated)))+'">FEB import series: consolidated table, source comparison and review notes →</a></p>'
        body += '<p>'+str(len(items))+' shown. Saved final choices and noise are hidden from Pending review; all records remain available through the filter.</p>'
        plan_path = self.root / 'accession-preparation.json'
        if plan_path.exists() and not json.loads(plan_path.read_text()).get('preparation_complete'):
            body += '<p class="doc-warning">The saved documentation choices require a refresh of the accession preparation before execution.</p>'
        body += '</section><section class="doc-grid">'
        if not items:
            body += '<section class="panel"><h2>No documents to review</h2><p>No documents match this view. Use the filter to inspect retained review records.</p></section>'
        for r in items:
            link=esc(url('/document',id=r['document_id']))
            body += '<article class="panel doc-card"><a tabindex="-1" aria-hidden="true" href="'+link+'"><div class="doc-thumb">'+self.preview(r,small=True)+'</div></a><h2>'+esc(Path(r['source_path']).name)+'</h2><div><span class="pill">'+esc(self.topic(r))+'</span><span class="pill '+('warn' if self.state(r)=='Unresolved' else '')+'">'+esc(self.state(r))+'</span></div><p class="doc-short">'+esc(self.short(r))+'</p><p class="muted">'+str(len(self.groups[r['content_sha256']]))+' exact source cop'+('y' if len(self.groups[r['content_sha256']])==1 else 'ies')+'</p><a class="doc-open" href="'+link+'">Preview and compare →</a></article>'
        return body+'</section>'

    def panel(self, row, page, side, other_id, other_page):
        e = self.evidence[row['content_sha256']]
        body='<section class="panel"><h2>'+esc(Path(row['source_path']).name)+'</h2><p>'+esc(self.short(row))+'</p><div class="facts"><span class="pill">'+esc(self.state(row))+'</span><span class="pill">'+f"{int(row['size_bytes'])/1024/1024:.2f} MiB"+'</span><span class="pill">'+esc(row['relationship'])+'</span></div>'
        if row['condition'].startswith(('damaged','unreadable')): body+='<div class="doc-warning">'+esc(row['condition'])+'</div>'
        body+='<p class="doc-facts doc-date">'+esc(row['document_date'] or 'Date unknown')+' · '+esc(row['date_basis'])+'</p>'
        if Path(row['source_path']).suffix.lower()=='.pdf':
            pages=int(e.get('properties',{}).get('Pages',1));page=max(1,min(page,pages))
            body+='<form class="doc-toolbar" method="get" action="/document"><input type="hidden" name="id" value="'+esc(row['document_id'] if side=='left' else other_id)+'"><input type="hidden" name="compare" value="'+esc(other_id if side=='left' else row['document_id'])+'"><input type="hidden" name="'+('compare_page' if side=='left' else 'page')+'" value="'+str(other_page)+'"><label>Page <input aria-label="'+side+' PDF page" style="width:5rem" type="number" min="1" max="'+str(pages)+'" name="'+('page' if side=='left' else 'compare_page')+'" value="'+str(page)+'"> of '+str(pages)+'</label><button>Show page</button></form>'
        body+=self.preview(row,page)+'<p><a href="'+esc(url('/file',path=row['source_path']))+'" target="_blank" rel="noopener">Open original file</a></p><details><summary>Content evidence and source copies</summary><p>'+esc(row['content_evidence'])+'</p><p class="doc-facts">'+esc(row['inspection_method'])+'</p>'
        body+='<ul class="doc-paths">'+''.join('<li>'+esc(r['source_path'])+'</li>' for r in self.groups[row['content_sha256']])+'</ul><p class="doc-paths">SHA-256: '+esc(row['content_sha256'])+'</p></details></section>'
        return body

    def detail(self, identifier, compare='', page=1, compare_page=1, saved=False):
        self.reload();row=self.get(identifier);related=self.related(row)
        if not compare:
            signed = row['content_sha256']=='c93eba333b4be52c54adef2aa4a132222de19538269bd072f3eb339506720e58'
            if signed:
                other=next(r for r in self.items() if r['content_sha256']=='8f803de28e11e30c509150a1c8c2f2f8148640588adc52a61657961e73d4422c');compare_page=15
            else: other=related[0]
        else: other=self.get(compare)
        body='<div class="doc-group-nav"><a href="/documents">← Pending documentation</a></div>'
        if saved and row.get('decided_at'): body+='<p class="good">Documentation choice saved. The accession preparation needs refreshing before execution.</p>'
        body+='<section class="summary"><h1>Compare documents</h1><form class="doc-toolbar" method="get"><input type="hidden" name="id" value="'+esc(identifier)+'"><label for="doc-compare">Compare with</label><select id="doc-compare" class="doc-select" name="compare">'
        body+=''.join('<option value="'+esc(r['document_id'])+'"'+(' selected' if r['content_sha256']==other['content_sha256'] else '')+'>'+esc(Path(r['source_path']).name+' · '+self.state(r))+'</option>' for r in related)
        body+='</select><button>Compare</button></form><p class="muted">Different drafts can both be retained. Comparing them does not change either selection.</p></section>'
        body+='<div class="doc-columns">'+self.panel(row,page,'left',other['document_id'],compare_page)+self.panel(other,compare_page,'right',identifier,page)+'</div>'
        left=self.evidence[row['content_sha256']];right=self.evidence[other['content_sha256']]
        lhs=left.get('relevance_excerpts',[]);rhs=right.get('relevance_excerpts',[])
        delta=int(other['size_bytes'])-int(row['size_bytes'])
        body+='<section class="panel"><h2>Comparison notes</h2><p>'+('Byte-identical content.' if row['content_sha256']==other['content_sha256'] else 'Different stored SHA-256 hashes; these are distinct files.')+f' The right file is {abs(delta):,} bytes '+('larger' if delta>=0 else 'smaller')+'.</p><p>Left: '+esc(row['condition'])+'<br>Right: '+esc(other['condition'])+'</p>'
        diff=list(difflib.unified_diff('\n'.join(lhs).splitlines(),'\n'.join(rhs).splitlines(),fromfile='Left content excerpts',tofile='Right content excerpts',lineterm=''))[:70]
        body+='<details><summary>Compare extracted content excerpts</summary><p class="muted">Partial evidence only; this is not a full-document comparison.</p><pre class="doc-diff">'+esc('\n'.join(diff) or 'No difference in the available excerpts.')+'</pre></details></section>'
        group=self.groups[row['content_sha256']];chosen=next((r['document_id'] for r in group if r['decision']=='included'),'')
        body+='<section class="panel"><h2>Review this document</h2><form action="/document-save" method="post"><input type="hidden" name="id" value="'+esc(identifier)+'"><input type="hidden" name="compare" value="'+esc(other['document_id'])+'"><label class="master-choice"><input type="radio" name="action" value="included"'+(' checked' if chosen else '')+'> Select one original for documentation</label><label>Original copy <select class="doc-select" name="source">'
        body+=''.join('<option value="'+esc(r['document_id'])+'"'+(' selected' if r['document_id']==chosen else '')+'>'+esc(r['source_path'].removeprefix(str(self.core.SOURCE_ROOT / 'restore/archive')+'/'))+'</option>' for r in group)
        body+='</select></label><label class="master-choice"><input type="radio" name="action" value="unrelated"'+(' checked' if self.state(row)=='Unrelated' else '')+'> Unrelated to Collection 551 — keep for wider collections</label><p class="muted">Removes these source copies from this unit\'s drain list and hides the document from Pending review. It remains accessible under Unrelated.</p><label class="master-choice"><input type="radio" name="action" value="redundant"'+(' checked' if not chosen and self.state(row) in {'Not selected', 'Noise'} else '')+'> Mark this document redundant</label><label class="master-choice"><input type="radio" name="action" value="needs_review"'+(' checked' if self.state(row)=='Unresolved' else '')+'> Leave unresolved</label><label>Short note<textarea name="note" maxlength="500">'+esc(row.get('review_note',''))+'</textarea></label><p class="muted">Applies only to this document and its byte-identical copies. Redundant never means safe to delete.</p><button>Save document choice</button></form></section>'
        return body

    def save(self, identifier, action, selected, note):
        self.core.require_reconciliation_open()
        if action not in {'included','redundant','needs_review','unrelated'}: raise ValueError('Unsupported document choice.')
        with self.workbench.lock:
            self.core.source_guard()
            self.reload();row=self.get(identifier);group=self.groups[row['content_sha256']]
            if action == 'unrelated':
                prefix = 'Unrelated to Collection 551:'
                if not note.startswith(prefix):
                    note = prefix + ' ' + (note or 'May belong to the wider Industriesalon collections; retain source and exclude from this unit\'s drain.')
                action = 'excluded'
            if action=='included':
                if selected not in {r['document_id'] for r in group}: raise ValueError('Selected original belongs to another document.')
                for r in group: self.source(r)
            fields=list(self.rows[0])
            for field in ['review_note','actor','decided_at']:
                if field not in fields:fields.append(field)
            for r in self.rows:
                for field in ['review_note','actor','decided_at']:r.setdefault(field,'')
            target=next((r['proposed_relative_path'] for r in group if r['proposed_relative_path']), '')
            if action=='included' and not target:
                source=self.get(selected);target='documents/'+Path(source['source_path']).name
                if any(r['proposed_relative_path']==target and r['content_sha256']!=row['content_sha256'] for r in self.rows):
                    target='documents/'+Path(source['source_path']).stem+'__'+source['document_id']+Path(source['source_path']).suffix
            for r in group:
                r['decision']='included' if action=='included' and r['document_id']==selected else 'redundant' if action=='included' else action
                r['review_note']=self.core.compact(note);r['actor']=self.workbench.actor;r['decided_at']=self.core.now()
                r['retained_source_path']=self.get(selected)['source_path'] if action=='included' else ''
                r['proposed_relative_path']=target if action=='included' else ''
            # Invalidate the prepared snapshot before changing its documentation input.
            plan_path=self.root/'accession-preparation.json'
            if plan_path.exists():
                plan=json.loads(plan_path.read_text());plan['preparation_complete']=False
                plan['remaining_execution_requirement']='Refresh accession preparation after saved documentation review, then obtain explicit accession authorization.'
                self.core.atomic_replace(plan_path,(json.dumps(plan,ensure_ascii=False,indent=2)+'\n').encode())
            self.core.atomic_replace(self.csv_path,self.core.csv_payload(fields,self.rows))
            self.update_drain_exclusions()
            self.reload()

    def update_drain_exclusions(self):
        excluded = self.core.unrelated_document_paths(self.rows)
        # Preserve the original readable evidence before filtering the active list.
        evidence_path = self.core.DRAIN_EVIDENCE_CSV
        if excluded and not evidence_path.exists():
            self.core.atomic_replace(evidence_path, self.core.csv_payload(self.core.DRAIN_FIELDS, self.workbench.drain_base))
        drain = self.workbench.drain_for(self.workbench.decisions(), excluded_paths=excluded)
        payload = self.core.csv_payload(self.core.DRAIN_FIELDS, drain)
        if payload != self.core.DRAIN_CSV.read_bytes():
            self.core.atomic_replace(self.core.DRAIN_CSV, payload)
        register_path = self.root / 'accession-source-disposition.csv'
        if excluded and register_path.exists():
            register = self.core.read_csv(register_path)
            retained = [r for r in register if r['source_path'] not in excluded]
            if len(retained) != len(register):
                self.core.atomic_replace(register_path, self.core.csv_payload(list(register[0]), retained))
