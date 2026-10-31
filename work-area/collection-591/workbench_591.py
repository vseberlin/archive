#!/usr/bin/env python3
"""Direct Collection 591 review: one Master, redundant alternatives, or unresolved."""
import argparse,csv,html,io,json,mimetypes,os,re,sys,threading
import difflib,subprocess
from functools import lru_cache
from datetime import datetime,timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs,urlencode
from prepare_591 import ROOT,SOURCE,guard,read_csv,inspect

LOCK=threading.Lock()
RENDER_LIMIT=threading.BoundedSemaphore(2)
def esc(v):return html.escape(str(v),quote=True)
def url(route,**args):return route+'?'+urlencode(args)
def csv_bytes(rows):
    s=io.StringIO(newline='');w=csv.DictWriter(s,list(rows[0]),quoting=csv.QUOTE_ALL);w.writeheader();w.writerows(rows);return s.getvalue().encode()
def atomic(path,payload):
    temp=path.with_name('.'+path.name+'.tmp')
    with temp.open('wb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)

class Review:
    def __init__(self):
        guard();self.objects=read_csv('objects.csv');self.candidates=read_csv('candidates.csv');self.resources=read_csv('md-resources.csv')
        self.by_id={r['md_object_id']:r for r in self.objects};self.by_candidate={r['candidate_id']:r for r in self.candidates}
        self.local=json.loads((ROOT/'local-search-evidence.json').read_text());self.context=json.loads((ROOT/'local-context-review.json').read_text())
        self.visual=[r for r in json.loads((ROOT/'visual-search-leads.json').read_text()) if r.get('review_result')!='rejected_visual_false_positive'] if (ROOT/'visual-search-leads.json').exists() else []
        self.allowed_sources={Path(r['source_path']).resolve() for r in self.local+self.visual}
        self.allowed_sources.update(Path(m['source_path']).resolve() for c in self.candidates for m in json.loads(c['members_json']))
        self.allowed_sources.update(Path(r['source_path']).resolve() for r in read_csv('documentation-review.csv'))
        assert len(self.by_id)==68 and len(self.resources)==109
    def decisions(self):return {r['md_object_id']:r for r in read_csv('master-decisions.csv')}
    def preview_source(self,key):
        """Resolve only a manifested review item, never a caller-supplied path."""
        kind,identifier=key.split(':',1)
        if kind=='resource':
            r=next(r for r in self.resources if r['resource_id']==identifier)
            return {'path':r['work_path'],'preview':r['preview_path'],'pages':int(r['pages']),
                    'size':int(r['size_bytes']),'mtime':None}
        if kind=='candidate':
            c=self.by_candidate[identifier];assert c['origin']=='protected_usb'
            m=json.loads(c['members_json'])[0]
            preview=c['preview_path'];path=m['source_path'];size=m['size_bytes'];mtime=m['mtime_ns']
        elif kind=='document':
            r=next(r for r in read_csv('documentation-review.csv') if r['document_id']==identifier)
            preview=r['preview_path'];path=r['source_path'];size=r['size_bytes'];mtime=r['mtime_ns']
        else:raise ValueError('Unknown preview item')
        tech=Path(preview.replace('-review.jpg','-technical.json')) if preview else None
        pages=int(json.loads(tech.read_text())['pages']) if tech and tech.is_file() else 1
        return {'path':path,'preview':preview,'pages':pages,'size':int(size),'mtime':int(mtime)}
    def preview(self,key,number,text=False):
        guard();item=self.preview_source(key);p=Path(item['path']).resolve(strict=True)
        assert p.is_relative_to(ROOT.resolve()) or (p in self.allowed_sources and p.is_relative_to(SOURCE.resolve()))
        st=p.stat();assert st.st_size==item['size'],'Source size changed since inspection'
        assert item['mtime'] is None or st.st_mtime_ns==item['mtime'],'Source changed since inspection'
        assert 1<=number<=item['pages'],'Page outside this document'
        assert p.suffix.lower()=='.pdf' or not text,'Text comparison requires PDFs'
        if p.suffix.lower()!='.pdf':
            assert number==1 and item['preview'],'No image preview recorded'
            preview=Path(item['preview']).resolve(strict=True);assert preview.is_relative_to(ROOT.resolve())
            return preview.read_bytes()
        return render_pdf(str(p),st.st_size,st.st_mtime_ns,number,text)
    def check(self):
        guard();d=self.decisions();assert set(d)==set(self.by_id)
        for oid,r in d.items():
            assert r['deletion_authorized']=='false'
            if r['selected_candidate_id']:
                c=self.by_candidate[r['selected_candidate_id']];assert c['md_object_id']==oid and c['eligible']=='true'
        expected=self.dispositions(d);assert expected==read_csv('candidate-dispositions.csv')
        from documents_591 import status
        return {'result':'ok','unit':'collection-591','objects':68,'resources':109,'candidate_representations':len(self.candidates),'resolved_objects':sum(bool(r['selected_candidate_id']) for r in d.values()),'metadata_authority':'museum-digital','accession_performed':False,'deletion_authorized':False,**status()}
    def dispositions(self,d):
        return [{'md_object_id':c['md_object_id'],'candidate_id':c['candidate_id'],'disposition':('selected_master' if d[c['md_object_id']]['selected_candidate_id']==c['candidate_id'] else 'redundant') if d[c['md_object_id']]['selected_candidate_id'] else 'unresolved','source_paths_json':json.dumps([m['source_path'] for m in json.loads(c['members_json'])],ensure_ascii=False),'deletion_authorized':'false'} for c in self.candidates]
    def save(self,oid,cid,note):
        if (ROOT/'reconciliation-decisions.jsonl').exists() or (ROOT/'reconciliation-decisions.jsonl').is_symlink():
            raise ValueError('This unit uses consolidated decisions; the legacy workbench is read-only')
        assert oid in self.by_id,'Unknown object'
        assert len(note)<=2000,'Note is too long'
        with LOCK:
            guard();d=self.decisions()
            if cid:
                c=self.by_candidate[cid];assert c['md_object_id']==oid and c['eligible']=='true','Candidate does not establish a complete eligible representation'
                for m in json.loads(c['members_json']):
                    st=Path(m['source_path']).stat();assert (st.st_size,st.st_mtime_ns)==(int(m['size_bytes']),int(m['mtime_ns'])),'Selected source changed'
            d[oid].update(selected_candidate_id=cid,decision='selected_master' if cid else 'unresolved',actor='Vladimir',decided_at=datetime.now(timezone.utc).isoformat(),note=note.strip())
            # Readable decisions are the authority; disposition is their small rebuildable projection.
            atomic(ROOT/'master-decisions.csv',csv_bytes(list(d.values())))
            atomic(ROOT/'candidate-dispositions.csv',csv_bytes(self.dispositions(d)))

@lru_cache(maxsize=24)
def render_pdf(path,size,mtime,number,text):
    # Review rendering stays in memory; no derivative files or source writes.
    command=(['pdftotext','-f',str(number),'-l',str(number),'-layout',path,'-'] if text else
             ['pdftoppm','-f',str(number),'-l',str(number),'-singlefile','-scale-to','1600','-jpeg',path])
    with RENDER_LIMIT:
        result=subprocess.run(command,capture_output=True,timeout=40,env={**os.environ,'OMP_THREAD_LIMIT':'1'})
    assert result.returncode==0,'Could not render this page; open the original to inspect it.'
    return result.stdout

def text_difference(left,right,left_label='MD text',right_label='Candidate text'):
    # Escape all evidence before adding markup. Bound very long Office extracts.
    a=left.splitlines();b=right.splitlines();body=''
    matcher=difflib.SequenceMatcher(None,a,b,autojunk=True)
    shown=0
    for group in matcher.get_grouped_opcodes(2):
        body+='<tr><th colspan="2">Extracted lines '+str(group[0][1]+1)+' / '+str(group[0][3]+1)+'</th></tr>'
        for tag,i,j,k,l in group:
            aa=a[i:j];bb=b[k:l]
            for n in range(max(len(aa),len(bb))):
                if shown>=180:break
                body+='<tr class="'+('changed' if tag!='equal' else '')+'"><td>'+esc(aa[n] if n<len(aa) else '')+'</td><td>'+esc(bb[n] if n<len(bb) else '')+'</td></tr>';shown+=1
            if shown>=180:break
        if shown>=180:break
    if not body:return '<p class="good">No differences in the available extracted text.</p>'
    return '<div class="table-scroll"><table class="text-diff"><thead><tr><th>'+esc(left_label)+'</th><th>'+esc(right_label)+'</th></tr></thead><tbody>'+body+'</tbody></table></div>'+('<p>First 180 comparison lines shown. Open the full extracted evidence for the rest.</p>' if shown>=180 else '')

def viewer(key,title,pages,path):
    return '<article class="viewer" data-key="'+esc(key)+'" data-pages="'+str(pages)+'"><h3>'+esc(title)+'</h3><div class="controls"><button type="button" data-step="-1" aria-label="Previous page">←</button><label>Page <input class="page-number" type="number" min="1" max="'+str(pages)+'" value="1"></label><span>of '+str(pages)+'</span><button type="button" data-step="1" aria-label="Next page">→</button><a target="_blank" href="'+esc(url('/file',path=path))+'">Open original ↗</a></div><div class="viewport"><img class="page-image" alt="'+esc(title)+' page 1" src="'+esc(url('/preview',key=key,page=1))+'"></div><p class="image-status muted" role="status"></p></article>'

def comparison_tools():
    return '<div class="controls compare-tools"><label><input type="checkbox" id="sync-pages" checked> Link page numbers</label><label>Zoom <select id="zoom"><option value="1">Fit</option><option value="1.5">150%</option><option value="2">200%</option></select></label><button type="button" id="show-difference">Show visual difference</button><button type="button" id="show-text">Compare page text</button></div><p class="muted">Visual difference compares the displayed 1600 px page renders. Crops and alignment affect it; it does not establish identity or native resolution.</p>'

STYLE='''
*{box-sizing:border-box}body{font:16px/1.5 system-ui;background:#f3f1eb;color:#203337;margin:0}header{background:#173d42;color:white;padding:16px 3vw;display:flex;gap:24px;flex-wrap:wrap}header a{color:white;text-decoration:none}main{max-width:1500px;margin:auto;padding:28px}h1{font-size:30px;line-height:1.2;margin:0 0 12px}h2{font-size:22px;margin:0 0 12px}h3{font-size:17px;margin:0 0 12px}a{color:#12636e}section,.card,.viewer{background:white;padding:20px;border:1px solid #d5d9d5;border-radius:8px;margin-bottom:18px}.grid,.compare-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px}.cards .card{margin:0;overflow:hidden;display:flex;flex-direction:column}.cards .card>p:last-child{margin-top:auto}.thumb{width:100%;height:205px;object-fit:contain;background:#eeece5}.preview{width:100%;height:410px;object-fit:contain;background:#eeece5}.muted{color:#59676a;font-size:14px}.warning,.reason{background:#fff7e4;padding:16px;border-left:4px solid #ad863f}.reason p:last-child{margin-bottom:0}.good{color:#126346}.badge{display:inline-block;background:#e3ebe8;padding:3px 9px;border-radius:5px;font-size:13px}.eyebrow{text-transform:uppercase;letter-spacing:.08em;font-size:12px;font-weight:700;color:#59676a}.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}input,select,button,textarea,.button{font:inherit;padding:9px 12px;border:1px solid #899b9a;border-radius:5px}button,.button{background:#173d42;color:white;cursor:pointer;text-decoration:none}button.secondary,.button.secondary{color:#173d42;background:#fff}button:disabled{opacity:.5;cursor:not-allowed}button:hover,.button:hover{filter:brightness(1.15)}input[type=radio],input[type=checkbox]{accent-color:#173d42}textarea{width:100%;min-height:72px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.5 system-ui}details{margin-top:15px}summary{cursor:pointer;font-weight:600}code{overflow-wrap:anywhere;font-size:13px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #ddd;text-align:left;padding:10px;vertical-align:top;overflow-wrap:anywhere}th{font-weight:600;background:#f3f6f4}.changed td{background:#fff1d1}.table-scroll{overflow:auto;max-height:600px}.text-diff{table-layout:fixed;white-space:pre-wrap}.text-diff td{width:50%}.viewer{padding:16px;min-width:0}.viewer input[type=number]{width:70px}.viewport{height:540px;overflow:auto;background:#e8e9e5;margin-top:14px;text-align:center}.page-image{width:100%;height:100%;object-fit:contain;display:block}.viewer.zoomed .page-image{height:auto;max-width:none}.decision-panel{position:sticky;bottom:0;z-index:5;box-shadow:0 -3px 12px #173d4218;background:#f6faf6;border:1px solid #b6c9bc;padding:16px;margin-top:20px}.decision-panel details{margin:0 0 12px}.decision-panel h2{font-size:18px;margin-bottom:8px}.decision-panel p{margin:8px 0}.review-heading{display:flex;justify-content:space-between;gap:20px;align-items:start}.review-heading nav{white-space:nowrap}.text-preview{max-height:300px;overflow:auto;background:#f7f7f3;padding:14px;font-size:14px}.slide{border-left:3px solid #9fb8ad;padding:12px;margin:12px 0;background:#f7f9f7}.excerpt mark{background:#ffdf86}.diff-result[hidden]{display:none}.diff-result canvas{width:100%;max-height:620px;object-fit:contain;background:#181818}.empty{padding:40px;text-align:center}.source-list label{display:block;padding:10px 0;border-bottom:1px solid #ddd}.inline-facts{display:flex;gap:15px;flex-wrap:wrap}.card h2{font-size:19px}.card .reason{font-size:14px}.viewer .controls{font-size:14px}.viewer button{padding:6px 10px}.tabs{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}.tabs a{padding:7px 12px;border:1px solid #b7c7c0;border-radius:5px;text-decoration:none}.tabs .active{background:#173d42;color:white}input:focus-visible,button:focus-visible,a:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #d49a24;outline-offset:3px}@media(max-width:800px){main{padding:15px}.compare-grid,.grid{grid-template-columns:1fr}.viewport{height:430px}.review-heading{display:block}.decision-panel{position:static}.cards{grid-template-columns:repeat(auto-fill,minmax(250px,1fr))}}
'''
SCRIPT=r'''
const viewers=[...document.querySelectorAll('.viewer')];
function invalidate(){const result=document.querySelector('#difference-result');if(result)result.hidden=true;const text=document.querySelector('#text-result');if(text){text.hidden=true;text.replaceChildren();}}
function setPage(v,n){const input=v.querySelector('.page-number');n=Math.max(1,Math.min(Number(v.dataset.pages),n));input.value=n;const img=v.querySelector('.page-image');img.dataset.loaded='';v.querySelector('.image-status').textContent='Loading page '+n+'…';img.src='/preview?'+new URLSearchParams({key:v.dataset.key,page:n});img.alt=v.querySelector('h3').textContent+' page '+n;v.querySelector('[data-step="-1"]').disabled=n===1;v.querySelector('[data-step="1"]').disabled=n===Number(v.dataset.pages);invalidate();}
for(const v of viewers){const img=v.querySelector('.page-image');const status=v.querySelector('.image-status');const loaded=()=>{img.dataset.loaded='yes';status.textContent='Page '+v.querySelector('.page-number').value+' · '+img.naturalWidth+' × '+img.naturalHeight+' preview pixels';};img.addEventListener('load',loaded);img.addEventListener('error',()=>{img.dataset.loaded='';status.textContent='Preview unavailable. Open the original to inspect this page.';});if(img.complete&&img.naturalWidth)loaded();const change=n=>{if(!Number.isFinite(n))return;setPage(v,n);if(document.querySelector('#sync-pages')?.checked)for(const other of viewers)if(other!==v)setPage(other,n);};v.querySelector('.page-number').addEventListener('change',e=>change(Number(e.target.value)));for(const button of v.querySelectorAll('[data-step]')){button.disabled=button.dataset.step==='-1'||Number(v.dataset.pages)===1;button.addEventListener('click',()=>change(Number(v.querySelector('.page-number').value)+Number(button.dataset.step)));}}
document.querySelector('#zoom')?.addEventListener('change',e=>{for(const v of viewers){v.classList.toggle('zoomed',e.target.value!=='1');v.querySelector('.page-image').style.width=(Number(e.target.value)*100)+'%';}});
document.querySelector('#show-difference')?.addEventListener('click',()=>{const result=document.querySelector('#difference-result');result.hidden=false;const note=result.querySelector('p');const images=viewers.map(v=>v.querySelector('.page-image'));const canvas=result.querySelector('canvas');canvas.hidden=true;if(images.length!==2||images.some(i=>i.dataset.loaded!=='yes')){note.textContent='Wait until both page previews have loaded.';return;}const [a,b]=images;if(a.naturalWidth!==b.naturalWidth||a.naturalHeight!==b.naturalHeight){note.textContent='The page renders have different dimensions ('+a.naturalWidth+' × '+a.naturalHeight+' and '+b.naturalWidth+' × '+b.naturalHeight+'). Compare their crop and extent side by side; a direct pixel difference would be misleading.';return;}canvas.width=a.naturalWidth;canvas.height=a.naturalHeight;const ctx=canvas.getContext('2d',{willReadFrequently:true});ctx.drawImage(a,0,0);const x=ctx.getImageData(0,0,canvas.width,canvas.height);ctx.drawImage(b,0,0);const y=ctx.getImageData(0,0,canvas.width,canvas.height);let changed=0;for(let i=0;i<x.data.length;i+=4){let max=0;for(let c=0;c<3;c++){const d=Math.abs(x.data[i+c]-y.data[i+c]);max=Math.max(max,d);x.data[i+c]=Math.min(255,d*4);}if(max>12)changed++;x.data[i+3]=255;}ctx.putImageData(x,0,0);canvas.hidden=false;note.textContent='Displayed pages '+viewers.map(v=>v.querySelector('.page-number').value).join(' / ')+': '+(100*changed/(canvas.width*canvas.height)).toFixed(2)+'% of pixels differ by more than 12/255. Black = equal; differences brightened 4×. JPEG rendering and alignment can affect this view; this is not a source-file equality test.';result.scrollIntoView({behavior:'smooth',block:'nearest'});});
document.querySelector('#show-text')?.addEventListener('click',async e=>{const target=document.querySelector('#text-result');target.hidden=false;target.textContent='Comparing extracted page text…';e.target.disabled=true;const keys=viewers.map(v=>v.dataset.key+':'+v.querySelector('.page-number').value);try{const response=await fetch('/text-difference?'+new URLSearchParams({left:viewers[0].dataset.key,right:viewers[1].dataset.key,left_page:viewers[0].querySelector('.page-number').value,right_page:viewers[1].querySelector('.page-number').value}));if(!response.ok)throw Error('Text comparison unavailable for these pages.');if(keys.join()!==viewers.map(v=>v.dataset.key+':'+v.querySelector('.page-number').value).join())return;target.innerHTML=await response.text();}catch(error){target.textContent=error.message;}finally{e.target.disabled=false;}});
'''
def page(body):return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AEB · Collection 591 review</title><style>'+STYLE+'</style></head><body><header><a href="/">Sammlung AEB / Collection 591</a><a href="/documents">Documentation</a><a href="/local">Search evidence</a><a href="'+esc(url('/file',path=str(ROOT/'00_README.md')))+'">Scope and authority</a></header><main>'+body+'</main><script>'+SCRIPT+'</script></body></html>').encode()

class Handler(BaseHTTPRequestHandler):
    def send(self,payload,status=200,kind='text/html; charset=utf-8'):
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    def do_GET(self):
        try:
            parsed=urlparse(self.path);args=parse_qs(parsed.query);review=self.server.review
            if parsed.path=='/health':self.send(json.dumps(review.check()).encode(),kind='application/json');return
            if parsed.path=='/documents':
                import documents_591
                self.send(documents_591.render(sys.modules[__name__],args));return
            if parsed.path=='/document':
                import documents_591
                self.send(documents_591.detail(sys.modules[__name__],review,args));return
            if parsed.path=='/document-evidence':
                import documents_591
                self.send(documents_591.evidence_page(sys.modules[__name__],args.get('id',[''])[0]));return
            if parsed.path=='/file':self.file(args.get('path',[''])[0]);return
            if parsed.path=='/preview':
                self.send(review.preview(args.get('key',[''])[0],int(args.get('page',['1'])[0])),kind='image/jpeg');return
            if parsed.path=='/text-difference':
                left=review.preview(args['left'][0],int(args.get('left_page',['1'])[0]),text=True).decode(errors='replace')
                right=review.preview(args['right'][0],int(args.get('right_page',['1'])[0]),text=True).decode(errors='replace')
                self.send(('<h2>Extracted page text differences</h2><p class="muted">Compared pages '+esc(args.get('left_page',['1'])[0])+' / '+esc(args.get('right_page',['1'])[0])+'. Text-layer content can differ even when the pages look alike. Empty text does not mean an empty page.</p>'+text_difference(left,right)).encode());return
            if parsed.path=='/visual-preview':
                source=args.get('path',[''])[0];lead=next(x for x in review.visual if x['source_path']==source)
                guard();p=Path(source);st=p.stat();assert (st.st_size,st.st_mtime_ns)==(int(lead['size_bytes']),int(lead['mtime_ns']))
                with LOCK:tech=inspect(p,'visual-'+lead['asset_id'],ocr=False)
                assert tech['state']=='readable',tech['error'];self.file(tech['preview_path']);return
            if parsed.path=='/':self.index(args);return
            if parsed.path=='/object':self.object(args.get('id',[''])[0],args);return
            if parsed.path=='/local':self.local(args);return
            self.send_error(404)
        except (AssertionError,KeyError,OSError,ValueError,StopIteration,subprocess.TimeoutExpired) as e:self.send(page('<h1>Cannot open review</h1><p>'+esc(e)+'</p>'),400)
    def do_POST(self):
        if urlparse(self.path).path=='/document-save':
            try:
                import documents_591
                length=int(self.headers.get('Content-Length','0'));assert 0<length<=16384
                f=parse_qs(self.rfile.read(length).decode(),keep_blank_values=True)
                documents_591.save(sys.modules[__name__],f['id'][0],f['action'][0],f.get('source',[''])[0],f.get('note',[''])[0])
                view=f.get('view',['aeb'])[0];view=view if view in {'aeb','broader','all'} else 'aeb'
                location=documents_591.next_location(sys.modules[__name__],f['id'][0],view)
                self.send_response(303);self.send_header('Location',location);self.end_headers()
            except (AssertionError,KeyError,OSError,ValueError,StopIteration) as e:self.send(page('<h1>Documentation choice not saved</h1><p>'+esc(e)+'</p>'),400)
            return
        if urlparse(self.path).path!='/save':self.send_error(404);return
        try:
            length=int(self.headers.get('Content-Length','0'));assert 0<length<=16384
            f=parse_qs(self.rfile.read(length).decode(),keep_blank_values=True)
            oid=f['object_id'][0];self.server.review.save(oid,f.get('candidate_id',[''])[0],f.get('note',[''])[0])
            r=self.server.review;ordered=sorted(r.objects,key=lambda x:(-int(x['local_candidates']),x['inventory']))
            index=next(i for i,o in enumerate(ordered) if o['md_object_id']==oid);decisions=r.decisions()
            pending=[o for o in ordered[index+1:]+ordered[:index] if not decisions[o['md_object_id']]['selected_candidate_id']]
            location=url('/object',id=pending[0]['md_object_id'],saved=oid) if pending else url('/',saved=oid)
            self.send_response(303);self.send_header('Location',location);self.end_headers()
        except (AssertionError,KeyError,OSError,ValueError) as e:self.send(page('<h1>Decision not saved</h1><p>'+esc(e)+'</p>'),400)
    def index(self,args):
        r=self.server.review;s=r.check();d=r.decisions();view=args.get('view',['pending'])[0];q=args.get('q',[''])[0].lower()
        rows=[o for o in r.objects if (view=='all' or (view=='local' and int(o['local_candidates'])>0) or (view=='pending' and not d[o['md_object_id']]['selected_candidate_id'])) and q in (o['inventory']+' '+o['title']).lower()]
        rows.sort(key=lambda x:(-int(x['local_candidates']),x['inventory']))
        body='<p class="eyebrow">Master review</p><h1>Sammlung AEB</h1><p>'+str(68-s['resolved_objects'])+' objects awaiting a Master choice · '+str(s['resolved_objects'])+' saved · <a href="/documents">'+str(s['documentation_pending_groups'])+' documentation groups to review</a></p>'
        if args.get('saved'):body+='<p class="good" role="status">Decision saved. No other pending object remains.</p>'
        body+='<section><h2>Start with the objects that have alternatives</h2><p>Three objects have local files to compare with museum-digital. The other objects need confirmation of their complete MD representation. Open a card to inspect pages, evidence and differences.</p><form method="get" class="controls"><select name="view" aria-label="Review view">'+''.join('<option value="'+v+'"'+(' selected' if view==v else '')+'>'+label+'</option>' for v,label in [('pending','Pending review'),('local','With local alternatives'),('all','All objects / saved choices')])+'</select><input name="q" value="'+esc(q)+'" placeholder="Inventory or title" aria-label="Search objects"><button>Filter</button></form></section><div class="cards">'
        for o in rows:
            oid=o['md_object_id'];rr=sorted([x for x in r.resources if x['md_object_id']==oid],key=lambda x:(x['type']!='text',int(x['sequence'])))
            first=rr[0];link=url('/object',id=oid);local=int(o['local_candidates']);reason=(str(local)+' local file(s): compare completeness, quality and duplicate evidence.' if local else 'No verified local replacement. Confirm the complete MD resource set.')
            saved=d[oid]['selected_candidate_id'];state='Master selected' if saved else 'Compare alternatives' if local else 'Confirm MD representation'
            body+='<article class="card"><a href="'+esc(link)+'"><img class="thumb" loading="lazy" src="'+esc(url('/file',path=first['preview_path']))+'" alt="Preview of '+esc(o['inventory'])+'"></a><p><span class="badge">'+state+'</span></p><h2><a href="'+esc(link)+'">'+esc(o['inventory'])+' · '+esc(o['title'])+'</a></h2><p class="muted">'+str(len(rr))+' ordered MD resource(s) · '+str(sum(int(x['pages']) for x in rr if x['type']=='text'))+' PDF pages</p><p class="reason"><b>Why this is here</b><br>'+reason+'</p><p><a class="button" href="'+esc(link)+'">'+('Review saved choice' if saved else 'Compare and decide' if local else 'Preview and decide')+'</a></p></article>'
        body+='</div>'
        if not rows:body+='<section class="empty"><h2>No objects in this view</h2><a href="/?view=all">Show all objects and saved choices</a></section>'
        self.send(page(body))

    def object(self,oid,args=None):
        args=args or {};r=self.server.review;o=r.by_id[oid];d=r.decisions()[oid]
        cc=[c for c in r.candidates if c['md_object_id']==oid];local=[c for c in cc if c['origin']=='protected_usb']
        rr=sorted([x for x in r.resources if x['md_object_id']==oid],key=lambda x:int(x['sequence']))
        candidate=next((c for c in local if c['candidate_id']==args.get('candidate',[''])[0]),None)
        if local and candidate is None:
            candidate=next((c for c in local if c['candidate_id']==d['selected_candidate_id']),None) or next((c for c in local if 'Byte-identical' not in c['quality_note']),local[0])
        md=next((x for x in rr if x['resource_id']==args.get('resource',[''])[0]),next((x for x in rr if x['type']=='text'),rr[0]))
        ordered=sorted(r.objects,key=lambda x:(-int(x['local_candidates']),x['inventory']));index=next(i for i,x in enumerate(ordered) if x['md_object_id']==oid)
        nav=' · '.join('<a href="'+esc(url('/object',id=ordered[i]['md_object_id']))+'">'+label+'</a>' for i,label in [(index-1,'← Previous'),(index+1,'Next →')] if 0<=i<len(ordered))
        body='<div class="review-heading"><div><p class="eyebrow">Master review · '+str(index+1)+' / 68</p><h1>'+esc(o['inventory'])+' · '+esc(o['title'])+'</h1></div><nav>'+nav+'</nav></div><p><a href="/">All objects</a> · <a target="_blank" href="'+esc(o['object_url'])+'">Museum-digital record ↗</a></p>'
        if args.get('saved'):body+='<p class="good" role="status">Previous decision saved. Showing the next pending object.</p>'
        if d['selected_candidate_id']:body+='<section class="good"><b>Saved Master: '+esc(r.by_candidate[d['selected_candidate_id']]['label'])+'</b><p>'+esc(d['actor'])+' · '+esc(d['decided_at'])+'</p><p>'+esc(d['note'])+'</p></section>'
        body+='<section class="reason"><h2>Why this is here</h2><p>'+('Local files match this MD object. Decide whether to retain the complete MD representation or select an eligible local replacement.' if local else 'No sufficiently supported local replacement was found in the completed indexed searches. Inspect the MD resources and confirm one complete Master representation, or leave it unresolved.')+'</p><p><b>Current recommendation:</b> retain the MD representation. '+('No higher-resolution replacement has been established.' if local else 'All '+str(len(rr))+' resource(s) stay together in their recorded order.')+'</p></section>'
        body+='<form class="controls" method="get"><input type="hidden" name="id" value="'+esc(oid)+'"><label>MD resource <select name="resource">'+''.join('<option value="'+esc(x['resource_id'])+'"'+(' selected' if x==md else '')+'>'+str(x['sequence'])+' · '+esc(x['description'] or Path(x['work_path']).name)+' · '+x['pages']+' page(s)</option>' for x in rr)+'</select></label>'
        if local:body+='<label>Compare with <select name="candidate">'+''.join('<option value="'+esc(c['candidate_id'])+'"'+(' selected' if c==candidate else '')+'>'+esc(c['label'])+(' · comparison only' if c['eligible']!='true' else '')+'</option>' for c in local)+'</select></label>'
        body+='<button>Show comparison</button></form><p class="muted">Changing the displayed resource or comparison file does not save a Master choice.</p>'
        if candidate:
            member=json.loads(candidate['members_json'])[0];item=r.preview_source('candidate:'+candidate['candidate_id'])
            evidence_html='<section><h2>Technical and content differences</h2><p class="warning">'+esc(candidate['quality_note'])+'</p><p><b>Why this candidate was matched:</b> '+esc(candidate['match_basis'])+'</p>'
            quality=next(x for x in read_csv('quality-comparisons.csv') if x['candidate_id']==candidate['candidate_id'])
            same=member['sha256']==md['sha256']
            rows=[('PDF pages',md['pages'],str(item['pages'])),('Native embedded raster (median ppi)',md['median_ppi'] or 'Not recorded',quality['local_median_ppi'] or 'Not recorded'),('File size',str(round(int(md['size_bytes'])/1048576,3))+' MiB',str(round(int(member['size_bytes'])/1048576,3))+' MiB'),('Stored SHA-256',md['sha256'],member['sha256'])]
            evidence_html+='<p class="'+('good' if same else 'muted')+'">'+('Exact byte copy of the displayed MD resource, according to stored hashes.' if same else 'Stored file hashes differ. This alone does not prove a visual or content difference.')+'</p><div class="table-scroll"><table><tr><th>Evidence</th><th>MD resource</th><th>Local candidate</th></tr>'+''.join('<tr class="'+('changed' if a!=b else '')+'"><th>'+esc(label)+'</th><td>'+esc(a)+'</td><td>'+esc(b)+'</td></tr>' for label,a,b in rows)+'</table></div>'
            if 'AEB-040-page-comparison.json' in candidate['quality_note']:
                evidence_html+='<p><a href="'+esc(url('/file',path=str(ROOT/'AEB-040-page-comparison.json')))+'">Recorded seven-page comparison at 1600 px</a></p>'
            evidence_html+='</section>'
            body+=comparison_tools()+'<div class="compare-grid">'+viewer('resource:'+md['resource_id'],'Museum-digital · resource '+md['resource_id'],int(md['pages']),md['work_path'])+viewer('candidate:'+candidate['candidate_id'],'Local · '+candidate['label'],item['pages'],item['path'])+'</div><section class="diff-result" id="difference-result" hidden><h2>Visual difference</h2><p role="status"></p><canvas aria-label="Difference between displayed page previews"></canvas></section><section id="text-result" hidden aria-live="polite"></section>'
            body+=evidence_html
        else:
            body+=viewer('resource:'+md['resource_id'],'Museum-digital · resource '+md['resource_id'],int(md['pages']),md['work_path'])
        body+='<details><summary>Object metadata and complete resource order</summary><section><h2>Authoritative MD description</h2><pre>'+esc(o['description'])+'</pre><ol>'+''.join('<li><a href="'+esc(url('/object',id=oid,resource=x['resource_id']))+'">'+esc(x['description'] or Path(x['work_path']).name)+'</a> · '+x['pages']+' page(s) · '+esc(x['rights'])+'</li>' for x in rr)+'</ol><p>All resources belong to the MD representation; the displayed page is only a preview.</p></section></details>'
        if candidate:body+='<details><summary>Original candidate path and provenance</summary><p><code>'+esc(member['source_path'])+'</code></p><p>'+esc(candidate['match_basis'])+'</p></details>'
        body+='<form class="decision-panel" method="post" action="/save"><input type="hidden" name="object_id" value="'+esc(oid)+'"><h2>Your Master choice</h2><details><summary>Add or edit a short note</summary><textarea name="note" maxlength="2000" aria-label="Master decision note">'+esc(d['note'])+'</textarea></details><div class="controls"><button name="candidate_id" value="md-'+esc(oid)+'">Accept MD as Master</button>'
        if candidate:
            body+='<button class="secondary" name="candidate_id" value="'+esc(candidate['candidate_id'])+'"'+(' disabled' if candidate['eligible']!='true' else '')+'>Use displayed local file as Master</button>'
        body+='<button class="secondary" name="candidate_id" value="">Decide later / unresolved</button></div><p class="muted">Selecting one complete Master records every other candidate as redundant. '+('The displayed watermarked derivative is comparison evidence and cannot be selected. ' if candidate and candidate['eligible']!='true' else '')+'Save advances to the next pending object. Sources remain unchanged.</p></form>'
        self.send(page(body))
    def local(self,args):
        r=self.server.review;q=args.get('q',[''])[0].lower();view=args.get('view',['context'])[0]
        body='<h1>Local search evidence</h1><section><p>Matches use the existing document hashes, extracted text/OCR and visual index. These are search leads, not automatic unit membership or Master decisions. Broader inventories and mentions are hidden from the default view.</p><form method="get" class="controls"><select name="view"><option value="context"'+(' selected' if view=='context' else '')+'>AEB contextual leads</option><option value="visual"'+(' selected' if view=='visual' else '')+'>Visual leads</option><option value="all"'+(' selected' if view=='all' else '')+'>All search hits (unassigned)</option></select><input name="q" value="'+esc(q)+'" placeholder="Search path or internal text"><button>Filter</button></form></section>'
        if view=='visual':
            if not r.visual:body+='<section><p>No credible visual alternative remains. Three perceptual-hash leads were checked and rejected because their visible content or dimensions did not match.</p><a href="'+esc(url('/file',path=str(ROOT/'visual-search-leads.json')))+'">Recorded comparisons and rejection reasons</a></section>'
            for x in r.visual:
                if q not in str(x).lower():continue
                md=next(z for z in r.resources if z['resource_id']==x['resource_id'])
                body+='<section><a href="'+url('/object',id=x['md_object_id'])+'">MD '+esc(x['md_object_id'])+' / resource '+esc(x['resource_id'])+'</a><p>Perceptual distance '+str(x['distance_total'])+'; local '+str(x['width'])+' × '+str(x['height'])+' pixels. Content and full page coverage remain unconfirmed.</p><div class="grid"><div><b>MD</b><img class="preview" loading="lazy" src="'+esc(url('/file',path=md['preview_path']))+'"></div><div><b>Local lead</b><img class="preview" loading="lazy" src="'+esc(url('/visual-preview',path=x['source_path']))+'"></div></div><a target="_blank" href="'+esc(url('/file',path=x['source_path']))+'">'+esc(x['source_path'])+'</a></section>'
        else:
            visible=[x for x in r.context if (view=='all' or x['relevance']=='collection_context_lead') and q in (x['source_path']+' '+x['header_text']).lower()]
            visible.sort(key=lambda x:(x['relevance']!='collection_context_lead',x['filename']))
            body+='<p>'+str(len(visible))+' source paths. Broader inventories and mentions remain unassigned and outside any drain list.</p>'
            import documents_591 as docs
            document_rows={x['source_path']:x for x in read_csv('documentation-review.csv')}
            candidate_rows={m['source_path']:c for c in r.candidates for m in json.loads(c['members_json'])}
            body+='<div class="cards">'
            for x in visible[:100]:
                document=document_rows.get(x['source_path']);candidate=candidate_rows.get(x['source_path'])
                body+='<article class="card"><h2>'+esc(x['filename'])+'</h2><p class="reason"><b>Why this is here</b><br>'+esc(document['description'] if document else candidate['match_basis'] if candidate else 'Indexed path or internal text matched the bounded AEB search. Unit membership is unconfirmed.')+'</p>'
                preview=document['preview_path'] if document else candidate['preview_path'] if candidate else ''
                if preview:body+='<img class="thumb" loading="lazy" src="'+esc(url('/file',path=preview))+'" alt="Preview of '+esc(x['filename'])+'">'
                body+='<pre class="excerpt">'+docs.highlighted(sys.modules[__name__],docs.excerpt(x['header_text'],500))+'</pre>'
                if document:body+='<p><a class="button" href="'+esc(url('/document',id=document['document_id'],view='all'))+'">Preview and decide relevance</a></p>'
                elif candidate:body+='<p><a class="button" href="'+esc(url('/object',id=candidate['md_object_id'],candidate=candidate['candidate_id']))+'">Compare Master candidates</a></p>'
                else:body+='<p class="muted">Unassigned search evidence; no Master candidate or documentation proposal is recorded.</p>'
                body+='<details><summary>Source path and indexed text</summary><p><a href="'+esc(url('/file',path=x['source_path']))+'" target="_blank"><code>'+esc(x['source_path'])+'</code></a></p><pre>'+esc(x['header_text'][:12000])+'</pre></details></article>'
            body+='</div>'
            if len(visible)>100:body+='<p>First 100 shown. Narrow the search to inspect remaining source paths.</p>'
        self.send(page(body))
    def file(self,value):
        p=Path(value).resolve(strict=True);root=ROOT.resolve();assert p.is_file()
        if not p.is_relative_to(root):
            guard();assert p in self.server.review.allowed_sources and p.is_relative_to(SOURCE.resolve())
        size=p.stat().st_size;start=0;end=size-1;status=200
        if self.headers.get('Range'):
            m=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers['Range']);assert m
            start=int(m[1]);end=min(end,int(m[2]) if m[2] else end);assert 0<=start<=end;status=206
        self.send_response(status);self.send_header('Content-Type',mimetypes.guess_type(p.name)[0] or 'application/octet-stream');self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
        if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        with p.open('rb') as f:
            f.seek(start);remaining=end-start+1
            while remaining:
                block=f.read(min(1048576,remaining))
                if not block:break
                self.wfile.write(block);remaining-=len(block)
    def log_message(self,*args):pass

def main():
    p=argparse.ArgumentParser();p.add_argument('--check',action='store_true');p.add_argument('--port',type=int,default=8791);a=p.parse_args();r=Review();print(json.dumps(r.check(),indent=2),flush=True)
    if a.check:return
    server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler);server.review=r
    print(f'AEB workbench: http://127.0.0.1:{a.port}',flush=True);server.serve_forever()
if __name__=='__main__':main()
