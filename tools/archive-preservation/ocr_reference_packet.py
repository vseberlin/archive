"""Prepare a local, source-first transcription packet; no inferred acceptance.

Regions may be drawn or corrected on the supplied page images. Exported references
remain drafts unless explicitly checked and attributed by the operator.
"""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path


def packet(manifest, target):
    source=Path(manifest); target=Path(target)
    data=json.loads(source.read_text()); digest=hashlib.sha256(source.read_bytes()).hexdigest()
    for page in data['pages']:
        page['image']=os.path.relpath(page['image'],target.parent)
        page.setdefault('annotation_slots',len(page.get('regions',[])))
    payload=json.dumps(data,ensure_ascii=False).replace('<','\\u003c')
    target.write_text(r'''<!doctype html><meta charset="utf-8"><title>Archive reference transcription</title>
<style>body{font:17px system-ui;margin:24px;background:#f5f3ee;color:#182422}header{max-width:1000px}button,select,input,textarea{font:inherit}button{padding:8px;margin:3px}article{display:grid;grid-template-columns:minmax(400px,1.4fr) minmax(320px,1fr);gap:20px;border-top:2px solid #abc;padding-top:20px;margin:30px 0}.scan{align-self:start;position:sticky;top:8px}.scan img{width:100%;display:block}.scan svg{position:absolute;inset:0;width:100%;height:100%;touch-action:none;cursor:crosshair}fieldset{margin:12px 0;padding:12px}textarea{width:96%;min-height:75px}input.box{width:95%}.active{outline:3px solid #007970}small{display:block}.notice{background:#ffe6ad;padding:12px}label{display:block;margin:8px 0}@media(max-width:850px){article{display:block}.scan{position:relative}}</style>
<header><h1>Check the source, then transcribe</h1><p class="notice">This packet contains provisional references or empty annotation slots. It is not checked ground truth. Do not inspect candidate OCR for final-test pages. Literal historical spelling, numbers, units and subscripts matter.</p>
<p>Select a region, then drag a box on the image. Correct its text and category. Leave absent or illegible material explicitly marked; do not guess. Critical labels use one <code>category|literal token</code> per line. Repeated labels refer to successive occurrences. Export keeps the page bindings and report split.</p>
<label>Operator name <input id="operator" autocomplete="name"></label><button id="export">Download reference JSON</button><span id="status" role="status"></span><p>Draft edits stay in this browser. Download JSON to retain them elsewhere. No archive or catalogue is changed.</p></header><main id="pages"></main>
<script>
const manifest=PAYLOAD, key='archive-reference-DIGEST';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let drafts;try{drafts=JSON.parse(localStorage.getItem(key)||'null')}catch{}
if(!drafts)drafts=manifest.pages.map(p=>Array.from({length:Math.max(p.annotation_slots,p.regions.length)},(_,i)=>{
const r=p.regions[i];return {block:r?.block||p.id+'-region-'+(i+1),region:r?.region||null,text:r?.text||'',category:r?.category||'',critical:(r?.critical||[]).map(t=>t.category+'|'+t.text).join('\n'),checked:false,illegible:false};}));
const categories=['','running_text','caption','formula','quantity','identifier','date','table','handwriting','absent'];
manifest.pages.forEach((p,pi)=>{
const article=document.createElement('article');article.innerHTML=`<div class="scan"><img src="${esc(p.image)}" alt="${esc(p.id)}"><svg viewBox="0 0 1 1" preserveAspectRatio="none"></svg></div><div><h2>${esc(p.id)}</h2><p>${esc(p.object||'Development')} · ${esc(p.split||'development')} · PDF ${esc(p.page?.index||p.page||p.id)}</p><div class="fields"></div></div>`;document.querySelector('#pages').append(article);
let active=0,start=null;const svg=article.querySelector('svg'), fields=article.querySelector('.fields');
const save=()=>{localStorage.setItem(key,JSON.stringify(drafts));document.querySelector('#status').textContent='Draft saved locally';};
const draw=()=>{svg.innerHTML=drafts[pi].map((r,i)=>r.region?`<rect x="${r.region.x}" y="${r.region.y}" width="${r.region.width}" height="${r.region.height}" fill="${i===active?'#00a38322':'none'}" stroke="${i===active?'#007970':'#df5926'}" stroke-width=".002"/>`:'').join('');};
drafts[pi].forEach((r,ri)=>{const f=document.createElement('fieldset');f.innerHTML=`<legend>${esc(r.block)}</legend><button type="button" class="select">Draw / inspect this region</button><label>Category <select>${categories.map(c=>`<option value="${c}" ${r.category===c?'selected':''}>${c||'Choose category'}</option>`).join('')}</select></label><label>Normalized x, y, width, height <input class="box" value="${r.region?['x','y','width','height'].map(k=>r.region[k]).join(', '):''}"></label><label>Literal reference <textarea class="text">${esc(r.text)}</textarea></label><label>Critical tokens <textarea class="critical">${esc(r.critical)}</textarea></label><label><input type="checkbox" class="illegible" ${r.illegible?'checked':''}> Illegible / uncertain: exclude from exact text scoring, retain coverage gap</label><label><input type="checkbox" class="checked" ${r.checked?'checked':''}> I checked this region and transcription against the source</label>`;fields.append(f);
f.querySelector('.select').onclick=()=>{active=ri;fields.querySelectorAll('fieldset').forEach(x=>x.classList.remove('active'));f.classList.add('active');draw();};
f.oninput=e=>{if(!e.target.classList.contains('checked'))f.querySelector('.checked').checked=false;r.category=f.querySelector('select').value;r.text=f.querySelector('.text').value;r.critical=f.querySelector('.critical').value;r.checked=f.querySelector('.checked').checked;r.illegible=f.querySelector('.illegible').checked;const a=f.querySelector('.box').value.split(',').map(Number);r.region=a.length===4&&a.every(Number.isFinite)?Object.fromEntries(['x','y','width','height'].map((k,i)=>[k,a[i]])):null;save();draw();};});
const point=e=>{const b=svg.getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-b.left)/b.width)),Math.max(0,Math.min(1,(e.clientY-b.top)/b.height))]};
svg.onpointerdown=e=>{start=point(e);svg.setPointerCapture(e.pointerId)};
svg.onpointerup=e=>{if(!start)return;const end=point(e),r=drafts[pi][active];r.region={x:Math.min(start[0],end[0]),y:Math.min(start[1],end[1]),width:Math.abs(end[0]-start[0]),height:Math.abs(end[1]-start[1])};r.checked=false;const f=fields.children[active];f.querySelector('.box').value=['x','y','width','height'].map(k=>r.region[k]).map(n=>n.toFixed(5)).join(', ');f.querySelector('.checked').checked=false;start=null;save();draw();};draw();});
document.querySelector('#export').onclick=()=>{try{
const operator=document.querySelector('#operator').value.trim(),out=structuredClone(manifest);out.reference_manifest_sha256='DIGEST';out.metadata_adopted=false;
out.pages.forEach((p,pi)=>{p.regions=drafts[pi].map(r=>{const b=r.region;if(r.checked&&r.category!=='absent'&&(!b||b.x<0||b.y<0||b.width<=0||b.height<=0||b.x+b.width>1.00001||b.y+b.height>1.00001))throw Error('Invalid region '+r.block);if(r.checked&&!r.illegible&&r.category!=='absent'&&!r.text.trim())throw Error('Transcribe or mark uncertainty for '+r.block);if(r.checked&&!r.category)throw Error('Choose category for '+r.block);const t=r.text.normalize('NFC').replace(/\s+/g,' ').trim(),critical=[],next={};for(const line of r.critical.split('\n').filter(Boolean)){const at=line.indexOf('|'),category=line.slice(0,at),text=line.slice(at+1);const start=t.indexOf(text,next[text]||0);if(at<1||!text||start<0)throw Error('Critical label not found in '+r.block);critical.push({category,text,start,end:start+text.length});next[text]=start+text.length;}return {...r,text:r.text,critical,checked:!!(r.checked&&operator),reviewer:r.checked?operator:null};});p.reference_status=p.regions.every(r=>r.checked)?'operator_checked':'pending_operator';});
const blob=new Blob([JSON.stringify(out,null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='archive-reference-edits.json';a.click();URL.revokeObjectURL(url);document.querySelector('#status').textContent='Exported; acceptance still requires complete valid annotations.';
}catch(e){document.querySelector('#status').textContent=e.message}};
</script>'''.replace('PAYLOAD',payload).replace('DIGEST',digest))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('manifest');p.add_argument('output');a=p.parse_args();packet(a.manifest,a.output)
