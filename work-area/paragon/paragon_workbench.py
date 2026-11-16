#!/usr/bin/env python3
"""Nine-record Paragon review; existing catalogue logbook and readable decisions.

No accession, source writes, persistent image derivatives, or schema changes.
The legacy catalogue's source-candidate decisions remain visible and are reused.
"""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / 'missing-originals-review.json'
DB = ROOT / 'work/catalog.sqlite'
DECISIONS = ROOT / 'master-decisions.json'
ARCHIVE = Path('/mnt/volume/iss-archive/units/paragon')
SOURCE = Path('/media/vladimir/Elements/restore/archive')
SCOPE = REPORT.name
LOCK = threading.Lock()
DRAIN_REVIEW = False
RETAINED_REVIEW = False


def enable_drain_review():
    global DRAIN_REVIEW, REPORT, DECISIONS, SCOPE
    import drain_candidates
    DRAIN_REVIEW = True
    REPORT = drain_candidates.REPORT
    DECISIONS = drain_candidates.DECISIONS
    SCOPE = REPORT.name


def mount_guard():
    result = subprocess.run(
        ['findmnt', '-rn', '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS',
         '/media/vladimir/Elements'], capture_output=True, text=True, check=True)
    fields = result.stdout.strip().split()
    if (len(fields) != 4 or fields[:3] !=
            ['/dev/sdf1', '/media/vladimir/Elements', 'ntfs3'] or
            'ro' not in fields[3].split(',') or 'rw' in fields[3].split(',')):
        raise ValueError('The required read-only Elements mount is unavailable.')
    return result.stdout.strip()


@contextmanager
def connect(readonly=True):
    con = sqlite3.connect(f'file:{DB}?mode={"ro" if readonly else "rw"}', uri=True)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def history(con):
    candidates, saved = {}, {}
    for row in con.execute("SELECT * FROM logbook WHERE action='review_source_candidate' "
                           'ORDER BY at_utc, log_id'):
        data = json.loads(row['after_json'])
        entity = data.get('entity_id')
        if data.get('evidence_id'):
            candidates.setdefault(entity, {})[data['evidence_id']] = {
                **data, 'actor': row['actor'], 'note': row['note'],
                'reviewed_at': row['at_utc']}
        review = data.get('object_review')
        if review and review.get('source_report') == SCOPE:
            saved[review['inventory']] = review
    return candidates, saved


def load_objects(con):
    if DRAIN_REVIEW:
        import drain_candidates
        return drain_candidates.load_objects(con, history)
    report = json.loads(REPORT.read_text())
    prior, saved = history(con)
    with (ARCHIVE / 'metadata/objects.csv').open(newline='') as f:
        catalogue = {r['archival_label']: r for r in csv.DictReader(f)}
    # User-confirmed working corrections; the accession catalogue stays read-only.
    for correction in report.get('metadata_corrections', []):
        ref = catalogue[correction['inventory']]
        fields = correction['accepted_fields']
        if ref['iss_id'] != correction['entity_id'] or set(fields) != {
                'descriptive_title', 'date_year'}:
            raise ValueError('Unexpected reference metadata correction')
        title = con.execute('SELECT descriptive_title FROM archive_objects WHERE entity_id=?',
                            (ref['iss_id'],)).fetchone()
        accepted = con.execute(
            "SELECT after_json FROM logbook WHERE action='accept_reconciliation_field' "
            'AND entity_id=?', (correction['decision_id'],)).fetchone()
        if (not title or title[0] != fields['descriptive_title'] or not accepted or
                json.loads(accepted[0]).get('value_text') != fields['descriptive_title']):
            raise ValueError('Readable reference correction differs from SQLite')
        if fields['date_year'] not in fields['descriptive_title']:
            raise ValueError('Reference year differs from its corrected title')
        ref.update(fields)
    objects = []
    for raw in report['objects']:
        inv = raw['inventory']
        obj = {**raw, 'candidates': [], 'references': []}
        obj['catalogue'] = catalogue[inv]
        entity = obj['catalogue']['iss_id']
        active = con.execute('SELECT status FROM archive_objects WHERE entity_id=?',
                             (entity,)).fetchone()
        if not active or active[0] != 'active':
            raise ValueError(f'{inv}: current catalogue identity is not active')
        old = prior.get(entity, {})
        assigned = set()
        for candidate in raw['candidates']:
            if not candidate.get('exists') or not candidate.get('decoded'):
                continue
            item = dict(candidate)
            match = next((v for k, v in old.items() if k not in assigned and
                          (k == item['source_path'] or
                           v.get('content_digest') == item['stored_sha256'])), None)
            evidence = match['evidence_id'] if match else item['source_path']
            assigned.add(evidence)
            item.update(evidence_id=evidence, prior=match)
            obj['candidates'].append(item)
        selected = [c for c in obj['candidates'] if c['prior'] and
                    c['prior']['state'] == 'selected_master']
        if len(selected) > 1:
            raise ValueError(f'{inv}: conflicting existing Master decisions')
        obj['prior_reviews'] = list(old.values())
        obj['decision'] = saved.get(inv)
        obj['selected_path'] = (obj['decision']['selected_master_path'] if obj['decision']
                                else selected[0]['source_path'] if selected else '')
        obj['note'] = (obj['decision']['note'] if obj['decision'] else
                       '\n'.join(dict.fromkeys(v['note'] for v in old.values() if v['note'])))
        obj['status'] = ('Master selected' if obj['selected_path'] else
                         'Unresolved · note saved' if obj['decision'] else
                         'Prior duplicate ruling' if inv == 'VLL-1013' and old else
                         'Unresolved')
        labels = (['VLV-1013'] if inv == 'VLL-1013' else
                  ['VLV-1092', 'VLV-1092-1'] if inv == 'VLV-1092-2' else [])
        for label in labels:
            ref = catalogue[label]
            path = (ARCHIVE / ref['access_file']).resolve()
            if not path.is_relative_to(ARCHIVE) or not ref['access_file'] or not path.is_file():
                raise ValueError(f'{label}: comparison reference is unavailable')
            obj['references'].append({**ref, 'source_path': str(path)})
        objects.append(obj)
    if len(objects) != 9 or sum(len(o['candidates']) for o in objects) != 19:
        raise ValueError('Expected exactly nine records and nineteen readable candidates')
    return objects


def verify_candidate(candidate):
    if DRAIN_REVIEW:
        import drain_candidates
        return drain_candidates.verify_candidate(candidate)
    path = Path(candidate['source_path'])
    if not path.resolve().is_relative_to(SOURCE):
        raise ValueError('Candidate is outside the approved source root')
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (candidate['size_bytes'], candidate['mtime_ns']):
        raise ValueError('Source evidence changed; recheck this candidate before review')
    return path


def build_review(obj, selected, note, actor):
    if obj.get('comparison_only'):
        raise ValueError('These views show different parts of the carrier. The raw scan remains unresolved; a Master choice cannot mark it redundant.')
    if not isinstance(selected, str) or not isinstance(note, str) or len(note) > 2000:
        raise ValueError('Choose one Master or leave unresolved, with a note of at most 2000 characters')
    if selected and selected not in {c['source_path'] for c in obj['candidates']}:
        raise ValueError('The selected file does not belong to this object')
    chosen = next((c for c in obj['candidates'] if c['source_path'] == selected), None)
    if chosen and not chosen['decoded']:
        raise ValueError('An unreadable candidate cannot be selected as Master; leave unresolved with a note')
    states = []
    for c in obj['candidates']:
        state = ('selected_master' if c['source_path'] == selected else 'redundant'
                 if selected or (c['prior'] and c['prior']['state'] == 'redundant')
                 else 'candidate')
        states.append({key: c[key] for key in
                       ['source_path', 'evidence_id', 'stored_sha256', 'size_bytes',
                        'mtime_ns', 'format', 'width', 'height']} | {'state': state})
        if DRAIN_REVIEW:
            states[-1]['members'] = c.get('members', [])
    if sum(c['state'] == 'selected_master' for c in states) != bool(selected):
        raise ValueError('Exactly one Master is required for a resolved object')
    review = {'inventory': obj['inventory'], 'entity_id': obj['catalogue']['iss_id'],
            'source_report': SCOPE, 'state': 'selected_master' if selected else 'unresolved',
            'selected_master_path': selected, 'candidates': states, 'note': note.strip(),
            'actor': actor, 'reviewed_at': datetime.now(timezone.utc).isoformat(),
            'accession_authorized': False, 'deletion_authorized': False}
    if DRAIN_REVIEW:
        review['selected_master_members'] = chosen.get('members', [chosen]) if chosen else []
    return review


def record_review(con, obj, review):
    """Use the existing review_source_candidate logbook format; no schema changes."""
    entity = review['entity_id']
    unit = con.execute('SELECT u.unit_id,u.status FROM units u JOIN entity_unit_memberships m '
                       "ON m.unit_id=u.unit_id WHERE m.entity_id=? AND m.role='primary'",
                       (entity,)).fetchone()
    if not unit or unit['status'] != 'open':
        raise ValueError('Review requires an existing open work unit')
    rows = []
    for c in review['candidates']:
        rows.append({'entity_id': entity, 'evidence_id': c['evidence_id'],
                     'content_digest': c['stored_sha256'],
                     # Retain the legacy JPEG role name. The UI reports actual pixels.
                     'candidate_role': 'master_tiff_candidate' if c['format'] == 'TIFF'
                     else 'high_resolution_jpeg_candidate',
                     'source_name': Path(c['source_path']).name, 'source_path': c['source_path'],
                     'state': c['state']})
    active_ids = {c['evidence_id'] for c in review['candidates']}
    for old in obj['prior_reviews']:
        if old['evidence_id'] not in active_ids and old['state'].startswith('selected_'):
            rows.append({key: old[key] for key in ['entity_id', 'evidence_id',
                         'content_digest', 'candidate_role', 'source_name']} |
                        {'state': 'redundant' if review['selected_master_path'] else 'candidate'})
    if not rows:
        rows = [{'entity_id': entity}]
    rows[0]['object_review'] = review
    for payload in rows:
        con.execute('INSERT INTO logbook (log_id,at_utc,actor,unit_id,action,entity_type,'
                    'entity_id,before_json,after_json,evidence,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    ('LOG-' + uuid4().hex.upper(), review['reviewed_at'], review['actor'],
                     unit['unit_id'], 'review_source_candidate', 'reconciliation_decision',
                     'SOURCE-CANDIDATE-' + uuid4().hex.upper(), '{}',
                     json.dumps(payload, ensure_ascii=False), payload.get('content_digest', ''),
                     review['note']))


def readable_decisions(con):
    objects = load_objects(con)
    return {'source_report': SCOPE, 'catalogue': 'work/catalog.sqlite',
            'objects': [{
                'inventory': o['inventory'], 'catalogue': o['catalogue'],
                'selected_master_path': o['selected_path'], 'status': o['status'],
                'note': o['note'], 'current_review': o['decision'],
                'candidate_decisions': [c['prior'] for c in o['candidates'] if c['prior']],
            } for o in objects], 'accession_authorized': False, 'deletion_authorized': False}


def export_decisions(con):
    payload = json.dumps(readable_decisions(con), ensure_ascii=False, indent=2) + '\n'
    temporary = DECISIONS.with_suffix('.json.tmp')
    with temporary.open('w') as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, DECISIONS)


def check():
    mount = mount_guard()
    with connect() as con:
        objects = load_objects(con)
        for obj in objects:
            for candidate in obj['candidates']:
                verify_candidate(candidate)
            if DRAIN_REVIEW:
                for reference in obj['references']:
                    verify_candidate(reference)
        if DECISIONS.exists() and json.loads(DECISIONS.read_text()) != readable_decisions(con):
            raise ValueError('Readable decisions differ from SQLite; export must be refreshed')
    result = {'ok': True, 'mount': mount, 'objects': len(objects),
            'candidates': sum(len(o['candidates']) for o in objects),
            'saved_masters': sum(bool(o['selected_path']) for o in objects),
            'reference_views': sum(len(o['references']) for o in objects),
            'decisions_agree': DECISIONS.exists(), 'drain_review': DRAIN_REVIEW,
            'saved_reviews': sum(bool(o['decision']) for o in objects)}
    if RETAINED_REVIEW:
        import retained_candidates
        with connect() as con:
            result['retained_review'] = retained_candidates.check(con)
    return result


HTML = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paragon · Master review</title>
<style>
:root{font-family:system-ui,sans-serif;color:#24352f;background:#f3f2ec;font-size:16px}*{box-sizing:border-box}body{margin:0}button,textarea,select{font:inherit}button,select{cursor:pointer}button{padding:11px 16px;border:1px solid #a9b4ad;border-radius:7px;background:white;color:#24352f}button.primary{background:#245c47;color:white;border-color:#245c47}button:disabled{opacity:.5;cursor:wait}a{color:#255f49}header{padding:20px 30px;background:#fff;border-bottom:1px solid #d9ded6;display:flex;justify-content:space-between;gap:20px;align-items:center}h1{font-size:23px;margin:0}header p{margin:5px 0 0;color:#5c6c62;font-size:14px}.layout{display:grid;grid-template-columns:240px minmax(0,1fr);max-width:1850px;margin:auto}nav{padding:24px 16px;position:sticky;top:0;align-self:start}nav button{display:block;width:100%;text-align:left;margin-bottom:8px;padding:12px;border-color:transparent;background:transparent}nav button.active{background:#e0eadd;border-color:#72957f}nav small{display:block;font-size:12px;margin-top:4px;color:#52695b}main{padding:24px 30px;min-width:0}h2{margin:5px 0 12px;font-size:26px}h3{font-size:16px;margin:0 0 10px}.eyebrow{color:#486150;font-size:13px;letter-spacing:.08em;text-transform:uppercase}.why{background:#fff5dc;border-left:4px solid #b58b32;padding:16px 20px;margin:18px 0;line-height:1.55}.why p{margin:6px 0}.metadata{line-height:1.6;max-width:1050px}.metadata p{margin:8px 0}.muted{color:#607165;font-size:14px}.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.card{padding:17px;background:white;border:1px solid #d9ded6;border-radius:10px;min-width:0}.card.selected{outline:3px solid #397454}.picture{display:flex;align-items:center;justify-content:center;height:370px;background:#e9eae4;border-radius:5px;margin:12px 0;overflow:hidden}.picture img{max-width:100%;max-height:100%;object-fit:contain}.pick{display:flex;align-items:center;gap:10px;font-weight:600}.pick input{width:20px;height:20px;accent-color:#245c47}.badge{display:inline-block;font-size:12px;padding:3px 7px;border-radius:4px;background:#ecf0e9;margin:4px 5px 4px 0}.path{font-size:12px;overflow-wrap:anywhere;line-height:1.5}details{margin-top:12px}summary{cursor:pointer;font-size:14px}.note{background:white;padding:20px;border-radius:9px;margin:22px 0;border:1px solid #d9ded6}textarea{width:100%;min-height:85px;padding:10px;margin-top:10px;border:1px solid #b5beb5;border-radius:5px}footer{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:14px}.message{font-weight:600;font-size:14px;color:#245c47}.error{color:#a22d24}.compare{margin:20px 0}.compare .controls{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}.compare select{padding:8px;max-width:42%}.compare .picture{height:520px}.compare input{vertical-align:middle}dialog{width:95vw;height:94vh;max-width:1600px;border:0;border-radius:9px;padding:15px;background:#f3f2ec}dialog::backdrop{background:#000b}dialog .zoom{height:calc(100% - 64px);overflow:auto;text-align:center;background:#ddd}dialog img{max-width:100%;height:auto}dialog.native img{max-width:none}dialog .bar{display:flex;gap:12px;align-items:center;margin-bottom:14px}.reference{border-top:2px solid #b8c5b5;padding-top:20px;margin-top:26px}.duplicate{color:#78540a}.loading{padding:40px}@media(max-width:1000px){.layout{grid-template-columns:190px minmax(0,1fr)}main{padding:22px 18px}.picture{height:290px}}@media(max-width:740px){header{padding:18px}.layout{display:block}nav{position:static;display:flex;overflow:auto;padding:12px}nav button{min-width:150px;margin:0 6px 0 0}.cards{grid-template-columns:1fr}.picture{height:340px}.compare select{max-width:100%}main{padding:18px}h2{font-size:23px}}
</style>
<header><div><h1>Paragon · Master review</h1><p>Nine representation gaps · surviving scans and saved decisions</p></div><span id="count" class="muted"></span></header>
<div class="layout"><nav id="nav" aria-label="Objects"></nav><main id="main"><p class="loading">Loading review…</p></main></div>
<dialog id="viewer"><div class="bar"><button id="closeViewer">Close</button><button id="zoomToggle">Original pixels</button><span id="viewerLabel"></span></div><div class="zoom"><img id="large" alt="Enlarged comparison image"></div></dialog>
<script>
let data, current=0, dirty=false;const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function fetchData(){const r=await fetch('/api/objects');if(!r.ok)throw Error(await r.text());data=await r.json();}
function imageUrl(o,i,ref=false,full=false){return '/image?inventory='+encodeURIComponent(o.inventory)+'&index='+i+'&reference='+(ref?'1':'0')+'&full='+(full?'1':'0');}
function markDirty(){dirty=true;$('#message').textContent='Unsaved choice';}
function navigate(i){if(dirty&&!confirm('Discard the unsaved choice for this object?'))return;current=i;dirty=false;render();window.scrollTo(0,0);}
function why(o){if(o.catalogue.master_file)return 'The selected Master has been accessioned. The archive catalogue now includes this representation. The saved review note retains the decision and any earlier identity evidence.';
if(o.inventory==='VLL-1013')return 'The current TIFF selection and the earlier duplicate ruling are both retained in the review note. Compare the represented VLV-1013 below for identity evidence.';
if(o.inventory==='VLV-1092-2')return 'No exact image was found. The title says 1964, but the description says 1962. Compare the two represented invitation views below and record what the evidence establishes. Genuine loss is unconfirmed.';
if(o.inventory==='VLV-1130-5')return 'A JPEG Master was selected previously. Two TIFF versions also survive: a full sheet and a cropped image. Compare their content with the photograph described in the catalogue before changing the saved choice.';
if(o.inventory==='VLV-1127')return 'A TIFF Master was selected previously. The final catalogue has no representation, title or description. The saved Master is shown here; record any identity evidence in the note.';
if(o.selected_path&&!o.decision)return 'A Master was selected previously, but the final catalogue has no representation for this record. The saved choice is already selected below.';
if(!o.candidates.some(c=>c.format==='TIFF'))return 'Only small JPEGs survive at the verified source paths. Compare legibility and content. Select the best surviving Master if suitable, or leave unresolved.';
return 'The final catalogue has no representation. These surviving files need comparison with the object description and one Master choice.';}
function sourceLabel(c){let p=c.source_path;if(p.includes('Rohscans'))return 'Raw full-sheet scan';if(p.includes('Einzelbilder'))return 'Cropped image';if(p.includes('Alle Paragon tiffs'))return 'Paragon TIFF';if(p.includes('Zugeordnete Objekte'))return 'Assigned-object JPEG';if(p.includes('Paragon_jpgs-fertig'))return 'Prepared JPEG';return p.split('/').slice(-3,-1).join(' / ');}
function candidateCard(o,c,i){const same=o.candidates.filter(x=>x.stored_sha256===c.stored_sha256).length;return `<article class="card ${o.selected_path===c.source_path?'selected':''}" data-card="${i}"><label class="pick"><input type="radio" name="master" value="${i}" ${o.selected_path===c.source_path?'checked':''}>Use as Master · candidate ${i+1}</label><div class="picture"><img src="${imageUrl(o,i)}" alt="${esc(o.inventory)} candidate ${i+1}"></div><h3>${esc(sourceLabel(c))}</h3><span class="badge">${c.format} · ${c.width} × ${c.height} px</span><span class="badge">${(c.size_bytes/1e6).toFixed(2)} MB</span>${Math.max(c.width,c.height)<=800?'<span class="badge duplicate">Small surviving image</span>':''}${same>1?'<span class="badge">Identical bytes at '+same+' paths</span>':''}<p class="muted">${c.prior?'Saved: '+esc(c.prior.state.replaceAll('_',' '))+' · '+esc(c.prior.reviewed_at.slice(0,10)):'No saved choice'}</p><button type="button" data-zoom="${i}">Inspect image</button><details><summary>Source and recorded hash</summary><p class="path">${esc(c.source_path)}</p><p class="path">${esc(c.stored_sha256)}</p></details></article>`;}
function render(){const o=data.objects[current];$('#count').textContent=data.objects.filter(x=>x.selected_path).length+' saved Masters / 9 records';$('#nav').innerHTML=data.objects.map((x,i)=>`<button class="${i===current?'active':''}" data-nav="${i}">${esc(x.inventory)}<small>${esc(x.status)}</small></button>`).join('');
$('#main').innerHTML=`<div class="eyebrow">Record ${current+1} of 9 · ${esc(o.inventory)}</div><h2>${esc(o.catalogue.descriptive_title||'Title not recorded')}</h2><div class="metadata"><p>${esc(o.catalogue.description||'Description not recorded.')}</p><p class="muted">${esc(o.catalogue.iss_id)} · Year: ${esc(o.catalogue.date_year||'not recorded')} · Dimensions: ${esc(o.catalogue.dimensions||'not recorded')}</p></div><section class="why"><h3>Why this is here</h3><p>${esc(why(o))}</p></section>${o.candidates.length>1?'<details class="compare"><summary>Compare two candidates side by side</summary><div class="controls"><select id="left" aria-label="Left comparison candidate"></select><select id="right" aria-label="Right comparison candidate"></select></div><div id="comparison" class="cards"></div></details>':''}<div class="cards">${o.candidates.map((c,i)=>candidateCard(o,c,i)).join('')}${o.references.map((r,i)=>`<article class="card"><p class="muted">Represented object · comparison reference</p><div class="picture"><img src="${imageUrl(o,i,true)}" alt="${esc(r.archival_label)} represented reference"></div><h3>${esc(r.archival_label)} · ${esc(r.descriptive_title)}</h3><p>${esc(r.description)}</p><p class="muted">${esc(r.dimensions)} · catalogue year ${esc(r.date_year)}</p><button data-ref="${i}">Inspect reference</button></article>`).join('')}</div><section class="note"><h3>Decision</h3><label class="pick"><input type="radio" name="master" value="" ${!o.selected_path?'checked':''}>Leave unresolved</label><label for="note"><p class="muted">Short note: identity, image quality, or the reason for your choice.</p></label><textarea id="note" maxlength="2000">${esc(o.note)}</textarea><p class="muted">Choosing a Master marks the other candidates redundant. Decisions are saved in the Paragon work records.</p><footer><button class="primary" id="save">Save decision</button><button id="next">Next object →</button><span class="message" id="message" role="status"></span></footer></section>`;
document.querySelectorAll('[data-nav]').forEach(b=>b.onclick=()=>navigate(Number(b.dataset.nav)));document.querySelectorAll('input[name=master]').forEach(r=>r.onchange=()=>{markDirty();document.querySelectorAll('[data-card]').forEach(c=>c.classList.toggle('selected',c.dataset.card===r.value));});$('#note').oninput=markDirty;$('#save').onclick=save;$('#next').onclick=()=>navigate((current+1)%data.objects.length);document.querySelectorAll('[data-zoom]').forEach(b=>b.onclick=()=>zoom(Number(b.dataset.zoom),false));document.querySelectorAll('[data-ref]').forEach(b=>b.onclick=()=>zoom(Number(b.dataset.ref),true));
if(o.candidates.length>1){for(const side of ['left','right']){$('#'+side).innerHTML=o.candidates.map((c,i)=>`<option value="${i}">${i+1}: ${esc(sourceLabel(c))} · ${c.width}×${c.height}</option>`).join('');$('#'+side).onchange=compare;}$('#right').value='1';compare();}}
function compare(){const o=data.objects[current];$('#comparison').innerHTML=['left','right'].map(side=>{const i=Number($('#'+side).value);return `<div><div class="picture"><img src="${imageUrl(o,i)}" alt="${side} comparison candidate ${i+1}"></div><button data-compare="${i}">Inspect candidate ${i+1}</button></div>`;}).join('');document.querySelectorAll('[data-compare]').forEach(b=>b.onclick=()=>zoom(Number(b.dataset.compare),false));}
function zoom(i,ref){const o=data.objects[current];$('#large').src=imageUrl(o,i,ref,true);$('#viewerLabel').textContent=ref?o.references[i].archival_label:o.inventory+' · candidate '+(i+1);$('#viewer').classList.remove('native');$('#zoomToggle').textContent='Original pixels';$('#viewer').showModal();}
$('#closeViewer').onclick=()=>$('#viewer').close();$('#zoomToggle').onclick=()=>{const native=$('#viewer').classList.toggle('native');$('#zoomToggle').textContent=native?'Fit image':'Original pixels';};
async function save(){const o=data.objects[current],selected=$('input[name=master]:checked').value;$('#save').disabled=true;$('#message').classList.remove('error');try{const r=await fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':data.token},body:JSON.stringify({inventory:o.inventory,selected_master_path:selected===''?'':o.candidates[Number(selected)].source_path,note:$('#note').value,previous_review:o.decision?.reviewed_at||''})});const result=await r.json();if(!r.ok)throw Error(result.error);await fetchData();dirty=false;render();$('#message').textContent='Decision saved';}catch(e){$('#message').textContent=e.message;$('#message').classList.add('error');$('#save').disabled=false;}}
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});fetchData().then(render).catch(e=>{$('#main').textContent='Review unavailable: '+e.message;});
</script></html>'''


class Handler(BaseHTTPRequestHandler):
    def response(self, data, status=200, content_type='application/json'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',
                                            f'localhost:{self.server.server_port}'}

    def do_GET(self):
        try:
            if not self.valid_host():
                return self.response({'error': 'Localhost access required'}, 403)
            url = urlparse(self.path)
            if RETAINED_REVIEW and url.path in {'/', '/retained', '/retained/'}:
                return self.response((ROOT / 'retained-workbench.html').read_bytes(),
                                     content_type='text/html; charset=utf-8')
            if RETAINED_REVIEW and url.path.startswith('/retained/'):
                import retained_candidates as retained
                mount_guard()
                retained.verify_evidence()
                query = parse_qs(url.query)
                if url.path == '/retained/audit':
                    return self.response((ROOT / 'AUDIT_PARAGON_20260922.md').read_bytes(),
                                         content_type='text/plain; charset=utf-8')
                if url.path == '/retained/api/audit':
                    audit = retained.readjson(ROOT / 'audit-results-20260922.json')
                    return self.response({key: audit[key] for key in
                        ('audited_at', 'pending_question_groups', 'deduplicated_findings')})
                if url.path == '/retained/api/archive-views':
                    inventory = query.get('inventory', [''])[0]
                    if inventory not in retained.evidence()['catalogue']:
                        raise ValueError('Unknown archive inventory')
                    return self.response({'views': [ref | (retained.archive_master_details(ref) or {})
                        for ref in retained.archived_views(inventory)]})
                if url.path == '/retained/similarity':
                    return self.response((ROOT / 'remaining-similarity.html').read_bytes(),
                                         content_type='text/html; charset=utf-8')
                if url.path == '/retained/image':
                    return self.response(retained.image_bytes(query['path'][0],
                                         query.get('full', ['0'])[0] == '1'),
                                         content_type='image/jpeg')
                if url.path in {'/retained/api/hires', '/retained/api/hires-case'}:
                    with LOCK, connect() as con:
                        if url.path.endswith('/hires'):
                            return self.response(retained.hires_index(con) | {'token': self.server.token})
                        return self.response(retained.public_hires_case(con, query['key'][0]))
                if url.path in {'/retained/api/cases', '/retained/api/case'}:
                    with LOCK, connect() as con:
                        if url.path.endswith('/cases'):
                            return self.response(retained.index(con) | {'token': self.server.token})
                        return self.response(retained.get_case(con, query['key'][0]))
                return self.response({'error': 'Not found'}, 404)
            if url.path in {'/', '/drain-review'}:
                page = (ROOT / 'drain-candidates.html').read_bytes() if DRAIN_REVIEW else HTML.encode()
                return self.response(page, content_type='text/html; charset=utf-8')
            if url.path == '/health':
                return self.response(check())
            if url.path == '/api/objects':
                mount_guard()
                with LOCK, connect() as con:
                    return self.response({'objects': load_objects(con), 'token': self.server.token,
                                          'drain_review': DRAIN_REVIEW,
                                          'carrier_rule': json.loads(REPORT.read_text()).get('carrier_rule', {})
                                          if DRAIN_REVIEW else {}})
            if url.path == '/image':
                query = parse_qs(url.query)
                mount_guard()
                with connect() as con:
                    obj = next(o for o in load_objects(con)
                               if o['inventory'] == query['inventory'][0])
                reference = query.get('reference', ['0'])[0] == '1'
                index = int(query['index'][0])
                entries = obj['references'] if reference else obj['candidates']
                if not 0 <= index < len(entries):
                    raise ValueError('Unknown image')
                candidate = entries[index]
                if DRAIN_REVIEW or not reference:
                    verify_candidate(candidate)
                member = int(query.get('member', ['0'])[0])
                members = candidate.get('members', [candidate])
                if not 0 <= member < len(members):
                    raise ValueError('Unknown Master member')
                candidate = members[member]
                path = Path(candidate['source_path'])
                # Images exist only in memory; no preview or derivative is written to disk.
                with Image.open(path) as original:
                    preview = ImageOps.exif_transpose(original).convert('RGB')
                    if query.get('full', ['0'])[0] != '1':
                        preview.thumbnail((1800, 1800))
                    output = io.BytesIO()
                    preview.save(output, format='JPEG', quality=92)
                return self.response(output.getvalue(), content_type='image/jpeg')
            return self.response({'error': 'Not found'}, 404)
        except (ValueError, KeyError, StopIteration, IndexError, OSError,
                subprocess.CalledProcessError) as error:
            return self.response({'error': str(error) or 'Unknown object or source'}, 409)

    def do_POST(self):
        try:
            hires_request = RETAINED_REVIEW and self.path == '/retained/api/hires-decision'
            retained_request = RETAINED_REVIEW and (self.path == '/retained/api/decision' or hires_request)
            if (not self.valid_host() or (self.path != '/api/decision' and not retained_request) or
                    not secrets.compare_digest(self.headers.get('X-Review-Token', ''), self.server.token)):
                return self.response({'error': 'Invalid review request'}, 403)
            origin = self.headers.get('Origin')
            if origin and origin not in {f'http://127.0.0.1:{self.server.server_port}',
                                        f'http://localhost:{self.server.server_port}'}:
                return self.response({'error': 'Origin does not match this workbench'}, 403)
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size < (120000 if retained_request else 16000):
                raise ValueError('Invalid decision size')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('Invalid decision')
            mount_guard()
            if retained_request:
                import retained_candidates as retained
                retained.verify_evidence()
                with LOCK, connect(False) as con:
                    con.execute('BEGIN IMMEDIATE')
                    if hires_request:
                        review = retained.save_hires_review(con, data, self.server.actor)
                        con.commit()
                        retained.export_decisions(con)
                        if json.loads(retained.DECISIONS.read_text()) != retained.readable_decisions(con):
                            raise ValueError('Readable decisions do not match the catalogue')
                        return self.response({'ok': True, 'review': review})
                    obj = retained.get_case(con, data['case_key'])
                    if (obj.get('decision') or {}).get('source_version_pair') or obj.get('review_in_case'):
                        raise ValueError('This source-version choice is recorded in the focused JPEG/TIFF review.')
                    previous = obj['decision']['reviewed_at'] if obj['decision'] else ''
                    if data.get('previous_review', '') != previous:
                        raise ValueError('This review changed in another tab. Reload before saving.')
                    for candidate in obj['sources']:
                        retained.verify_asset(candidate)
                    review = (retained.build_picture_review(obj, data, self.server.actor)
                              if 'source_path' in data else retained.build_review(obj, data, self.server.actor))
                    if 'source_path' in data and review == obj.get('decision'):
                        con.rollback()
                        return self.response({'ok': True, 'review': review, 'unchanged': True})
                    retained.record_review(con, review)
                    con.commit()
                    retained.export_decisions(con)
                    if json.loads(retained.DECISIONS.read_text()) != retained.readable_decisions(con):
                        raise ValueError('Readable decisions do not match the catalogue')
                return self.response({'ok': True, 'review': review})
            with LOCK, connect(False) as con:
                con.execute('BEGIN IMMEDIATE')
                obj = next(o for o in load_objects(con) if o['inventory'] == data['inventory'])
                previous = obj['decision']['reviewed_at'] if obj['decision'] else ''
                if data.get('previous_review', '') != previous:
                    raise ValueError('This decision changed in another tab. Reload before saving.')
                for candidate in obj['candidates']:
                    verify_candidate(candidate)
                review = build_review(obj, data['selected_master_path'], data['note'], self.server.actor)
                record_review(con, obj, review)
                con.commit()
                export_decisions(con)
                if json.loads(DECISIONS.read_text()) != readable_decisions(con):
                    raise ValueError('Readable decisions do not match the catalogue')
            return self.response({'ok': True})
        except (ValueError, KeyError, StopIteration, TypeError, OSError,
                sqlite3.Error, subprocess.CalledProcessError) as error:
            return self.response({'error': str(error) or 'Unknown record'}, 409)


def main():
    global RETAINED_REVIEW
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--actor', default='Vladimir')
    parser.add_argument('--drain-review', action='store_true',
                        help='Review the eighteen alternatives in the existing drain list')
    parser.add_argument('--retained-review', action='store_true',
                        help='Review retained objects and carrier/member coverage; keep earlier reviews available')
    args = parser.parse_args()
    if args.drain_review or args.retained_review:
        enable_drain_review()
    RETAINED_REVIEW = args.retained_review
    if args.check:
        print(json.dumps(check(), indent=2))
        return
    check()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.token = secrets.token_urlsafe(32)
    server.actor = args.actor
    with connect() as con:
        if RETAINED_REVIEW:
            import retained_candidates
            retained_candidates.export_decisions(con)
        else:
            export_decisions(con)
    print(f'Paragon review: http://127.0.0.1:{args.port}/', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
