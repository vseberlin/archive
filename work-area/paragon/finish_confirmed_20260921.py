"""Apply the bounded, visually checked Paragon completion plan to existing records."""
import argparse
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps
from openpyxl import load_workbook
import paragon_workbench as app
import retained_candidates as review
from accession_carrier_section import read_csv, write_csv, stamp, connect, update_workbook, render_range
from accession_reviewed import embed, verify_workbook
from accession_replacements_20260921 import metadata, descriptive_metadata, META
from reconcile_drain_decisions import counters

W, U = review.ROOT, review.ARCHIVE
PLAN = W / 'confirmed-finish-plan-20260921.json'
RECEIPT = 'db-restore/manifests/confirmed-finish-20260921.json'
WORK_RECEIPT = W / Path(RECEIPT).name
MODEL = ['archive_objects', 'entity_names', 'entity_identifiers', 'entity_relations', 'evidence_assertions']
CONTROLS = ['CATALOG.csv', 'CATALOG.xlsx', 'metadata/objects.csv', 'db-restore/RESTORE.csv',
    'db-restore/RESTORE.xlsx', 'db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql',
    'db-restore/manifests/representations.csv', 'db-restore/manifests/source-disposition.csv',
    'db-restore/manifests/drain-list.csv', 'db-restore/manifests/drain-evidence.json',
    'db-restore/manifests/accession-remaining-20260921.json',
    'db-restore/manifests/accession-remaining-20260921.csv',
    'db-restore/manifests/ACCESSION_REMAINING_20260921.md',
    '00_README.md', 'catalog-export-summary.json', 'db-restore/export-summary.json', RECEIPT]


def now(): return datetime.now(timezone.utc).isoformat()
def save(r): review.write_json(WORK_RECEIPT, r)
def sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def source_check(e):
    p = Path(e['source_path'])
    assert p.resolve(strict=True) == p and p.is_relative_to(app.SOURCE) and not p.is_symlink()
    assert stamp(p) == [e['source_size_bytes'], e['source_mtime_ns']], str(p)


def records():
    return {name: read_csv(U / name) for name in ['CATALOG.csv', 'metadata/objects.csv',
        'db-restore/RESTORE.csv', 'db-restore/manifests/representations.csv',
        'db-restore/manifests/source-disposition.csv']}


def prepare():
    app.mount_guard()
    assert not WORK_RECEIPT.exists() and not (U / RECEIPT).exists(), 'Existing completion receipt: inspect or verify, never replay.'
    plan = review.readjson(PLAN)
    assert Counter(p['kind'] for p in plan['files']) == {'section': 20, 'replace': 8, 'redundant': 38}
    review.verify_evidence()
    tables = records()
    objects = {o['archival_label']: o for o in tables['metadata/objects.csv'][1]}
    checks = {x['path']: x for x in read_csv(U / 'db-restore/checksums.csv')[1]}
    evidence = {e['source_path']: e for e in review.readjson(W / 'drain-evidence.json')}
    rows = {r['source_path']: r for r in tables['db-restore/manifests/source-disposition.csv'][1]}
    assert len(rows) == 1419 and counters(list(rows.values()))['candidate_rows'] == 1211
    for name, work in [('source-disposition.csv', 'source-disposition.planned.csv'), ('drain-evidence.json', 'drain-evidence.json')]:
        assert (U / 'db-restore/manifests' / name).read_bytes() == (W / work).read_bytes()
    for e in evidence.values(): source_check(e)
    with app.connect() as c:
        saved = review.saved_reviews(c)
        assert review.readable_decisions(c) == review.readjson(review.DECISIONS)
        assert review.drain_proposals(c) == review.readjson(review.DRAIN_PROPOSALS)
    owners = {f['path']: (key, f) for key, d in saved.items() for f in d['files']}
    assert len({p['source'] for p in plan['files']}) == 66
    pending = review.readjson(review.PENDING_ACCESSION)
    assert len({f['source_path'] for f in pending['files']} & {p['source'] for p in plan['files']}) == 65
    r = dict(authorization=plan['authorization'], started_at=now(), result='prepared', files=plan['files'],
        prior_tables=tables, prior_reviews=saved, prior_pending=pending, source_evidence=evidence,
        prior_work_decisions=review.readjson(review.DECISIONS), log_id='LOG-' + uuid4().hex.upper(),
        source_modified=False, deletion_authorized=False, publication_performed=False,
        original_directories=sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()),
        original_files={str(p.relative_to(U)): stamp(p) for p in U.rglob('*') if p.is_file()},
        prior_databases={}, workbook_runtime='Artifact Tool import failed: WASM SIMD unavailable; existing bundled openpyxl narrow editor used.')
    for db in [W / 'work/catalog.sqlite', U / 'db-restore/RESTORE.sqlite']:
        with connect(db) as c:
            assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not c.execute('PRAGMA foreign_key_check').fetchall()
            r['prior_databases'][str(db)] = {t: [dict(x) for x in c.execute('SELECT * FROM ' + t + ' ORDER BY rowid')] for t in MODEL + ['entity_files']}
    comparisons = review.readjson(W / 'carrier-parts-consideration-20260921.json')
    for d in comparisons['decisions']:
        assert evidence[d['path']]['source_sha256'] == d['source_sha256']
        for v in d['archive_comparisons']:
            assert stamp(v['path']) == [v['size_bytes'], v['mtime_ns']]
            assert stamp(v['master_path']) == [v['master_size_bytes'], v['master_mtime_ns']]
    with connect(U / 'db-restore/RESTORE.sqlite') as c:
        for p in r['files']:
            source = p['source']; e = evidence[source]; key, old_file = owners[source]
            assert old_file['sha256'] == e['source_sha256'] == rows[source]['source_sha256']
            assert not e.get('other_unit_links'), source
            p.update(case_key=key, prior_file=old_file, source_evidence=e, media=[])
            obj = objects[p['inventory']]
            p['entity_id'] = obj['iss_id']
            if p['kind'] == 'redundant': continue
            p['prior_object'] = obj
            with Image.open(source) as im:
                im.load(); assert im.format == 'TIFF' and im.n_frames == 1 and im.mode == 'RGB'
                p['technical'] = dict(width=im.width, height=im.height, mode=im.mode, compression=im.info.get('compression', 'raw'))
            old_master = obj['master_file'].splitlines()[0]
            old_access = obj['access_file'].splitlines()[0]
            p['old_master'], p['old_access'] = old_master, old_access
            base = p['inventory'] + '__' + obj['iss_id']
            if p['kind'] == 'section':
                base += '__section-' + Path(source).stem
                p['part'] = 'section shown in source ' + Path(source).name
            else:
                assert old_master.endswith('__surrogate.tif')
                with Image.open(U / old_master) as im: assert max(im.size) <= 800
                assert min(p['technical']['width'], p['technical']['height']) > 2500
            p['master_path'] = 'masters/' + base + '__master.tif'
            p['access_path'] = 'export/access/' + base + '__access.jpg'
            assert not (U / p['master_path']).exists()
            if p['kind'] == 'section': assert not (U / p['access_path']).exists()
            else: assert p['access_path'] == old_access
            for role, path, old in [('preservation_master', p['master_path'], old_master), ('access_derivative', p['access_path'], old_access)]:
                prior = dict(c.execute("SELECT fo.* FROM file_occurrences fo JOIN entity_files ef USING(occurrence_id) WHERE ef.entity_id=? AND fo.relative_path=? AND ef.status='active'", (obj['iss_id'], old)).fetchone())
                assert prior['content_digest'] == checks[old]['sha256'] and int(checks[old]['size_bytes']) == (U / old).stat().st_size
                assert (U / path).parent.is_dir()
                p['media'].append(dict(path=path, role=role, old_path=old, prior_occurrence=prior,
                    prior_stamp=stamp(U / old), prior_embedded=metadata(U / old),
                    occurrence_id=prior['occurrence_id'] if p['kind'] == 'replace' and path == old else 'OCC-' + uuid4().hex.upper()))
    assert shutil.disk_usage(U).free > 2 * sum(p['source_evidence']['source_size_bytes'] for p in r['files'] if p['media'])
    save(r)
    print(json.dumps(dict(ready=True, selected_sections=20, overview_replacements=8, redundant_sources=38, pending_after=143)))


def all_media(r): return [f for p in r['files'] for f in p['media']]


def make_media(r):
    assert r['result'] == 'prepared'
    app.mount_guard()
    assert review.readjson(review.DECISIONS) == r['prior_work_decisions']
    for p in r['files']:
        if not p['media']: continue
        source_check(p['source_evidence'])
        p['source_embedded'] = metadata(p['source'])
        if p['kind'] == 'section': assert not descriptive_metadata(p['source_embedded']), 'Unexpected source metadata requires reconciliation.'
        for f in p['media']:
            staged = W / ('confirmed-finish-' + Path(f['path']).name)
            assert not staged.exists()
            if f['role'] == 'preservation_master':
                assert not (U / f['path']).exists()
                with Path(p['source']).open('rb') as src, staged.open('xb') as out: shutil.copyfileobj(src, out, 1024 * 1024)
            else:
                with Image.open(U / p['master_path']) as im, staged.open('xb') as out:
                    ImageOps.exif_transpose(im).convert('RGB').save(out, 'JPEG', quality=92, subsampling=0)
            assert stamp(U / f['old_path']) == f['prior_stamp']
            subprocess.run(['exiftool', '-overwrite_original', '-P', '-XMP:All=', '-IPTC:All=',
                '-IFD0:ImageDescription=', '-IFD0:Artist=', '-IFD0:Copyright=', '-TagsFromFile', str(U / f['old_path']),
                '-XMP', '-IPTC', '-IFD0:ImageDescription', '-IFD0:Artist', '-IFD0:Copyright', str(staged)], check=True, capture_output=True)
            assert descriptive_metadata(metadata(staged)) == descriptive_metadata(f['prior_embedded'])
            if p['kind'] == 'section':
                obj = p['prior_object']
                p['embedded'] = dict(iss_id=obj['iss_id'], archival_label=obj['archival_label'],
                    descriptive_title=obj['descriptive_title'] + ' — Teilscan ' + Path(p['source']).stem,
                    description=obj['description'] + '\n\nTeilscan derselben Wandzeitung ' + p['inventory'] + '. Quelldatei: ' + Path(p['source']).name + '. Keine Gesamtansicht.')
                embed(staged, p['embedded'])
            with Image.open(staged) as im:
                im.load(); width, height, mode = im.width, im.height, im.mode
                if f['role'] == 'preservation_master':
                    with Image.open(p['source']) as src:
                        src.load(); assert im.size == src.size and im.mode == src.mode and im.tobytes() == src.tobytes()
                        assert im.getexif().get(274, 1) == src.getexif().get(274, 1)
            if p['kind'] == 'replace' and f['path'] == f['old_path']:
                assert stamp(U / f['path']) == f['prior_stamp']
                os.replace(staged, U / f['path'])
            else:
                assert not (U / f['path']).exists()
                os.rename(staged, U / f['path'])
            # One final payload hash, only after metadata and final placement.
            f.update(sha256=sha(U / f['path']), size_bytes=(U / f['path']).stat().st_size,
                stamp=stamp(U / f['path']), width=width, height=height, mode=mode,
                media_type='image/tiff' if f['role'] == 'preservation_master' else 'image/jpeg',
                compression=p['technical']['compression'] if f['role'] == 'preservation_master' else 'JPEG',
                observed_mtime_utc=datetime.fromtimestamp((U / f['path']).stat().st_mtime, timezone.utc).isoformat(),
                final_embedded=metadata(U / f['path']))
            f['provenance'] = dict(source_path=p['source'], source_sha256=p['source_evidence']['source_sha256'],
                source_size_bytes=p['source_evidence']['source_size_bytes'], source_mtime_ns=p['source_evidence']['source_mtime_ns'],
                inventory=p['inventory'], entity_id=p['entity_id'], master_path=p['master_path'],
                representation_part=p.get('part', 'carrier overview'), derivation='metadata_enriched_selected_master' if f['role'] == 'preservation_master' else 'generated_from_selected_master',
                receipt=RECEIPT, same_physical_specimen_confirmed=True, deletion_authorized=False)
            save(r)
        print('Prepared final media:', p['inventory'], Path(p['source']).name, flush=True)
    r['result'] = 'media_written'; save(r)


def apply_reviews(r):
    updated = copy.deepcopy(r['prior_reviews'])
    for p in r['files']:
        d = updated[p['case_key']]
        f = next(f for f in d['files'] if f['path'] == p['source'])
        assert f == p['prior_file']
        if p.get('preserve_saved_decision'): continue
        f.update(state={'section': 'carrier_section', 'replace': 'selected_master', 'redundant': 'redundant'}[p['kind']],
            note=f.get('note', '') + '\nFinal decision: ' + p['reason'],
            accession_evidence=dict(inventory=p['inventory'], entity_id=p['entity_id'], receipt=RECEIPT,
                reason=p['reason'], evidence_report=p['evidence_report'], deletion_authorized=False))
        if p['kind'] == 'section': f.update(relation='section_of', carrier=p['inventory'])
        d.update(actor='Vladimir; applied by Codex', reviewed_at=r['started_at'])
    r['updated_reviews'] = {k: d for k, d in updated.items() if d != r['prior_reviews'][k]}


def register_archive(c, r):
    assert not c.execute('SELECT 1 FROM logbook WHERE log_id=?', (r['log_id'],)).fetchone()
    for p in r['files']:
        for f in p['media']:
            digest = 'sha256:' + f['sha256']; prior = f['prior_occurrence']
            c.execute('INSERT OR IGNORE INTO content_blobs (content_digest,size_bytes,media_type,width_px,height_px,bit_depth,color_space,compression,page_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (digest, f['size_bytes'], f['media_type'], f['width'], f['height'], 8, f['mode'], f['compression'], 1, r['started_at']))
            if p['kind'] == 'replace' and f['path'] == f['old_path']:
                assert dict(c.execute('SELECT * FROM file_occurrences WHERE occurrence_id=?', (prior['occurrence_id'],)).fetchone()) == prior
                c.execute('UPDATE file_occurrences SET content_digest=?,observed_mtime_utc=?,observed_at_utc=?,note=? WHERE occurrence_id=?',
                    (digest, f['observed_mtime_utc'], r['started_at'], json.dumps(f['provenance'], ensure_ascii=False), f['occurrence_id']))
            else:
                if p['kind'] == 'replace':
                    c.execute("UPDATE entity_files SET status='superseded' WHERE entity_id=? AND occurrence_id=? AND status='active'", (p['entity_id'], prior['occurrence_id']))
                c.execute('INSERT INTO file_occurrences (occurrence_id,unit_id,content_digest,occurrence_kind,relative_path,original_filename,observed_mtime_utc,observed_at_utc,status,note) VALUES (?,?,?,?,?,?,?,?,?,?)', (f['occurrence_id'], 'WF-PARAGON', digest, 'archive', f['path'], Path(f['path']).name,
                    f['observed_mtime_utc'], r['started_at'], 'verified', json.dumps(f['provenance'], ensure_ascii=False)))
                c.execute('INSERT INTO entity_files VALUES (?,?,?,?,?)', (p['entity_id'], f['occurrence_id'], f['role'], 'active', r['started_at']))
    c.execute('UPDATE catalog_state SET catalog_revision=catalog_revision+1,published_revision=published_revision+1')
    c.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)', (r['log_id'], r['started_at'], 'Vladimir', 'WF-PARAGON',
        'finish_confirmed_accession', 'archive_unit', 'WF-PARAGON', '{}', json.dumps(dict(receipt=RECEIPT, files=r['files'], decisions=r['updated_reviews'], deletion_authorized=False), ensure_ascii=False), RECEIPT, r['authorization']))
    assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not c.execute('PRAGMA foreign_key_check').fetchall()


def register(r):
    assert r['result'] == 'media_written'
    apply_reviews(r)
    with connect(U / 'db-restore/RESTORE.sqlite') as c:
        m = sqlite3.connect(':memory:'); m.row_factory = sqlite3.Row; c.backup(m); m.execute('PRAGMA foreign_keys=ON')
        register_archive(m, r); m.close()
    with connect(U / 'db-restore/RESTORE.sqlite', True) as c: register_archive(c, r)
    affected = {p['entity_id'] for p in r['files'] if p['media']}
    with connect(U / 'db-restore/RESTORE.sqlite') as archive, connect(W / 'work/catalog.sqlite', True) as c:
        assert review.saved_reviews(c) == r['prior_reviews']
        for entity in sorted(affected):
            active = [dict(x) for x in archive.execute("SELECT * FROM entity_files WHERE entity_id=? AND status='active' AND role IN ('preservation_master','preservation_surrogate','access_derivative')", (entity,))]
            c.execute("UPDATE entity_files SET status='superseded' WHERE entity_id=? AND status='active' AND role IN ('preservation_master','preservation_surrogate','access_derivative')", (entity,))
            for link in active:
                occurrence = dict(archive.execute('SELECT * FROM file_occurrences WHERE occurrence_id=?', (link['occurrence_id'],)).fetchone())
                blob = dict(archive.execute('SELECT * FROM content_blobs WHERE content_digest=?', (occurrence['content_digest'],)).fetchone())
                for table, row, key in [('content_blobs', blob, 'content_digest'), ('file_occurrences', occurrence, 'occurrence_id')]:
                    columns = list(row)
                    suffix = 'DO NOTHING' if table == 'content_blobs' else 'DO UPDATE SET ' + ','.join(k + '=excluded.' + k for k in columns if k != key)
                    c.execute('INSERT INTO ' + table + ' (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ') ON CONFLICT(' + key + ') ' + suffix, tuple(row.values()))
                c.execute('INSERT INTO entity_files VALUES (?,?,?,?,?) ON CONFLICT(entity_id,occurrence_id,role) DO UPDATE SET status=excluded.status,created_at=excluded.created_at', tuple(link.values()))
        c.execute('UPDATE catalog_state SET catalog_revision=catalog_revision+1,published_revision=published_revision+1')
        c.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)', tuple(archive.execute('SELECT * FROM logbook WHERE log_id=?', (r['log_id'],)).fetchone()))
        for d in r['updated_reviews'].values(): review.record_review(c, d)
    r['result'] = 'registered'; save(r)


def export_records(r):
    assert r['result'] == 'registered'
    tables = copy.deepcopy(r['prior_tables'])
    ch, catalog = tables['CATALOG.csv']; oh, objects = tables['metadata/objects.csv']
    rh, restore = tables['db-restore/RESTORE.csv']; ph, reps = tables['db-restore/manifests/representations.csv']
    for p in r['files']:
        if not p['media']: continue
        obj = next(o for o in objects if o['iss_id'] == p['entity_id'])
        if p['kind'] == 'replace':
            obj['master_file'] = '\n'.join(p['master_path'] if x == p['old_master'] else x for x in obj['master_file'].splitlines())
            obj['master_parts'] = obj['master_parts'].replace('carrier overview (surrogate)', 'carrier overview')
            obj['notes_for_review'] += '; former overview retained as redundant: ' + p['old_master'] + '; ' + RECEIPT
        else:
            obj['master_file'] += '\n' + p['master_path']; obj['access_file'] += '\n' + p['access_path']
            if not obj['master_parts']: obj['master_parts'] = 'carrier overview'
            obj['master_parts'] += '\n' + p['part']
            obj['master_member_count'] = str(len(obj['master_file'].splitlines()))
        for f in p['media']:
            old_c = next(x for x in catalog if x['final_relative_path'] == f['old_path'])
            old_r = next(x for x in restore if x['relative_path'] == f['old_path'] and x['representation_status'] == 'active')
            c, rr = copy.deepcopy(old_c), copy.deepcopy(old_r)
            if p['kind'] == 'replace' and f['path'] == f['old_path']:
                c, rr = old_c, old_r
            else:
                if p['kind'] == 'replace':
                    old_c['file_role'] = 'redundant_candidate'; old_c['notes'] += '; replaced by ' + f['path'] + '; bytes retained; ' + RECEIPT
                    old_r['representation_status'] = 'superseded'
                catalog.append(c); restore.append(rr)
            master = f['role'] == 'preservation_master'
            c.update(file_role=f['role'], final_relative_path=f['path'], master_relative_path=p['master_path'], filename=Path(f['path']).name,
                file_type=Path(f['path']).suffix[1:], size_bytes=str(f['size_bytes']), final_sha256=f['sha256'],
                original_source_path=p['source'] if master else str(U / p['master_path']),
                source_sha256=p['source_evidence']['source_sha256'].removeprefix('sha256:') if master else p['media'][0]['sha256'],
                derivative_of='' if master else p['master_path'], width=str(f['width']), height=str(f['height']),
                provenance_path=c['provenance_path'] + '\n' + RECEIPT, notes=c['notes'] + '; confirmed same physical carrier; ' + RECEIPT)
            if p['kind'] == 'section':
                c.update(description=p['embedded']['description'], sequence=str(len(obj['master_file'].splitlines())),
                    relationship='member of the single selected Master for ' + p['inventory'] + '; part=' + p['part'] if master else 'generated access image of carrier-section Master member',
                    condition='Partial carrier view; separate photograph Masters preserved.')
            else: c['relationship'] = c['relationship'].replace('carrier overview (surrogate)', 'carrier overview')
            rr.update(representation_role=f['role'], representation_status='active', relative_path=f['path'], original_filename=Path(f['path']).name,
                sha256=f['sha256'], size_bytes=str(f['size_bytes']), media_type=f['media_type'], width_px=str(f['width']), height_px=str(f['height']),
                bit_depth='8', color_space=f['mode'], compression=f['compression'], observed_mtime_utc=f['observed_mtime_utc'],
                provenance_note=json.dumps(f['provenance'], ensure_ascii=False))
        master, access = p['media']
        rep = next((x for x in reps if x['master_path'] == p['old_master'] and x['access_path'] == p['old_access']), None)
        if p['kind'] == 'section': rep = dict(rep); reps.append(rep)
        rep.update(source_role='preservation_master', master_path=master['path'], master_sha256='sha256:' + master['sha256'],
            access_path=access['path'], access_sha256='sha256:' + access['sha256'], access_source=master['path'], derivation_status='generated_from_selected_master',
            width_px=str(access['width']), height_px=str(access['height']), generated_by=Path(__file__).name, generated_at=r['started_at'])
    affected = {p['entity_id'] for p in r['files'] if p['media']}
    with connect(U / 'db-restore/RESTORE.sqlite', True) as c:
        c.execute('UPDATE catalog_state SET published_revision=catalog_revision,dirty_since=NULL WHERE singleton=1')
        revision = c.execute('SELECT catalog_revision FROM catalog_state').fetchone()[0]
        for obj in objects:
            if obj['iss_id'] in affected:
                counts = Counter(x[0] for x in c.execute("SELECT role FROM entity_files WHERE entity_id=? AND status='active'", (obj['iss_id'],)))
                obj['representation_summary'] = '; '.join(k + ': ' + str(v) for k, v in sorted(counts.items()))
                obj['notes_for_review'] += '; confirmed carrier accession: ' + RECEIPT
    for row in restore: row['catalog_revision'] = str(revision)
    for name in ['CATALOG.csv', 'metadata/objects.csv', 'db-restore/RESTORE.csv', 'db-restore/manifests/representations.csv']:
        write_csv(U / name, *tables[name])
    r['workbook_checks'] = {'CATALOG.xlsx': update_workbook(U / 'CATALOG.xlsx', [('Catalog', ch, catalog), ('Objects', oh, objects)]),
        'db-restore/RESTORE.xlsx': update_workbook(U / 'db-restore/RESTORE.xlsx', [('Catalog', rh, restore)])}
    wb = load_workbook(U / 'CATALOG.xlsx'); sheet = wb['Objects']
    for ri, obj in enumerate(objects, 2):
        if obj['iss_id'] not in affected: continue
        for field in ['master_file', 'access_file', 'master_parts', 'representation_summary', 'notes_for_review']:
            cell = sheet.cell(ri, oh.index(field) + 1); align = copy.copy(cell.alignment); align.wrap_text = True; cell.alignment = align
        sheet.row_dimensions[ri].height = max(sheet.row_dimensions[ri].height or 23, len(obj['master_file'].splitlines()) * 30 + 25)
    wb.save(U / 'CATALOG.xlsx'); wb.close()
    with connect(U / 'db-restore/RESTORE.sqlite') as c: sql = '\n'.join(c.iterdump()) + '\n'
    m = sqlite3.connect(':memory:'); m.executescript(sql)
    assert m.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not m.execute('PRAGMA foreign_key_check').fetchall(); m.close()
    (U / 'db-restore/RESTORE.sql').write_text(sql)
    r.update(result='exported', catalog_revision=revision, counts=dict(objects=len(objects), represented_objects=sum(bool(o['master_file']) for o in objects),
        master_members=len(reps), access_images=len(reps), documents=10, catalog_rows=len(catalog), restore_rows=len(restore),
        sections_added=20, overviews_replaced=8, derivatives_generated=28, redundant_sources=38))
    save(r)


def finish_register(r):
    assert r['result'] == 'exported'
    fields, rows = copy.deepcopy(r['prior_tables']['db-restore/manifests/source-disposition.csv'])
    plan = {p['source']: p for p in r['files']}
    objects = {o['archival_label']: o for o in read_csv(U / 'metadata/objects.csv')[1]}
    catalog = {c['final_relative_path']: c for c in read_csv(U / 'CATALOG.csv')[1]}
    replaced = {p['old_master']: p for p in r['files'] if p['kind'] == 'replace'}
    changed = []
    for row in rows:
        p = plan.get(row['source_path'])
        if not p and row['final_path'] not in replaced: continue
        before = copy.deepcopy(row)
        if p:
            if p.get('preserve_saved_decision'):
                evidence = p['prior_file']['catalogue_evidence']
                target = evidence.get('master_path')
                if not target: target = evidence['comparison_target']['master_path']
                target = str(Path(target).relative_to(U)) if str(target).startswith(str(U)) else target
            elif p['kind'] in ['replace', 'section']: target = p['master_path']
            elif p.get('coverage_section'):
                target = next(s['master_path'] for s in r['files'] if s['kind'] == 'section' and Path(s['source']).stem == p['coverage_section'])
            else: target = objects[p['inventory']]['master_file'].splitlines()[0]
            row.update(inventory=p['inventory'], source_role='selected_master_candidate' if p['media'] else 'redundant_candidate',
                preservation_status='preserved_as_metadata_enriched_master' if p['media'] else 'covered_by_selected_master',
                preservation_basis=p['reason'], drain_state='candidate_after_separate_review')
        else:
            p = replaced[row['final_path']]; target = p['master_path']
            row.update(preservation_status='covered_by_replacement_master', preservation_basis='Prior source representation replaced by a better confirmed view of the same physical carrier; previous binding retained in ' + RECEIPT)
        row.update(final_path=target, final_sha256='sha256:' + catalog[target]['final_sha256'], exact_copy_proven='false', deletion_authorized='false',
            notes=row['notes'] + '; confirmed completion: ' + RECEIPT + '; source retained, no deletion.')
        changed.append(dict(before=before, after=copy.deepcopy(row)))
    assert counters(rows)['candidate_rows'] == 1276 and counters(rows)['retain_rows'] == 143
    for path in [W / 'source-disposition.planned.csv', U / 'db-restore/manifests/source-disposition.csv']: write_csv(path, fields, rows)
    for path in [W / 'drain-list.csv', U / 'db-restore/manifests/drain-list.csv']:
        write_csv(path, fields, [x for x in rows if x['drain_state'] == 'candidate_after_separate_review'])
    updates = {x['after']['source_path']: x['after'] for x in changed}
    evidence = copy.deepcopy(list(r['source_evidence'].values()))
    for e in evidence:
        if e['source_path'] in updates:
            row = updates[e['source_path']]
            e.update(accession_receipt=RECEIPT, final_path=row['final_path'], final_sha256=row['final_sha256'], preservation_status=row['preservation_status'], deletion_authorized=False)
    for path in [W / 'drain-evidence.json', U / 'db-restore/manifests/drain-evidence.json']: review.write_json(path, evidence)
    pending = copy.deepcopy(r['prior_pending']); pending['files'] = [f for f in pending['files'] if f['source_path'] not in plan]
    current = {**r['prior_reviews'], **r['updated_reviews']}
    by_path = {f['path']: f for d in current.values() for f in d['files']}
    for f in pending['files']:
        assert by_path[f['source_path']]['state'] == f['saved_state']
    assert Counter(f['saved_state'] for f in pending['files']) == {'retain': 101, 'selected_master': 42}
    pending.update(counts=dict(Counter(f['saved_state'] for f in pending['files'])), confirmed_finish_receipt=RECEIPT,
        scope='143 sources remain pending: 101 retained candidates and 42 local TIFF selections require confirmed physical-object or Master binding.',
        physical_specimen_rule='Require the same physical specimen; keep uncertain bindings pending')
    fh, _ = read_csv(W / 'accession-remaining-20260921.csv')
    for root in [W, U / 'db-restore/manifests']:
        review.write_json(root / 'accession-remaining-20260921.json', pending)
        write_csv(root / 'accession-remaining-20260921.csv', fh, [{k: f[k] for k in fh} for f in pending['files']])
        old = (root / 'ACCESSION_REMAINING_20260921.md').read_text()
        detail = old[old.index('## Saved TIFF choices'):].replace('all 208 exact source paths', 'all 143 exact source paths')
        (root / 'ACCESSION_REMAINING_20260921.md').write_text('# Paragon: pending physical-object bindings, 21 September 2026\n\n'
            'Completed 20 carrier sections, eight better carrier overviews and registration of 38 redundant sources. '
            '65 previously pending paths are resolved. **143 paths remain: 101 retained candidates and 42 selected TIFFs.**\n\n'
            'These are full-sheet/cropped-object identity questions, unassigned distinct specimens, album or ledger context needing object association, '
            'and the cross-collection VLV-1144-3 question. No new object identity was inferred from a filename or a failed similarity match. '
            'The stored per-path notes explain the retained content and comparison limitations. Require the same physical specimen; keep uncertain bindings pending.\n\n'
            'Current completion receipt: `' + RECEIPT + '`. The drain register has 1,276 exact paths; deletion remains unauthorized.\n\n' + detail)
    r['source_disposition_updates'] = changed
    r['counts'].update(drain_candidates=1276, pending_paths=143, pending_paths_resolved=65)
    summary = review.readjson(W / 'drain-review-summary.json'); summary.update(counters(rows), confirmed_finish_receipt=RECEIPT,
        current_register='1276 drain candidates; 143 paths pending physical-object/Master binding. No deletion.')
    review.write_json(W / 'drain-review-summary.json', summary)
    review.evidence.cache_clear()
    with app.connect() as c: review.export_decisions(c)
    r['result'] = 'records_written'; save(r)


def verify_core(r):
    app.mount_guard()
    assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()) == r['original_directories']
    changed = set(CONTROLS) | {'db-restore/checksums.csv'} | {f['path'] for f in all_media(r)}
    # SQLite may update its existing shared-memory/WAL files on reads or commits.
    # They are transient database mechanics, not accession payloads or records.
    changed |= {'db-restore/RESTORE.sqlite' + suffix for suffix in ('-shm', '-wal', '-journal')}
    for path, before in r['original_files'].items():
        if path not in changed: assert stamp(U / path) == before, path
    for e in r['source_evidence'].values(): source_check(e)
    tables = records(); ch, catalog = tables['CATALOG.csv']; oh, objects = tables['metadata/objects.csv']; rh, restore = tables['db-restore/RESTORE.csv']
    assert (len(objects), len(catalog), len(restore), len(tables['db-restore/manifests/representations.csv'][1])) == (260, 649, 637, 302)
    verify_workbook(U / 'CATALOG.xlsx', [('Catalog', ch, catalog), ('Objects', oh, objects)])
    verify_workbook(U / 'db-restore/RESTORE.xlsx', [('Catalog', rh, restore)])
    affected = {p['entity_id'] for p in r['files'] if p['media']}
    allowed_fields = {'master_file', 'access_file', 'master_parts', 'master_member_count', 'representation_summary', 'notes_for_review'}
    old_objects = {o['iss_id']: o for o in r['prior_tables']['metadata/objects.csv'][1]}
    for o in objects:
        for key, value in old_objects[o['iss_id']].items():
            if o['iss_id'] not in affected or key not in allowed_fields: assert o[key] == value, (o['archival_label'], key)
    active_sets = []
    for db, baseline in r['prior_databases'].items():
        with connect(db) as c:
            assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not c.execute('PRAGMA foreign_key_check').fetchall()
            for table in MODEL: assert [dict(x) for x in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')] == baseline[table], (db, table)
            links = [dict(x) for x in c.execute('SELECT * FROM entity_files ORDER BY rowid')]
            assert [x for x in links if x['entity_id'] not in affected] == [x for x in baseline['entity_files'] if x['entity_id'] not in affected]
            aset = {}
            for entity in affected:
                actual = {x[0] for x in c.execute("SELECT fo.relative_path FROM entity_files ef JOIN file_occurrences fo USING(occurrence_id) WHERE ef.entity_id=? AND ef.status='active' AND ef.role IN ('preservation_master','preservation_surrogate')", (entity,))}
                o = next(x for x in objects if x['iss_id'] == entity)
                assert actual == set(o['master_file'].splitlines()) and len(actual) == int(o['master_member_count'])
                aset[entity] = sorted(actual)
            active_sets.append(aset)
            for f in all_media(r):
                assert stamp(U / f['path']) == f['stamp']
                assert c.execute('SELECT content_digest FROM file_occurrences WHERE occurrence_id=?', (f['occurrence_id'],)).fetchone()[0] == 'sha256:' + f['sha256']
                assert next(x for x in catalog if x['final_relative_path'] == f['path'])['final_sha256'] == f['sha256']
    assert active_sets[0] == active_sets[1]
    with app.connect() as c:
        assert review.readable_decisions(c) == review.readjson(review.DECISIONS)
        assert review.saved_reviews(c) == {**r['prior_reviews'], **r['updated_reviews']}
        assert review.drain_proposals(c) == review.readjson(review.DRAIN_PROPOSALS)
        assert all(p['in_drain_register'] for p in review.drain_proposals(c)['files'])
    for rel, work in [('source-disposition.csv', 'source-disposition.planned.csv'), ('drain-list.csv', 'drain-list.csv'), ('drain-evidence.json', 'drain-evidence.json'),
                      ('accession-remaining-20260921.csv', 'accession-remaining-20260921.csv'), ('accession-remaining-20260921.json', 'accession-remaining-20260921.json')]:
        assert (U / 'db-restore/manifests' / rel).read_bytes() == (W / work).read_bytes()
    rows = tables['db-restore/manifests/source-disposition.csv'][1]
    assert len(rows) == len({x['source_path'] for x in rows}) == 1419 and all(x['deletion_authorized'] == 'false' for x in rows)
    pending = review.readjson(review.PENDING_ACCESSION)['files']
    assert {f['source_path'] for f in pending} == {x['source_path'] for x in rows if x['drain_state'] != 'candidate_after_separate_review'}
    assert len(pending) == 143
    return dict(ok=True, **r['counts'], source_stamps_unchanged=1419, metadata_names_attribution_and_relationships_preserved=True,
        one_selected_master_per_object_preserved=True, source_modified=False, deletion_authorized=False,
        directory_structure_unchanged=True, sqlite_and_readable_records_agree=True, uncertain_bindings_pending=True)


def finish(r):
    assert r['result'] == 'records_written'
    r['validation'] = verify_core(r)
    intro='## Confirmed candidates finished — 21 September 2026\n\nAccessioned 20 carrier sections and eight better full-carrier overviews. Generated 28 access images from their selected Masters. Registered 38 redundant sources, including the saved invitation interior. 65 previously pending paths are resolved. Existing names, metadata, attribution, relationships and photograph Masters are preserved.\n\nCurrent totals: **260 objects, 259 represented, 302 selected Master members, 302 access images, 10 documents**. The drain list contains **1,276 exact paths**. **143 source paths remain pending: 101 retained candidates and 42 selected TIFFs with unresolved physical-object or Master bindings.** VLV-1092-2 still lacks an identified source. No source deletion or publication.\n\nReceipt: `'+RECEIPT+'`. Current exceptions and exact paths are in `db-restore/manifests/accession-remaining-20260921.json` and `ACCESSION_REMAINING_20260921.md`. Earlier dated counts below are historical.\n\n'
    for path in [U/'00_README.md', W/'00_README.md']:
        title, body=path.read_text().split('\n',1);path.write_text(title+'\n\n'+intro+body)
    r.update(result='passed',completed_at=now(),accession_performed=True,final_payload_hashes_computed_once=56,
        confirmed_accession_complete=True,all_source_bindings_resolved=False)
    save(r);review.write_json(U/RECEIPT,r)
    export=review.readjson(U/'catalog-export-summary.json');export.update(updated_at=r['completed_at'],file_rows=649,object_rows=260,confirmed_finish_receipt=RECEIPT)
    summary=review.readjson(U/'db-restore/export-summary.json');summary.update(updated_at=r['completed_at'],masters=302,access=302,documents=10,
        generated_access=summary.get('generated_access',0)+28,catalog_revision=r['catalog_revision'],
        catalog_state=dict(catalog_revision=r['catalog_revision'],published_revision=r['catalog_revision'],dirty_since=None),
        source_disposition_review=dict(total=1419,candidate_rows=1276,retained_rows=143,receipt=RECEIPT),confirmed_finish_receipt=RECEIPT)
    summary['restore']['rows']=637
    h,checks=read_csv(U/'db-restore/checksums.csv');checks={x['path']:x for x in checks}
    for f in all_media(r):checks[f['path']]=dict(path=f['path'],sha256='sha256:'+f['sha256'],size_bytes=str(f['size_bytes']))
    summary['checksums']=len(set(checks)|set(CONTROLS));review.write_json(U/'db-restore/export-summary.json',summary)
    for path in CONTROLS:
        if path=='catalog-export-summary.json':continue
        checks[path]=dict(path=path,sha256='sha256:'+sha(U/path),size_bytes=str((U/path).stat().st_size))
    for value in export['outputs'].values():value['sha256']=checks[str(Path(value['path']).relative_to(U))]['sha256']
    review.write_json(U/'catalog-export-summary.json',export)
    checks['catalog-export-summary.json']=dict(path='catalog-export-summary.json',sha256='sha256:'+sha(U/'catalog-export-summary.json'),size_bytes=str((U/'catalog-export-summary.json').stat().st_size))
    write_csv(U/'db-restore/checksums.csv',h,sorted(checks.values(),key=lambda x:x['path']))
    print(json.dumps(r['validation'],indent=2),flush=True)


def verify():
    r=review.readjson(U/RECEIPT);assert r==review.readjson(WORK_RECEIPT) and r['result']=='passed'
    result=verify_core(r)
    checks={x['path']:x for x in read_csv(U/'db-restore/checksums.csv')[1]}
    for path in CONTROLS:assert checks[path]['sha256']=='sha256:'+sha(U/path),path
    for f in all_media(r):assert checks[f['path']]['sha256']=='sha256:'+f['sha256']
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');parser.add_argument('--apply',action='store_true');parser.add_argument('--verify',action='store_true');args=parser.parse_args()
    if args.prepare:prepare()
    elif args.apply:
        r=review.readjson(WORK_RECEIPT);make_media(r);register(r);export_records(r);finish_register(r);finish(r)
    elif args.verify:verify()
    else:parser.error('Choose --prepare, --apply, or --verify')
