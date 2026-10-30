"""Accession the 26 visually confirmed better views; retain object metadata."""
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from PIL import Image, ImageOps
import paragon_workbench as app
import retained_candidates as review
from accession_carrier_section import read_csv, write_csv, stamp, connect, update_workbook, render_range
from reconcile_drain_decisions import counters

W, U = review.ROOT, review.ARCHIVE
RECEIPT = 'db-restore/manifests/replacement-accession-20260921.json'
WORK_RECEIPT = W / Path(RECEIPT).name
AUTH = 'Vladimir: ok done. lets finalize accession and form final drain list; apply the approved same-object and better-quality rules.'
META = ['-XMP:All', '-IPTC:All', '-IFD0:ImageDescription', '-IFD0:Artist', '-IFD0:Copyright']
CONTROLS = ['CATALOG.csv', 'CATALOG.xlsx', 'metadata/objects.csv', 'db-restore/RESTORE.csv',
    'db-restore/RESTORE.xlsx', 'db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql',
    'db-restore/manifests/representations.csv', 'db-restore/manifests/source-disposition.csv',
    'db-restore/manifests/drain-evidence.json', 'db-restore/manifests/drain-list.csv',
    'catalog-export-summary.json', 'db-restore/export-summary.json', '00_README.md', RECEIPT]


def save(r): review.write_json(WORK_RECEIPT, r)


def sha(path):
    with path.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def metadata(path):
    d = json.loads(subprocess.check_output(['exiftool', '-j', '-G1', '-s', *META, str(path)], text=True))[0]
    # ExifTool numbers repeated IPTC blocks by physical container location;
    # JPEG and TIFF may expose the same values as IPTC or IPTC2.
    out = {}
    for k, v in d.items():
        if k in ['SourceFile', 'XMP-x:XMPToolkit'] or k.endswith((':ApplicationRecordVersion', ':EnvelopeRecordVersion')): continue
        key = re.sub(r'^IPTC\d*:', 'IPTC:', k)
        assert key not in out or out[key] == v, ('conflicting repeated metadata', key)
        out[key] = v
    return out


def descriptive_metadata(d):
    # Conflicting historical software packets are preserved in the receipt.
    # They are not object metadata and must not block generation of a fresh
    # access image. All other known and unknown descriptive fields must match.
    technical = {'XMP-dc:Format', 'XMP-xmp:CreateDate', 'XMP-xmp:ModifyDate',
        'XMP-xmp:MetadataDate', 'XMP-photoshop:ICCProfileName',
        'XMP-photoshop:ColorMode', 'XMP-photoshop:LegacyIPTCDigest'}
    return {k: v for k, v in d.items() if not k.startswith('XMP-xmpMM:') and k not in technical}


def prepare():
    app.mount_guard()
    assert not (U / RECEIPT).exists(), 'Accession already completed; use verification only.'
    if WORK_RECEIPT.exists():
        r = review.readjson(WORK_RECEIPT)
        assert r['result'] == 'prepared', 'An interrupted apply needs inspection before continuing.'
        return r
    review.verify_evidence()
    with app.connect() as con:
        assert review.readable_decisions(con) == review.readjson(review.DECISIONS)
    groups = defaultdict(list)
    for d in review.readjson(W / 'picture-review-20260921.json')['decisions']:
        if d['state'] == 'retain' and d['review_group'] == 'matched' and d['review_index'] not in [29, 32, 40]:
            groups[Path(d['comparison_path']).name.split('__')[0]].append(d)
    assert len(groups) == 26 and sum(map(len, groups.values())) == 32
    headers, checks = read_csv(U / 'db-restore/checksums.csv'); checks = {x['path']: x for x in checks}
    _, catalog = read_csv(U / 'CATALOG.csv'); _, restore = read_csv(U / 'db-restore/RESTORE.csv')
    objects = []
    for inventory, choices in sorted(groups.items()):
        # TIFF is the full-frame prepared scan; where only JPEG survives, use
        # the visually matched corrected photograph in its original encoding.
        choices.sort(key=lambda d: (Path(d['path']).suffix.lower() not in ['.tif', '.tiff'],
            'Winkel-korrigiert' not in d['path'], d['path']))
        selected = choices[0]; source = review.evidence()['assets'][selected['path']]
        for d in choices: review.verify_asset(review.evidence()['assets'][d['path']])
        assert not source.get('other_unit_links'), selected['path']
        obj = copy.deepcopy(review.evidence()['catalogue'][inventory])
        old_master, access = obj['master_file'].splitlines()[0], selected['comparison_path'].removeprefix(str(U) + '/')
        assert access in obj['access_file'].splitlines()
        with Image.open(U / old_master) as im: old_size = list(im.size)
        with Image.open(source['path']) as im:
            im.load(); assert im.mode in ['L', 'RGB'] and getattr(im, 'n_frames', 1) == 1
            technical = dict(size=list(im.size), mode=im.mode, format=im.format, compression=im.info.get('compression', 'JPEG'))
        assert technical['size'][0] * technical['size'][1] > old_size[0] * old_size[1]
        extension = '.tif' if technical['format'] == 'TIFF' else '.jpg'
        master = 'masters/' + inventory + '__' + obj['iss_id'] + '__master' + extension
        assert master == old_master or not (U / master).exists(), master
        members = obj['master_file'].splitlines()
        members[members.index(old_master)] = master
        o = dict(inventory=inventory, entity_id=obj['iss_id'], source=source, choices=choices,
            prior_object=obj, old_master=old_master, master=master, access=access,
            selected_master_members=members, technical=technical, old_master_size=old_size,
            metadata_policy='Preserve all existing descriptive and embedded XMP/IPTC/attribution metadata, including blank fields; retain identities and relationships.',
            media=[])
        for kind, old, new in [('master', old_master, master), ('access', access, access)]:
            c = next(copy.deepcopy(x) for x in catalog if x['final_relative_path'] == old)
            rr = next(copy.deepcopy(x) for x in restore if x['relative_path'] == old and x['representation_status'] == 'active')
            with connect(U / 'db-restore/RESTORE.sqlite') as con:
                f = dict(con.execute('SELECT fo.* FROM file_occurrences fo JOIN entity_files ef USING(occurrence_id) WHERE ef.entity_id=? AND fo.relative_path=? AND ef.status=\'active\'', (obj['iss_id'], old)).fetchone())
            assert f['content_digest'] == checks[old]['sha256'] and int(checks[old]['size_bytes']) == (U / old).stat().st_size
            o['media'].append(dict(kind=kind, path=new, old_path=old, prior_catalog=c, prior_restore=rr,
                prior_occurrence=f, prior_stamp=stamp(U / old), prior_checksum=checks[old],
                prior_embedded=metadata(U / old), occurrence_id=f['occurrence_id'] if new == old else 'OCC-' + uuid4().hex.upper(),
                staged=str(W / ('accession-20260921-' + inventory + '-' + kind + Path(new).suffix))))
        objects.append(o)
    assert shutil.disk_usage(U).free > sum(o['source']['size_bytes'] for o in objects) * 3
    with connect(U / 'db-restore/RESTORE.sqlite') as con:
        invariants = {t: [dict(x) for x in con.execute('SELECT * FROM ' + t + ' ORDER BY rowid')] for t in
            ['archive_objects', 'entity_names', 'entity_identifiers', 'entity_relations', 'evidence_assertions']}
    r = dict(authorization=AUTH, started_at=datetime.now(timezone.utc).isoformat(), result='prepared',
        objects=objects, accession_performed=False, deletion_authorized=False, source_modified=False,
        publication_performed=False, log_id='LOG-' + uuid4().hex.upper(), prior_model=invariants,
        original_directories=sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()),
        original_files={str(p.relative_to(U)): stamp(p) for p in U.rglob('*') if p.is_file() and not p.name.endswith(('-wal', '-shm', '-journal'))},
        decision_stamp=stamp(review.DECISIONS))
    save(r)
    return r


def stage(r):
    for o in r['objects']:
        review.verify_asset(o['source'])
        for f in o['media']:
            dest = Path(f['staged'])
            if 'sha256' in f:
                assert stamp(dest) == f['stamp']
                continue
            assert not dest.exists() or r['result'] == 'prepared', str(dest)
            if f['kind'] == 'master':
                shutil.copyfile(o['source']['path'], dest)
            else:
                with Image.open(o['media'][0]['staged']) as im:
                    ImageOps.exif_transpose(im).convert('RGB').save(dest, 'JPEG', quality=92, subsampling=0)
            # Existing object metadata is authoritative; clear source descriptors
            # so an absent old value stays absent instead of leaking another label.
            subprocess.run(['exiftool', '-overwrite_original', '-P', '-XMP:All=', '-IPTC:All=',
                '-IFD0:ImageDescription=', '-IFD0:Artist=', '-IFD0:Copyright=',
                '-TagsFromFile', str(U / f['old_path']), '-XMP', '-IPTC',
                '-IFD0:ImageDescription', '-IFD0:Artist', '-IFD0:Copyright', str(dest)], check=True, capture_output=True)
            after_metadata = metadata(dest)
            assert descriptive_metadata(after_metadata) == descriptive_metadata(f['prior_embedded']), (o['inventory'], f['kind'], 'descriptive metadata differs')
            f['final_embedded'] = after_metadata
            f['technical_metadata_changes'] = {k: dict(before=f['prior_embedded'].get(k), after=after_metadata.get(k))
                for k in set(f['prior_embedded']) | set(after_metadata) if f['prior_embedded'].get(k) != after_metadata.get(k)}
            with Image.open(dest) as im:
                im.load(); size, mode = im.size, im.mode
                if f['kind'] == 'master':
                    with Image.open(o['source']['path']) as src:
                        src.load(); assert im.mode == src.mode and im.size == src.size and im.tobytes() == src.tobytes()
                        assert im.getexif().get(274, 1) == src.getexif().get(274, 1)
            f.update(sha256=sha(dest), stamp=stamp(dest), size_bytes=dest.stat().st_size,
                width=size[0], height=size[1], mode=mode,
                role='preservation_master' if f['kind'] == 'master' else 'access_derivative',
                media_type='image/tiff' if dest.suffix == '.tif' else 'image/jpeg',
                compression=o['technical']['compression'] if f['kind'] == 'master' else 'JPEG',
                observed_mtime_utc=datetime.fromtimestamp(dest.stat().st_mtime, timezone.utc).isoformat(),
                provenance=dict(source_path=o['source']['path'], source_sha256=o['source']['sha256'],
                    source_size_bytes=o['source']['size_bytes'], source_mtime_ns=o['source']['mtime_ns'],
                    master_path=o['master'], prior_path=f['old_path'], prior_sha256=f['prior_checksum']['sha256'],
                    derivation='metadata_enriched_selected_master' if f['kind'] == 'master' else 'generated_from_selected_master',
                    metadata_authority='Existing accessioned object; all embedded descriptive metadata preserved',
                    receipt=RECEIPT, deletion_authorized=False))
            save(r)
        print('Prepared', o['inventory'], flush=True)
    r['result'] = 'staged'; save(r)


def install(r):
    app.mount_guard(); assert stamp(review.DECISIONS) == r['decision_stamp']
    for o in r['objects']:
        review.verify_asset(o['source'])
        for f in o['media']:
            if r['result'] == 'staged':
                assert stamp(U / f['old_path']) == f['prior_stamp']
                assert stamp(Path(f['staged'])) == f['stamp']
            else:
                assert r['result'] == 'installing' and stamp(U / f['path']) == f['stamp']
    if r['result'] == 'staged':
        r['result'] = 'installing'; save(r)
        for o in r['objects']:
            for f in o['media']:
                assert f['path'] == f['old_path'] or not (U / f['path']).exists()
                os.replace(f['staged'], U / f['path'])
                assert stamp(U / f['path']) == f['stamp']
            print('Installed', o['inventory'], flush=True)
    # The work index has older unit-relative media references. Preserve its own
    # metadata baseline, then synchronize only these 26 objects' representations.
    if 'prior_work_model' not in r:
        with connect(W / 'work/catalog.sqlite') as con:
            r['prior_work_model'] = {t: [dict(x) for x in con.execute('SELECT * FROM ' + t + ' ORDER BY rowid')] for t in r['prior_model']}
            r['prior_work_file_links'] = [dict(x) for x in con.execute('SELECT * FROM entity_files ORDER BY rowid')]
        save(r)
    for db in [U / 'db-restore/RESTORE.sqlite']:
        with connect(db, True) as con:
            if con.execute('SELECT 1 FROM logbook WHERE log_id=?', (r['log_id'],)).fetchone(): continue
            for o in r['objects']:
                for f in o['media']:
                    digest = 'sha256:' + f['sha256']; prior = f['prior_occurrence']; note = json.dumps(f['provenance'], ensure_ascii=False)
                    assert dict(con.execute('SELECT * FROM file_occurrences WHERE occurrence_id=?', (prior['occurrence_id'],)).fetchone()) == prior
                    con.execute('INSERT OR IGNORE INTO content_blobs (content_digest,size_bytes,media_type,width_px,height_px,bit_depth,color_space,compression,page_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (digest, f['size_bytes'], f['media_type'], f['width'], f['height'], 8, f['mode'], f['compression'], 1, r['started_at']))
                    if f['path'] == f['old_path']:
                        con.execute('UPDATE file_occurrences SET content_digest=?,observed_mtime_utc=?,observed_at_utc=?,note=? WHERE occurrence_id=?',
                            (digest, f['observed_mtime_utc'], r['started_at'], note, f['occurrence_id']))
                        con.execute('UPDATE entity_files SET role=? WHERE entity_id=? AND occurrence_id=? AND status=\'active\'', (f['role'], o['entity_id'], f['occurrence_id']))
                    else:
                        con.execute('UPDATE entity_files SET status=\'superseded\' WHERE entity_id=? AND occurrence_id=? AND status=\'active\'', (o['entity_id'], prior['occurrence_id']))
                        con.execute('INSERT INTO file_occurrences (occurrence_id,unit_id,content_digest,occurrence_kind,relative_path,original_filename,observed_mtime_utc,observed_at_utc,status,note) VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (f['occurrence_id'], 'WF-PARAGON', digest, 'archive', f['path'], Path(f['path']).name, f['observed_mtime_utc'], r['started_at'], 'verified', note))
                        con.execute('INSERT INTO entity_files VALUES (?,?,?,?,?)', (o['entity_id'], f['occurrence_id'], f['role'], 'active', r['started_at']))
            con.execute('UPDATE catalog_state SET catalog_revision=catalog_revision+1,published_revision=published_revision+1')
            con.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)', (r['log_id'], r['started_at'], 'Vladimir', 'WF-PARAGON',
                'accession_replacement_masters', 'archive_unit', 'WF-PARAGON', '{}', json.dumps(dict(objects=r['objects'], receipt=RECEIPT, deletion_authorized=False), ensure_ascii=False), RECEIPT, AUTH))
            for t, rows in r['prior_model'].items():
                assert [dict(x) for x in con.execute('SELECT * FROM ' + t + ' ORDER BY rowid')] == rows
            assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not con.execute('PRAGMA foreign_key_check').fetchall()
    with connect(U / 'db-restore/RESTORE.sqlite') as archive, connect(W / 'work/catalog.sqlite', True) as con:
        if not con.execute('SELECT 1 FROM logbook WHERE log_id=?', (r['log_id'],)).fetchone():
            for o in r['objects']:
                active = [dict(x) for x in archive.execute("SELECT * FROM entity_files WHERE entity_id=? AND status='active' AND role IN ('preservation_master','preservation_surrogate','access_derivative')", (o['entity_id'],))]
                con.execute("UPDATE entity_files SET status='superseded' WHERE entity_id=? AND status='active' AND role IN ('preservation_master','preservation_surrogate','access_derivative')", (o['entity_id'],))
                for link in active:
                    occurrence = dict(archive.execute('SELECT * FROM file_occurrences WHERE occurrence_id=?', (link['occurrence_id'],)).fetchone())
                    blob = dict(archive.execute('SELECT * FROM content_blobs WHERE content_digest=?', (occurrence['content_digest'],)).fetchone())
                    columns = list(blob)
                    con.execute('INSERT OR IGNORE INTO content_blobs (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', tuple(blob.values()))
                    columns = list(occurrence)
                    con.execute('INSERT INTO file_occurrences (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ') ON CONFLICT(occurrence_id) DO UPDATE SET ' + ','.join(k + '=excluded.' + k for k in columns if k != 'occurrence_id'), tuple(occurrence.values()))
                    con.execute('INSERT INTO entity_files VALUES (?,?,?,?,?) ON CONFLICT(entity_id,occurrence_id,role) DO UPDATE SET status=excluded.status,created_at=excluded.created_at', tuple(link.values()))
            con.execute('UPDATE catalog_state SET catalog_revision=catalog_revision+1,published_revision=published_revision+1')
            log = tuple(archive.execute('SELECT * FROM logbook WHERE log_id=?', (r['log_id'],)).fetchone())
            con.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)', log)
            for t, rows in r['prior_work_model'].items(): assert [dict(x) for x in con.execute('SELECT * FROM ' + t + ' ORDER BY rowid')] == rows
            affected = {o['entity_id'] for o in r['objects']}
            assert [dict(x) for x in con.execute('SELECT * FROM entity_files ORDER BY rowid') if x['entity_id'] not in affected] == [x for x in r['prior_work_file_links'] if x['entity_id'] not in affected]
            assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not con.execute('PRAGMA foreign_key_check').fetchall()
    r['result'] = 'media_and_database_installed'; save(r)


def exports(r):
    oh, objects = read_csv(U / 'metadata/objects.csv'); ch, catalog = read_csv(U / 'CATALOG.csv')
    rh, restore = read_csv(U / 'db-restore/RESTORE.csv'); ph, reps = read_csv(U / 'db-restore/manifests/representations.csv')
    with connect(U / 'db-restore/RESTORE.sqlite') as con:
        revision = con.execute('SELECT catalog_revision FROM catalog_state').fetchone()[0]
        sql = '\n'.join(con.iterdump()) + '\n'
    for o in r['objects']:
        ob = next(x for x in objects if x['iss_id'] == o['entity_id']); assert ob == o['prior_object']
        ob['master_file'] = '\n'.join(o['selected_master_members'])
        ob['master_parts'] = ob['master_parts'].replace('carrier overview (surrogate)', 'carrier overview')
        counts = Counter()
        with connect(U / 'db-restore/RESTORE.sqlite') as con:
            counts.update(x[0] for x in con.execute('SELECT role FROM entity_files WHERE entity_id=? AND status=\'active\'', (o['entity_id'],)))
        ob['representation_summary'] = '; '.join(k + ': ' + str(v) for k, v in sorted(counts.items()))
        if o['master'] != o['old_master']:
            ob['notes_for_review'] += '; former representation retained as redundant: ' + o['old_master'] + '; ' + RECEIPT
        for f in o['media']:
            c = next(x for x in catalog if x['final_relative_path'] == f['old_path'])
            rr = next(x for x in restore if x['relative_path'] == f['old_path'] and x['representation_status'] == 'active')
            assert c == f['prior_catalog'] and rr == f['prior_restore']
            if f['path'] != f['old_path']:
                c['file_role'] = 'redundant_candidate'; c['notes'] += '; replaced by ' + f['path'] + '; bytes retained; ' + RECEIPT
                rr['representation_status'] = 'superseded'
                c = copy.deepcopy(f['prior_catalog']); catalog.append(c)
                rr = copy.deepcopy(f['prior_restore']); restore.append(rr)
            c.update(file_role=f['role'], final_relative_path=f['path'], master_relative_path=o['master'], filename=Path(f['path']).name,
                file_type=Path(f['path']).suffix[1:], size_bytes=str(f['size_bytes']), final_sha256=f['sha256'],
                original_source_path=o['source']['path'] if f['kind'] == 'master' else str(U / o['master']),
                source_sha256=o['source']['sha256'].removeprefix('sha256:') if f['kind'] == 'master' else o['media'][0]['sha256'],
                derivative_of='' if f['kind'] == 'master' else o['master'], width=str(f['width']), height=str(f['height']),
                notes=f['prior_catalog']['notes'] + '; better same-object view accessioned; prior provenance retained in ' + RECEIPT,
                provenance_path=f['prior_catalog']['provenance_path'] + '\n' + RECEIPT)
            c['relationship'] = c['relationship'].replace('carrier overview (surrogate)', 'carrier overview')
            rr.update(representation_role=f['role'], representation_status='active', relative_path=f['path'],
                original_filename=Path(f['path']).name, sha256=f['sha256'], size_bytes=str(f['size_bytes']), media_type=f['media_type'],
                width_px=str(f['width']), height_px=str(f['height']), bit_depth='8', color_space=f['mode'], compression=f['compression'],
                observed_mtime_utc=f['observed_mtime_utc'], provenance_note=json.dumps(f['provenance'], ensure_ascii=False))
        rep = next(x for x in reps if x['master_path'] == o['old_master'] and x['access_path'] == o['access'])
        rep.update(source_role='preservation_master', master_path=o['master'], master_sha256='sha256:' + o['media'][0]['sha256'],
            access_sha256='sha256:' + o['media'][1]['sha256'], access_source=o['master'], derivation_status='generated_from_selected_master',
            width_px=str(o['media'][1]['width']), height_px=str(o['media'][1]['height']), generated_by=Path(__file__).name, generated_at=r['started_at'])
    for row in restore: row['catalog_revision'] = str(revision)
    for path, h, rows in [('metadata/objects.csv', oh, objects), ('CATALOG.csv', ch, catalog), ('db-restore/RESTORE.csv', rh, restore), ('db-restore/manifests/representations.csv', ph, reps)]:
        write_csv(U / path, h, rows)
    memory = sqlite3.connect(':memory:'); memory.executescript(sql)
    assert memory.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not memory.execute('PRAGMA foreign_key_check').fetchall(); memory.close()
    (U / 'db-restore/RESTORE.sql').write_text(sql)
    r['workbook_checks'] = {'CATALOG.xlsx': update_workbook(U / 'CATALOG.xlsx', [('Catalog', ch, catalog), ('Objects', oh, objects)]),
        'db-restore/RESTORE.xlsx': update_workbook(U / 'db-restore/RESTORE.xlsx', [('Catalog', rh, restore)])}
    r['counts'] = dict(objects=len(objects), represented_objects=sum(bool(x['master_file']) for x in objects),
        representations=len(reps), catalog_rows=len(catalog), restore_rows=len(restore), masters_replaced=26, access_derivatives_generated=26)
    r['catalog_revision'] = revision; r['result'] = 'catalogues_exported'; save(r)


def drain(r):
    fields, rows = read_csv(U / 'db-restore/manifests/source-disposition.csv')
    selected = {o['source']['path']: o for o in r['objects']}
    alternatives = {d['path']: o for o in r['objects'] for d in o['choices'] if d['path'] != o['source']['path']}
    replacements = {o['old_master']: o for o in r['objects']}
    changed = []
    for row in rows:
        path = row['source_path']; o = selected.get(path) or alternatives.get(path) or replacements.get(row['final_path'])
        if not o: continue
        changed.append(dict(before=copy.deepcopy(row)))
        is_selected = path in selected
        row.update(final_path=o['master'], final_sha256='sha256:' + o['media'][0]['sha256'], exact_copy_proven='false',
            preservation_status='preserved_as_metadata_enriched_master' if is_selected else 'covered_by_replacement_master',
            preservation_basis='Selected source pixels verified in the final Master.' if is_selected else 'Same complete object visually compared; previous representation and its provenance are documented in the accession receipt.',
            notes=row['notes'] + '; replacement accession: ' + RECEIPT + '; deletion remains unauthorized.')
        if path in selected or path in alternatives:
            row.update(source_role='selected_master_candidate' if is_selected else 'redundant_candidate', drain_state='candidate_after_separate_review', deletion_authorized='false')
        changed[-1]['after'] = copy.deepcopy(row)
    for path in [U / 'db-restore/manifests/source-disposition.csv', W / 'source-disposition.planned.csv']: write_csv(path, fields, rows)
    for path in [U / 'db-restore/manifests/drain-list.csv', W / 'drain-list.csv']:
        write_csv(path, fields, [x for x in rows if x['drain_state'] == 'candidate_after_separate_review'])
    updates = {x['after']['source_path']: x['after'] for x in changed}
    evidence = review.readjson(W / 'drain-evidence.json')
    for e in evidence:
        if e['source_path'] in updates:
            row = updates[e['source_path']]
            e.update(accession_receipt=RECEIPT, final_path=row['final_path'], final_sha256=row['final_sha256'], preservation_status=row['preservation_status'])
    for path in [W / 'drain-evidence.json', U / 'db-restore/manifests/drain-evidence.json']: review.write_json(path, evidence)
    stats = counters(rows); assert stats['candidate_rows'] == 1197 and stats['retain_rows'] == 222
    summary = review.readjson(W / 'drain-review-summary.json'); summary.update(stats, updated_at=r['started_at'], replacement_accession=RECEIPT,
        current_register='Work and archive agree: 1197 drain candidates, 222 excluded source paths. 26 confirmed replacement Masters accessioned; unmatched retained material remains excluded. No deletion authorized.')
    review.write_json(W / 'drain-review-summary.json', summary)
    r['source_disposition_updates'] = changed; r['source_register'] = stats; r['result'] = 'drain_updated'; save(r)


def finish(r):
    title, body = (U / '00_README.md').read_text().split('\n', 1)
    (U / '00_README.md').write_text(title + '\n\n## Current accession — 21 September 2026\n\n'
        'Accessioned 26 better views of existing objects and regenerated their 26 access\n'
        'images. Existing object names, descriptions, dates, attribution and relationships\n'
        'were preserved. Former files at different paths remain redundant, with no deletion.\n'
        'The current drain list has 1,197 candidates; 222 source paths remain excluded.\n'
        'Unmatched material and incomplete Master/identity decisions remain pending.\n'
        'Receipt: `' + RECEIPT + '`. Source media remain read-only. Earlier counts below are historical.\n\n' + body)
    cs = review.readjson(U / 'catalog-export-summary.json'); cs.update(updated_at=r['started_at'], replacement_accession=RECEIPT,
        file_rows=r['counts']['catalog_rows'], object_rows=r['counts']['objects'])
    for key in ['catalog_csv', 'object_csv', 'human_xlsx', 'restore_csv', 'restore_xlsx']:
        cs['outputs'][key]['sha256'] = 'sha256:' + sha(Path(cs['outputs'][key]['path']))
    review.write_json(U / 'catalog-export-summary.json', cs)
    h, rows = read_csv(U / 'db-restore/checksums.csv'); checks = {x['path']: x for x in rows}
    es = review.readjson(U / 'db-restore/export-summary.json'); es.update(updated_at=r['started_at'], replacement_accession=RECEIPT,
        catalog_revision=r['catalog_revision'], source_disposition_review=dict(total=1419, candidate_rows=1197, retained_rows=222, receipt=RECEIPT),
        checksums=len(set(checks) | set(CONTROLS) | {f['path'] for o in r['objects'] for f in o['media']}))
    review.write_json(U / 'db-restore/export-summary.json', es)
    r.update(result='passed', completed_at=datetime.now(timezone.utc).isoformat(), accession_performed=True,
        embedded_descriptive_metadata_preserved=True, prior_technical_metadata_recorded_in_receipt=True,
        source_pixels_preserved=True, sql_restore_verified=True,
        directory_structure_unchanged=True, descriptive_metadata_and_relationships_unchanged=True)
    for path in [WORK_RECEIPT, U / RECEIPT]: review.write_json(path, r)
    for o in r['objects']:
        for f in o['media']: checks[f['path']] = dict(path=f['path'], sha256='sha256:' + f['sha256'], size_bytes=str(f['size_bytes']))
    for path in CONTROLS: checks[path] = dict(path=path, sha256='sha256:' + sha(U / path), size_bytes=str((U / path).stat().st_size))
    write_csv(U / 'db-restore/checksums.csv', h, sorted(checks.values(), key=lambda x: x['path']))
    review.evidence.cache_clear()
    with app.connect() as con: review.export_decisions(con)


def verify():
    app.mount_guard(); r = review.readjson(U / RECEIPT)
    assert r == review.readjson(WORK_RECEIPT) and r['result'] == 'passed'
    assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()) == r['original_directories']
    modified = set(CONTROLS) | {'db-restore/checksums.csv'} | {f['path'] for o in r['objects'] for f in o['media']}
    for path, old in r['original_files'].items():
        if path not in modified: assert stamp(U / path) == old, path
    objects = {o['iss_id']: o for o in read_csv(U / 'metadata/objects.csv')[1]}
    for o in r['objects']:
        review.verify_asset(o['source'])
        for f in o['media']:
            assert stamp(U / f['path']) == f['stamp']
            actual_metadata = metadata(U / f['path'])
            assert actual_metadata == f.get('final_embedded', f['prior_embedded'])
            assert descriptive_metadata(actual_metadata) == descriptive_metadata(f['prior_embedded'])
        for key, value in o['prior_object'].items():
            if key not in ['master_file', 'master_parts', 'representation_summary', 'notes_for_review']: assert objects[o['entity_id']][key] == value
    for db in [W / 'work/catalog.sqlite', U / 'db-restore/RESTORE.sqlite']:
        with connect(db) as con:
            assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not con.execute('PRAGMA foreign_key_check').fetchall()
            baseline = r['prior_work_model'] if db == W / 'work/catalog.sqlite' else r['prior_model']
            for t, expected in baseline.items(): assert [dict(x) for x in con.execute('SELECT * FROM ' + t + ' ORDER BY rowid')] == expected
            for o in r['objects']:
                masters = {x[0] for x in con.execute("SELECT fo.relative_path FROM entity_files ef JOIN file_occurrences fo USING(occurrence_id) WHERE ef.entity_id=? AND ef.status='active' AND ef.role IN ('preservation_master','preservation_surrogate')", (o['entity_id'],))}
                assert masters == set(o['selected_master_members'])
    assert (W / 'source-disposition.planned.csv').read_bytes() == (U / 'db-restore/manifests/source-disposition.csv').read_bytes()
    assert (W / 'drain-list.csv').read_bytes() == (U / 'db-restore/manifests/drain-list.csv').read_bytes()
    assert (W / 'drain-evidence.json').read_bytes() == (U / 'db-restore/manifests/drain-evidence.json').read_bytes()
    with app.connect() as con:
        assert review.readable_decisions(con) == review.readjson(review.DECISIONS)
        assert review.drain_proposals(con) == review.readjson(review.DRAIN_PROPOSALS)
    return dict(ok=True, masters_replaced=26, derivatives_generated=26, drain_candidates=1197,
        excluded_source_paths=222, metadata_preserved=True, source_modified=False, deletion_authorized=False)


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--prepare']:
        r = prepare(); print(json.dumps(dict(ready=True, objects=len(r['objects']), selected_sources=26)))
    elif sys.argv[1:] == ['--stage']:
        r = prepare(); stage(r)
    elif sys.argv[1:] == ['--apply-staged']:
        r = review.readjson(WORK_RECEIPT); assert r['result'] == 'staged'
        install(r); exports(r); drain(r); finish(r); print(json.dumps(verify(), indent=2))
    elif sys.argv[1:] == ['--resume-installed']:
        r = review.readjson(WORK_RECEIPT); assert r['result'] == 'installing'
        install(r); exports(r); drain(r); finish(r); print(json.dumps(verify(), indent=2))
    elif sys.argv[1:] == ['--verify']: print(json.dumps(verify(), indent=2))
    else: raise SystemExit('Use --prepare, --stage, --apply-staged or --verify')
