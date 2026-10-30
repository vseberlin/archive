from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from PIL import Image

TOOL_ROOT = Path('/home/vladimir/wp/tools/archive-preservation')
sys.path.insert(0, str(TOOL_ROOT))
import paragon_document_bundle

SOURCE_ROOT = Path('/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz')
OLD_UNIT = Path('/mnt/volume/iss-archive/units/kurt-schwarz')
FINAL_UNIT = Path('/mnt/volume/iss-archive/units/kurt-schwarz-md-final-refresh')
WORK_ROOT = Path('/mnt/volume/iss-archive-work/kurt-schwarz')
RUN_ROOT = WORK_ROOT / 'work/md-finalization/kurt-schwarz-md-final-refresh-001'
PACKAGE = RUN_ROOT / 'package'
WORKBENCH_DB = WORK_ROOT / 'work/simple-workbench/workbench.sqlite'
LEGACY_DB = WORK_ROOT / 'work/reconciliation/unit-reconciliation-v1/reconciliation.sqlite'
LEGACY_PREVIEWS = LEGACY_DB.parent / 'cache/previews'
OBJECT_CENSUS_PREVIEWS = LEGACY_DB.parent / 'cache/object-census-previews'
OLD_REPRESENTATIONS = OLD_UNIT / 'db-restore/manifests/representations.csv'
OLD_DISPOSITIONS = OLD_UNIT / 'db-restore/manifests/source-disposition.csv'
OLD_CATALOG = OLD_UNIT / 'CATALOG.csv'
UNIT_ID = 'kurt-schwarz-md-final-refresh'

REPRESENTATION_FIELDS = (
    'index','completed_at','representation_key','record_keys','inventories','role',
    'source_kind','source_path','source_member','source_sha256','final_sha256',
    'final_relative_path','size_bytes','mtime_ns','copy_policy','basis',
    'relationship','access_restriction',
)
SOURCE_DISPOSITION_FIELDS = (
    'source_path','source_kind','unit','inventory','source_role','final_path',
    'preservation_status','preservation_basis','source_sha256','final_sha256',
    'source_size_bytes','exact_copy_proven','drain_state','deletion_authorized','notes',
)
CHECKSUM_FIELDS = ('path','sha256','size_bytes','content_role')
DECISION_FIELDS = (
    'source_path','group_id','disposition','master_path','note','actor','decided_at',
    'deletion_authorized',
)
GAP_FIELDS = (
    'record_key','inventory','master_relative_path','master_sha256','reason',
)
ALIAS_FIELDS = (
    'alias_record_key','alias_inventory','alias_md_object_id','alias_md_url',
    'alias_title','canonical_record_key','canonical_inventory','relationship',
    'decision_basis',
)
LOSING_MASTERS = {
    '/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/4.Upload/1631719_MF/KS-6-BZ_0427-1.tif',
    '/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/4.Upload/1631719_MF/KS-6-BZ_0427-2.tif',
    '/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/UPLOADS/4.Upload/1631719_MF/KS-6-BZ_0476.tif',
}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def csv_bytes(rows: list[dict], fields: Iterable[str]) -> bytes:
    fields = tuple(fields)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore', quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8')


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp-{os.getpid()}')
    if path.exists() or temporary.exists():
        raise RuntimeError(f'refusing overwrite: {path}')
    with temporary.open('xb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return 'sha256:' + digest.hexdigest()


def create_table(connection: sqlite3.Connection, name: str, rows: list[dict], fields: Iterable[str]) -> None:
    fields = tuple(fields)
    connection.execute(
        f'CREATE TABLE "{name}" (' + ','.join(f'"{field}" TEXT NOT NULL' for field in fields) + ')'
    )
    if rows:
        connection.executemany(
            f'INSERT INTO "{name}" VALUES (' + ','.join('?' for _ in fields) + ')',
            [[str(row.get(field, '')) for field in fields] for row in rows],
        )


def source_is_read_only() -> str:
    line = subprocess.run(
        ['findmnt','-n','-o','SOURCE,TARGET,FSTYPE,OPTIONS','--target',str(SOURCE_ROOT)],
        check=True,capture_output=True,text=True,
    ).stdout.strip()
    options = set(line.split(None,3)[3].split(','))
    if 'ro' not in options or 'rw' in options:
        raise RuntimeError(f'source is not read-only: {line}')
    return line


if RUN_ROOT.exists():
    raise SystemExit(f'run root already exists: {RUN_ROOT}')
if FINAL_UNIT.exists():
    raise SystemExit(f'final candidate already exists: {FINAL_UNIT}')
if OLD_UNIT.stat().st_dev != FINAL_UNIT.parent.stat().st_dev:
    raise SystemExit('old and new unit roots are not on the same filesystem')
source_mount = source_is_read_only()
RUN_ROOT.mkdir(parents=True)
PACKAGE.mkdir()
for directory in (
    PACKAGE/'masters', PACKAGE/'documents', PACKAGE/'export/access',
    PACKAGE/'export/md', PACKAGE/'db-restore/manifests',
):
    directory.mkdir(parents=True)

# Freeze a normal SQLite backup at the closeout boundary.
source_db = sqlite3.connect(WORKBENCH_DB, timeout=30)
backup_db = sqlite3.connect(RUN_ROOT/'workbench-closeout.sqlite')
source_db.backup(backup_db)
backup_db.close()
source_db.row_factory = sqlite3.Row
source_db.execute('PRAGMA foreign_keys=ON')
if source_db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
    raise RuntimeError('workbench quick_check failed')
if source_db.execute('PRAGMA foreign_key_check').fetchall():
    raise RuntimeError('workbench foreign keys failed')
pending = source_db.execute('''SELECT COUNT(*) FROM (
 SELECT g.group_id FROM groups g LEFT JOIN decisions d ON d.path=g.member_path
 GROUP BY g.group_id
 HAVING SUM(CASE WHEN g.is_master_reference=1 THEN 1 ELSE 0 END)>=1
 AND SUM(CASE WHEN g.is_master_reference=0 AND d.path IS NULL THEN 1 ELSE 0 END)>=1)''').fetchone()[0]
if pending:
    raise RuntimeError(f'MD-linked queue is not closed: {pending}')
decisions = [dict(row) for row in source_db.execute(
    'SELECT path,group_id,disposition,master_path,note,actor,decided_at FROM decisions ORDER BY path'
)]
files = {row['path']: dict(row) for row in source_db.execute('SELECT * FROM files')}
source_db.close()
if Counter(row['disposition'] for row in decisions) != Counter({'redundant':2302,'noise':8}):
    raise RuntimeError('decision scope drifted')

old_representations = read_csv(OLD_REPRESENTATIONS)
old_masters = [row for row in old_representations if row['role']=='preservation_master']
if len(old_masters) != 1668:
    raise RuntimeError('frozen Master count drifted')
master_by_source = {row['source_path']: row for row in old_masters}
master_decisions = [row for row in decisions if row['path'] in master_by_source]
if {row['path'] for row in master_decisions} != LOSING_MASTERS:
    raise RuntimeError('duplicate-Master correction scope drifted')
if any(row['disposition']!='redundant' for row in master_decisions):
    raise RuntimeError('duplicate Master is not redundant')
base_retained_masters = [row for row in old_masters if row['source_path'] not in LOSING_MASTERS]
if len(base_retained_masters) != 1665:
    raise RuntimeError('retained Master count drifted')
base_retained_by_source = {row['source_path']: row for row in base_retained_masters}
for row in decisions:
    if row['disposition']=='redundant' and row['master_path'] not in base_retained_by_source:
        raise RuntimeError(f'redundant decision lacks retained Master: {row["path"]}')

old_catalog = read_csv(OLD_CATALOG)
old_catalog_fields = tuple(old_catalog[0])
old_object_by_key = {row['record_key']: row for row in old_catalog}
if any(row['record_keys'] not in old_object_by_key for row in base_retained_masters):
    raise RuntimeError('retained Master lacks object metadata')

legacy = sqlite3.connect(f'file:{LEGACY_DB}?mode=ro', uri=True)
legacy.row_factory = sqlite3.Row
candidate_by_final = {
    row['final_path']: row['candidate_id']
    for row in legacy.execute('SELECT final_path,candidate_id FROM media_candidates')
}
census_candidates = {
    row['candidate_id']: dict(row)
    for row in legacy.execute('SELECT * FROM object_census_candidates')
}
candidate_by_final.update({
    row['final_path']: row['candidate_id']
    for row in census_candidates.values()
})
surrogate_decisions = [dict(row) for row in legacy.execute('''
    SELECT c.candidate_id,c.final_path,c.primary_source_path,c.content_sha256,
           c.size_bytes,c.extension,d.disposition,d.target_record_key,d.actor,
           d.reason,d.decided_at
      FROM object_census_candidates c
      JOIN object_census_decisions d
        ON d.decision_id=(SELECT MAX(d2.decision_id)
                            FROM object_census_decisions d2
                           WHERE d2.candidate_id=c.candidate_id)
     WHERE d.disposition IN ('surrogate_master','surrogate_keep')
     ORDER BY d.target_record_key
''')]
legacy.close()

if Counter(row['disposition'] for row in surrogate_decisions) != Counter({
    'surrogate_master': 27,
    'surrogate_keep': 2,
}):
    raise RuntimeError('confirmed surrogate-decision scope drifted')
if len({row['target_record_key'] for row in surrogate_decisions}) != 29:
    raise RuntimeError('surrogate decisions do not bind 29 distinct objects')

old_representation_by_final = {
    row['final_relative_path']: row for row in old_representations
}
base_master_by_record = {
    row['record_keys']: row for row in base_retained_masters
}
upgrade_by_record: dict[str, dict] = {}
keep_by_record: dict[str, dict] = {}
for decision in surrogate_decisions:
    record_key = decision['target_record_key']
    old_master = base_master_by_record.get(record_key)
    candidate_representation = old_representation_by_final.get(
        decision['final_path']
    )
    candidate_file = (OLD_UNIT / decision['final_path']).resolve(strict=True)
    candidate_file.relative_to(OLD_UNIT.resolve())
    if old_master is None or candidate_representation is None:
        raise RuntimeError(f'unbound surrogate decision: {record_key}')
    if candidate_file.stat().st_size != int(decision['size_bytes']):
        raise RuntimeError(f'surrogate candidate size drifted: {record_key}')
    if candidate_representation['final_sha256'] != decision['content_sha256']:
        raise RuntimeError(f'surrogate candidate digest binding drifted: {record_key}')
    if decision['disposition'] == 'surrogate_keep':
        keep_by_record[record_key] = decision
        continue
    selected = dict(old_master)
    selected.update({
        'representation_key': decision['content_sha256'],
        'source_kind': candidate_representation.get('source_kind', 'file') or 'file',
        'source_path': decision['primary_source_path'],
        'source_member': candidate_representation.get('source_member', ''),
        'source_sha256': decision['content_sha256'],
        'final_sha256': decision['content_sha256'],
        'final_relative_path': decision['final_path'],
        'size_bytes': str(decision['size_bytes']),
        'mtime_ns': str(candidate_file.stat().st_mtime_ns),
        'copy_policy': 'same_filesystem_hardlink_of_validated_archive_bytes',
        'basis': 'curator_confirmed_higher_resolution_master',
        'relationship': 'approved_master_replacement',
    })
    upgrade_by_record[record_key] = {
        'decision': decision,
        'old_master': old_master,
        'selected_master': selected,
    }

if len(upgrade_by_record) != 27 or len(keep_by_record) != 2:
    raise RuntimeError('surrogate projection count drifted')
retained_masters = [
    upgrade_by_record.get(row['record_keys'], {}).get('selected_master', row)
    for row in base_retained_masters
]
retained_by_source = {row['source_path']: row for row in retained_masters}
if len(retained_masters) != 1665 or len(retained_by_source) != 1665:
    raise RuntimeError('selected Master projection is not one-to-one')

built_at = now()
representation_rows: list[dict] = []
checksums: list[dict] = []
master_target_by_source: dict[str,str] = {}
master_digest_by_source: dict[str,str] = {}
access_by_key: dict[str,str] = {}
gaps: list[dict] = []

# Masters are unchanged archive bytes. A same-filesystem hard link makes the
# corrected shallow candidate without rereading or rewriting 399 GB.
for index, row in enumerate(retained_masters, 1):
    inventory = row['inventories'].strip()
    if not inventory or Path(inventory).name != inventory or '/' in inventory or '\\' in inventory:
        raise RuntimeError(f'unsafe inventory: {inventory!r}')
    source = (OLD_UNIT / row['final_relative_path']).resolve(strict=True)
    source.relative_to(OLD_UNIT.resolve())
    extension = Path(row['final_relative_path']).suffix.lower()
    target_rel = f'masters/{inventory}{extension}'
    target = PACKAGE / target_rel
    if target.exists():
        raise RuntimeError(f'Master path collision: {target_rel}')
    os.link(source, target)
    stat = target.stat()
    if stat.st_ino != source.stat().st_ino or stat.st_size != int(row['size_bytes']):
        raise RuntimeError(f'Master hard-link verification failed: {target_rel}')
    item = {
        **{field: row.get(field,'') for field in REPRESENTATION_FIELDS},
        'index': str(index),
        'completed_at': built_at,
        'role': 'preservation_master',
        'final_relative_path': target_rel,
        'copy_policy': 'same_filesystem_hardlink_of_validated_archive_bytes',
        'basis': row.get('basis') or 'frozen_md_master_minus_three_curator_duplicate_corrections',
        'relationship': row.get('relationship') or 'md_closeout_scope',
    }
    representation_rows.append(item)
    checksums.append({'path':target_rel,'sha256':row['final_sha256'],'size_bytes':row['size_bytes'],'content_role':'preservation_master'})
    master_target_by_source[row['source_path']] = target_rel
    master_digest_by_source[row['source_path']] = row['final_sha256']
    if index % 250 == 0 or index == len(retained_masters):
        print(f'masters={index}/{len(retained_masters)}', flush=True)

# Reuse the established preview-v3 output only when its cache key binds it to
# this exact published Master path, size, and mtime. Eight known unreadable TIFFs
# remain explicit derivative gaps.
for position, row in enumerate(retained_masters, 1):
    record_key = row['record_keys']
    inventory = row['inventories']
    old_master = OLD_UNIT / row['final_relative_path']
    candidate_id = candidate_by_final.get(row['final_relative_path'])
    preview = None
    if candidate_id:
        stat = old_master.stat()
        if candidate_id in census_candidates:
            key = hashlib.sha256(
                (f'object-census-preview-v1\0{candidate_id}\0{stat.st_size}\0{stat.st_mtime_ns}').encode()
            ).hexdigest()
            candidate = OBJECT_CENSUS_PREVIEWS / f'{key}.jpg'
        else:
            key = hashlib.sha256(
                (f'jpeg-preview-v3\0{candidate_id}\0{stat.st_size}\0{stat.st_mtime_ns}').encode()
            ).hexdigest()
            candidate = LEGACY_PREVIEWS / f'{key}.jpg'
        if candidate.is_file() and candidate.stat().st_size > 0:
            preview = candidate
    master_rel = master_target_by_source[row['source_path']]
    if preview is None:
        gaps.append({
            'record_key':record_key,
            'inventory':inventory,
            'master_relative_path':master_rel,
            'master_sha256':row['final_sha256'],
            'reason':'Access derivative not generated: selected Master did not decode in the established preview-v3 pipeline; Master retained unchanged.',
        })
        continue
    target_rel = f'export/access/{inventory}.jpg'
    target = PACKAGE / target_rel
    if target.exists():
        raise RuntimeError(f'access derivative collision: {target_rel}')
    shutil.copyfile(preview, target)
    with Image.open(target) as image:
        image.verify()
    with Image.open(target) as image:
        if image.format != 'JPEG' or image.width < 1 or image.height < 1:
            raise RuntimeError(f'invalid access derivative: {target_rel}')
    digest = sha256_file(target)
    stat = target.stat()
    representation_rows.append({
        'index':'',
        'completed_at':built_at,
        'representation_key':digest,
        'record_keys':record_key,
        'inventories':inventory,
        'role':'access_derivative',
        'source_kind':'generated_from_master',
        'source_path':row['source_path'],
        'source_member':master_rel,
        'source_sha256':row['final_sha256'],
        'final_sha256':digest,
        'final_relative_path':target_rel,
        'size_bytes':str(stat.st_size),
        'mtime_ns':str(stat.st_mtime_ns),
        'copy_policy':'copied_validated_preview_v3',
        'basis':'established_preview_v3_bound_to_exact_selected_master',
        'relationship':f'access_derivative_of:{master_rel}',
        'access_restriction':row.get('access_restriction',''),
    })
    checksums.append({'path':target_rel,'sha256':digest,'size_bytes':str(stat.st_size),'content_role':'access_derivative'})
    access_by_key[record_key] = target_rel
    if position % 250 == 0 or position == len(retained_masters):
        print(f'access_checked={position}/{len(retained_masters)} generated={len(access_by_key)} gaps={len(gaps)}', flush=True)

if len(access_by_key) != 1657 or len(gaps) != 8:
    raise RuntimeError(f'derivative scope drifted: {len(access_by_key)} generated, {len(gaps)} gaps')
for index, row in enumerate(representation_rows, 1):
    row['index'] = str(index)

# One object row per retained Master, with no filesystem sidecar nesting.
object_rows=[]
for master in retained_masters:
    old = dict(old_object_by_key[master['record_keys']])
    for obsolete in ('unit','object_directory','object_metadata_path','representation_count'):
        old.pop(obsolete, None)
    old.update({
        'unit':UNIT_ID,
        'master_relative_path':master_target_by_source[master['source_path']],
        'master_sha256':master['final_sha256'],
        'access_relative_path':access_by_key.get(master['record_keys'],''),
        'access_derivative_status':('generated_preview_v3' if master['record_keys'] in access_by_key else 'not_generated_master_decode_failure'),
    })
    object_rows.append(old)
object_fields=tuple(object_rows[0])

# Three curator-confirmed duplicate MD records remain readable aliases of the
# retained archival object instead of silently disappearing from the closeout.
alias_rows=[]
for decision in master_decisions:
    alias_master = master_by_source[decision['path']]
    canonical_master = master_by_source[decision['master_path']]
    alias_object = old_object_by_key[alias_master['record_keys']]
    canonical_object = old_object_by_key[canonical_master['record_keys']]
    alias_rows.append({
        'alias_record_key': alias_master['record_keys'],
        'alias_inventory': alias_master['inventories'],
        'alias_md_object_id': alias_object.get('md_object_id',''),
        'alias_md_url': alias_object.get('md_image_full_url',''),
        'alias_title': alias_object.get('current_title','') or alias_object.get('historical_title',''),
        'canonical_record_key': canonical_master['record_keys'],
        'canonical_inventory': canonical_master['inventories'],
        'relationship': 'same_archival_object_duplicate_md_record',
        'decision_basis': decision['note'] or 'Curator-confirmed duplicate Master relationship.',
    })
if len(alias_rows) != 3:
    raise RuntimeError('duplicate-object alias count drifted')
accounted_record_keys = (
    {row['record_key'] for row in object_rows}
    | {row['alias_record_key'] for row in alias_rows}
)
if accounted_record_keys != {row['record_keys'] for row in old_masters}:
    raise RuntimeError('not all 1,668 MD record relationships are accounted for')

# Catalog is the readable per-archived-file view.
catalog_fields = (
    'record_key','inventory','file_role','final_relative_path','filename','file_type',
    'size_bytes','final_sha256','master_relative_path','access_derivative_status',
    'title','description','date_text','material','dimensions','places','tags',
    'creator','md_object_id','md_url','match_status','match_basis','original_source_path',
)
catalog_rows=[]
object_lookup={row['record_key']:row for row in object_rows}
for rep in representation_rows:
    obj=object_lookup[rep['record_keys']]
    catalog_rows.append({
        'record_key':rep['record_keys'],
        'inventory':rep['inventories'],
        'file_role':rep['role'],
        'final_relative_path':rep['final_relative_path'],
        'filename':Path(rep['final_relative_path']).name,
        'file_type':Path(rep['final_relative_path']).suffix.lower().lstrip('.'),
        'size_bytes':rep['size_bytes'],
        'final_sha256':rep['final_sha256'],
        'master_relative_path':obj['master_relative_path'],
        'access_derivative_status':obj['access_derivative_status'],
        'title':obj.get('current_title','') or obj.get('historical_title',''),
        'description':obj.get('current_description','') or obj.get('historical_description',''),
        'date_text':obj.get('current_date_text','') or obj.get('historical_date_text',''),
        'material':obj.get('current_material','') or obj.get('historical_material',''),
        'dimensions':obj.get('current_dimensions','') or obj.get('historical_dimensions',''),
        'places':obj.get('current_places','') or obj.get('historical_places',''),
        'tags':obj.get('current_tags','') or obj.get('historical_tags',''),
        'creator':'Kurt Schwarz',
        'md_object_id':obj.get('md_object_id',''),
        'md_url':obj.get('md_image_full_url',''),
        'match_status':('matched' if obj.get('md_object_id') else 'no_match_found'),
        'match_basis':obj.get('selection_basis',''),
        'original_source_path':rep['source_path'],
    })

# Project the earlier simple-workbench decisions beneath the newly selected
# Masters, then add the 29 explicit higher-resolution comparison outcomes.
selected_source_for_base_source = {
    item['old_master']['source_path']: item['selected_master']['source_path']
    for item in upgrade_by_record.values()
}
projection_decisions=[]
for decision in decisions:
    projected=dict(decision)
    projected['master_path']=selected_source_for_base_source.get(
        decision['master_path'], decision['master_path']
    )
    projection_decisions.append(projected)
for item in upgrade_by_record.values():
    decision=item['decision']
    old_master=item['old_master']
    selected=item['selected_master']
    group_id=f"object-census:{decision['candidate_id']}"
    projection_decisions.extend((
        {
            'path':old_master['source_path'],'group_id':group_id,
            'disposition':'redundant','master_path':selected['source_path'],
            'note':'Superseded by the curator-confirmed higher-resolution Master.',
            'actor':decision['actor'],'decided_at':decision['decided_at'],
        },
        {
            'path':selected['source_path'],'group_id':group_id,
            'disposition':'selected_master','master_path':selected['source_path'],
            'note':decision['reason'] or 'Curator-confirmed higher-resolution Master.',
            'actor':decision['actor'],'decided_at':decision['decided_at'],
        },
    ))
for record_key, decision in keep_by_record.items():
    old_master=base_master_by_record[record_key]
    projection_decisions.append({
        'path':decision['primary_source_path'],
        'group_id':f"object-census:{decision['candidate_id']}",
        'disposition':'redundant','master_path':old_master['source_path'],
        'note':decision['reason'] or 'Current Master retained after visual comparison.',
        'actor':decision['actor'],'decided_at':decision['decided_at'],
    })
if len({row['path'] for row in projection_decisions}) != len(projection_decisions):
    raise RuntimeError('a source path has conflicting projected decisions')
if Counter(row['disposition'] for row in projection_decisions) != Counter({
    'redundant':2331,'noise':8,'selected_master':27,
}):
    raise RuntimeError('projected decision counts drifted')

# Complete path-specific source register: selected Masters and decisions are
# actionable evidence; everything else remains outside this bounded closeout.
old_dispositions=read_csv(OLD_DISPOSITIONS)
old_by_path={row['source_path']:row for row in old_dispositions}
decision_by_path={row['path']:row for row in projection_decisions}
census_by_source={
    row['primary_source_path']:row for row in census_candidates.values()
}
all_source_paths=list(old_by_path)
for path in set(retained_by_source) | set(decision_by_path):
    if path not in old_by_path:
        all_source_paths.append(path)
source_dispositions=[]
for path in all_source_paths:
    base={field:'' for field in SOURCE_DISPOSITION_FIELDS}
    base.update(old_by_path.get(path,{}))
    base['source_path']=path
    base['unit']=UNIT_ID
    base['deletion_authorized']='false'
    if path in retained_by_source:
        master=retained_by_source[path]
        base.update({
            'source_kind':master.get('source_kind','file') or 'file',
            'inventory':master['inventories'],
            'source_role':'selected_master',
            'final_path':master_target_by_source[path],
            'preservation_status':'preserved_exact_archive_hardlink',
            'preservation_basis':(
                'curator_confirmed_higher_resolution_master'
                if master['record_keys'] in upgrade_by_record
                else 'frozen_MD_master_retained_after_closeout'
            ),
            'source_sha256':master['source_sha256'],
            'final_sha256':master['final_sha256'],
            'source_size_bytes':master['size_bytes'],
            'exact_copy_proven':'true',
            'drain_state':'candidate_after_separate_review',
            'notes':'Accepted Master is present in the corrected shallow unit as unchanged archive bytes. No deletion is authorized.',
        })
    elif path in decision_by_path:
        decision=decision_by_path[path]
        file_row=files.get(path,{})
        census_row=census_by_source.get(path,{})
        old_master_row=master_by_source.get(path,{})
        source_sha256=(
            file_row.get('sha256') or census_row.get('content_sha256')
            or old_master_row.get('source_sha256') or base.get('source_sha256','')
        )
        source_size=(
            file_row.get('size_bytes') or census_row.get('size_bytes')
            or old_master_row.get('size_bytes') or base.get('source_size_bytes','')
        )
        metadata=json.loads(file_row.get('metadata_json') or '{}')
        if decision['disposition']=='redundant':
            keeper=retained_by_source[decision['master_path']]
            exact=source_sha256==keeper['final_sha256']
            base.update({
                'source_kind':'file','inventory':keeper['inventories'],
                'source_role':'redundant',
                'final_path':master_target_by_source[decision['master_path']],
                'preservation_status':'reviewed_redundant_not_accessioned',
                'preservation_basis':'MD-linked reconciliation decision',
                'source_sha256':source_sha256,'final_sha256':keeper['final_sha256'],
                'source_size_bytes':str(source_size),
                'exact_copy_proven':'true' if exact else 'false',
                'drain_state':'redundant_candidate_after_separate_review',
                'notes':(decision['note'] or 'Reviewed as redundant to the selected Master.')+' No deletion is authorized.',
            })
        elif decision['disposition']=='noise':
            base.update({
                'source_kind':'file','inventory':str(metadata.get('current_inventory','')),
                'source_role':'noise','final_path':'',
                'preservation_status':'reviewed_noise_not_accessioned',
                'preservation_basis':'human visual reconciliation decision',
                'source_sha256':source_sha256,'final_sha256':'',
                'source_size_bytes':str(source_size),'exact_copy_proven':'false',
                'drain_state':'noise_candidate_after_separate_review',
                'notes':(decision['note'] or 'Reviewed as Noise.')+' No deletion is authorized.',
            })
        else:
            raise RuntimeError(f'unexpected non-selected disposition for {path}')
    else:
        base.update({
            'unit':UNIT_ID,'final_path':'',
            'preservation_status':'retain_outside_md_closeout_scope',
            'preservation_basis':'not_part_of_frozen_Master_or_MD-linked_decision_scope',
            'final_sha256':'','exact_copy_proven':'false',
            'drain_state':'retain_not_reviewed_in_md_closeout',
            'deletion_authorized':'false',
            'notes':'Outside the bounded MD closeout. Retain for later local/cross-unit review; no deletion is authorized.',
        })
    source_dispositions.append({field:str(base.get(field,'')) for field in SOURCE_DISPOSITION_FIELDS})
if len({row['source_path'] for row in source_dispositions})!=len(source_dispositions):
    raise RuntimeError('source disposition paths are not unique')

source_disposition_by_path={row['source_path']:row for row in source_dispositions}
drain_decisions=[row for row in projection_decisions if row['disposition']!='selected_master']
drain_rows=[source_disposition_by_path[row['path']] for row in drain_decisions]
if len(drain_rows)!=2339 or any(row['deletion_authorized']!='false' for row in drain_rows):
    raise RuntimeError('drain-list scope drifted')
decision_rows=[{
    'source_path':row['path'],'group_id':row['group_id'],'disposition':row['disposition'],
    'master_path':row['master_path'] or '','note':row['note'],'actor':row['actor'],
    'decided_at':row['decided_at'],'deletion_authorized':'false',
} for row in projection_decisions]

# Human-readable controls.
atomic_write(PACKAGE/'CATALOG.csv',csv_bytes(catalog_rows,catalog_fields))
atomic_write(PACKAGE/'CATALOG.xlsx',paragon_document_bundle.table_xlsx_bytes(
    catalog_fields,catalog_rows,sheet_name='Catalog',table_name='Catalog'
))
atomic_write(PACKAGE/'db-restore/RESTORE.csv',csv_bytes(catalog_rows,catalog_fields))
atomic_write(PACKAGE/'db-restore/RESTORE.xlsx',paragon_document_bundle.table_xlsx_bytes(
    catalog_fields,catalog_rows,sheet_name='Restore',table_name='Restore'
))
atomic_write(PACKAGE/'db-restore/checksums.csv',csv_bytes(sorted(checksums,key=lambda r:r['path']),CHECKSUM_FIELDS))
atomic_write(PACKAGE/'db-restore/manifests/representations.csv',csv_bytes(representation_rows,REPRESENTATION_FIELDS))
atomic_write(PACKAGE/'db-restore/manifests/source-disposition.csv',csv_bytes(source_dispositions,SOURCE_DISPOSITION_FIELDS))
atomic_write(PACKAGE/'db-restore/manifests/drain-list.csv',csv_bytes(drain_rows,SOURCE_DISPOSITION_FIELDS))
atomic_write(PACKAGE/'db-restore/manifests/reconciliation-decisions.csv',csv_bytes(decision_rows,DECISION_FIELDS))
atomic_write(PACKAGE/'db-restore/manifests/access-derivative-gaps.csv',csv_bytes(gaps,GAP_FIELDS))
atomic_write(PACKAGE/'db-restore/manifests/object-aliases.csv',csv_bytes(alias_rows,ALIAS_FIELDS))

contents='''# Kurt Schwarz MD closeout documentation\n\nThis corrected unit is deliberately limited to the frozen MD-reconciled media scope.\nNo additional local, cross-unit, contextual, restricted, or project-document records\nwere accessioned in this pass. Those sources remain retained and explicitly blocked\nin `db-restore/manifests/source-disposition.csv` for later review.\n'''
atomic_write(PACKAGE/'documents/CONTENTS.md',contents.encode('utf-8'))
readme=f'''# Kurt Schwarz MD-reconciled archive unit\n\nThis is the corrected shallow proposal for the completed Kurt Schwarz MD closeout.\nIt retains one selected Master per accepted object and excludes all reviewed\nRedundant and Noise files from the payload layout.\n\n- Retained objects and Masters: {len(object_rows)}\n- Generated access derivatives: {len(access_by_key)}\n- Explicit derivative gaps: {len(gaps)}\n- Confirmed higher-resolution Master replacements: {len(upgrade_by_record)}\n- Current Masters retained after comparison: {len(keep_by_record)}\n- Reviewed Redundant files: {sum(row['disposition']=='redundant' for row in projection_decisions)}\n- Reviewed Noise files: {sum(row['disposition']=='noise' for row in projection_decisions)}\n- Duplicate catalogue-record aliases: {len(alias_rows)}\n- Source-disposition rows: {len(source_dispositions)}\n- Drain-list rows: {len(drain_rows)}\n- Deletion authorized: false\n\n## Layout\n\n`masters/` is flat and contains exactly one authoritative file per retained object.\n`export/access/` contains the established JPEG derivative generated from that\nselected Master when the Master decoded. Eight Masters remain unchanged without\nan access derivative because the established preview pipeline could not decode\nthem; see `db-restore/manifests/access-derivative-gaps.csv`.\n\nThe three confirmed duplicate catalogue records remain explicit aliases in\n`db-restore/manifests/object-aliases.csv`; together with the 1,665 canonical\nobjects this accounts for all 1,668 frozen record relationships.\n\nRecovery begins with `db-restore/RESTORE.sqlite` or the paired SQL, CSV, and XLSX\nfiles. Essential object identity, metadata, source provenance, representation\nrelationships, checksums, review decisions, and source disposition remain readable\nwithout SQLite. `drain-list.csv` is review evidence only and never authorizes deletion.\n\nThe previous published `kurt-schwarz` unit and all protected source files were left\nunchanged. This candidate must not replace the prior unit until its controlled\ncutover is explicitly approved.\n'''
atomic_write(PACKAGE/'00_README.md',readme.encode('utf-8'))

# Small rebuildable restore database.
restore_build=RUN_ROOT/'RESTORE.building.sqlite'
connection=sqlite3.connect(restore_build)
try:
    create_table(connection,'objects',object_rows,object_fields)
    create_table(connection,'representations',representation_rows,REPRESENTATION_FIELDS)
    create_table(connection,'source_disposition',source_dispositions,SOURCE_DISPOSITION_FIELDS)
    create_table(connection,'drain_list',drain_rows,SOURCE_DISPOSITION_FIELDS)
    create_table(connection,'reconciliation_decisions',decision_rows,DECISION_FIELDS)
    create_table(connection,'object_aliases',alias_rows,ALIAS_FIELDS)
    create_table(connection,'access_derivative_gaps',gaps,GAP_FIELDS)
    create_table(connection,'checksums',checksums,CHECKSUM_FIELDS)
    connection.commit()
    if connection.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
        raise RuntimeError('restore SQLite integrity failed')
    sql='\n'.join(connection.iterdump())+'\n'
finally:
    connection.close()
restore_build.replace(PACKAGE/'db-restore/RESTORE.sqlite')
atomic_write(PACKAGE/'db-restore/RESTORE.sql',sql.encode('utf-8'))

# Validate before atomic candidate publication.
if len(list((PACKAGE/'masters').glob('*')))!=1665 or any(path.is_dir() for path in (PACKAGE/'masters').iterdir()):
    raise RuntimeError('flat Master layout validation failed')
if len(list((PACKAGE/'export/access').glob('*.jpg')))!=1657:
    raise RuntimeError('access derivative count validation failed')
if len(representation_rows)!=3322 or len(checksums)!=3322 or len(catalog_rows)!=3322:
    raise RuntimeError('representation/catalog/checksum count mismatch')
if len(object_rows)!=1665 or len(gaps)!=8 or len(drain_rows)!=2339:
    raise RuntimeError('object/gap/drain count mismatch')
if any(row['deletion_authorized']!='false' for row in source_dispositions+drain_rows+decision_rows):
    raise RuntimeError('deletion authorization detected')
unwanted=[path for path in PACKAGE.rglob('*') if path.is_symlink() or '.tmp-' in path.name]
if unwanted:
    raise RuntimeError(f'unwanted package paths: {unwanted[:3]}')
restore=sqlite3.connect(f'file:{PACKAGE/"db-restore/RESTORE.sqlite"}?mode=ro',uri=True)
expected={'objects':1665,'representations':3322,'source_disposition':17077,'drain_list':2339,'reconciliation_decisions':2366,'object_aliases':3,'access_derivative_gaps':8,'checksums':3322}
for table,count in expected.items():
    actual=restore.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    if actual!=count: raise RuntimeError(f'{table} count {actual} != {count}')
if restore.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('restore quick_check failed')
restore.close()
source_mount_after=source_is_read_only()
validation={
    'format':'iss-kurt-schwarz-md-final-validation-v1','validated_at':now(),'result':'ok',
    'candidate_unit':str(FINAL_UNIT),'source_mount':source_mount_after,'source_read_only':True,
    'objects':1665,'masters':1665,'access_derivatives':1657,'access_derivative_gaps':8,
    'representations':3322,'checksums':3322,'source_disposition_rows':17077,
    'drain_list_rows':2339,'redundant_decisions':2331,'noise_decisions':8,
    'selected_master_decisions':27,'retained_master_comparisons':2,
    'confirmed_master_replacements':27,'duplicate_master_corrections':3,
    'canonical_record_relationships':1668,'object_aliases':3,
    'deletion_authorized':False,
    'master_hash_policy':'reused stored checksum because candidate Masters are hard links to unchanged validated archive bytes',
    'derivative_hash_policy':'SHA-256 calculated once after copying each derivative into final candidate path',
    'prior_published_unit_unchanged':True,
}
atomic_write(RUN_ROOT/'validation.json',(json.dumps(validation,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode())
if PACKAGE.stat().st_dev!=FINAL_UNIT.parent.stat().st_dev:
    raise RuntimeError('candidate publication would cross filesystems')
os.rename(PACKAGE,FINAL_UNIT)
publication={**validation,'published_at':now(),'promotion':'same-filesystem atomic rename','candidate_only':True,'replaces_prior_unit':False}
atomic_write(RUN_ROOT/'publication.json',(json.dumps(publication,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode())
print(json.dumps(publication,ensure_ascii=False,sort_keys=True,indent=2))
