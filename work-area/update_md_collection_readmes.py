#!/usr/bin/env python3
"""Add captured ISS collection descriptions to the fourteen existing unit READMEs.

This is a bounded documentation edit. Existing checksum references are adjusted;
object catalogues, SQLite, Masters, derivatives and source files are untouched.
"""
import argparse
import csv
import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

WORK = Path('/mnt/volume/iss-archive-work')
ARCHIVE = Path('/mnt/volume/iss-archive/units')
CAPTURE = WORK/'md-collection-descriptions.json'
START = '<!-- md-collection-description:start -->'
END = '<!-- md-collection-description:end -->'
MAPPING = {
    'FEB_ Forschungs-und-Entwicklungsberichte_aus_dem_WF':'551',
    'aeb':'591', 'archiv-wf':'553', 'baeckerhandwerk':'827', 'bibliothek':'556',
    'brigadebuecher':'861', 'kurt-schwarz':'783', 'objektsammlung':'549',
    'paragon':'593', 'pressebildarchiv-wf':'555', 'tfa':'857', 'trafo':'762',
    'wf-alben':'607', 'wf-sender':'552',
}
CHECKSUMS = ['00_MANIFESTS/checksums.csv','00_MANIFESTS/final-checksums.csv',
             '00_MANIFESTS/payload-checksums.csv','db-restore/checksums.csv']
RECEIPTS = ['00_MANIFESTS/accession-completion.json','db-restore/accession-completion.json',
            'db-restore/document-accession-completion.json']


def require(value,message):
    if not value:raise ValueError(message)


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def stamped(path):
    st=path.stat()
    return [st.st_size,st.st_mtime_ns]


def replace(path,before,after):
    require(path.resolve(strict=True)==path and path.is_file(),'Unexpected path or symlink')
    require(path.read_bytes()==before,'File changed since preparation: '+str(path))
    # No backup, generation, directory or extra archive artifact is introduced.
    with path.open('r+b') as f:
        f.write(after)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())
    require(path.read_bytes()==after,'Write readback differs')


def description(record):
    # Keep German wording, punctuation and source typos; normalize paragraph whitespace only.
    return '\n\n'.join(line.strip() for line in record['collection_description'].replace('\r\n','\n').split('\n') if line.strip())


def section(cid,records,date,heading=False):
    r=records[cid]
    text=(f"### {r['collection_name']}\n\n" if heading else '')+description(r)
    return text+f"\n\n*Quelle: [museum-digital — {r['collection_name']}](https://berlin.museum-digital.de/collection/{cid}); übernommen am {date}.*\n"


def descendants(cid,records):
    ids=[]
    for child in records[cid].get('collection_subcollections',[]):
        key=str(child['collection_id'])
        require(key in records,'Missing child description')
        if key not in ids:ids.append(key)
        for key in descendants(key,records):
            if key not in ids:ids.append(key)
    return ids


def prepare():
    require(ARCHIVE.resolve(strict=True)==ARCHIVE,'Archive path differs')
    capture=json.loads(CAPTURE.read_text())
    records=capture['collections']
    require(len(records)==36,'Captured collection census differs')
    require(all(r['collection_institution']['institution_id']==29 for r in records.values()),'Non-ISS collection')
    require(all(description(r) for r in records.values()),'Empty description')
    require({p.name for p in ARCHIVE.iterdir() if p.is_dir()}==set(MAPPING),'Local unit census changed')
    date=datetime.fromisoformat(capture['completed_at']).strftime('%d.%m.%Y')
    plan=[]
    for unit,cid in MAPPING.items():
        root=ARCHIVE/unit
        paths=[root/n for n in ['00_README.md','README.md'] if (root/n).is_file()]
        require(len(paths)==1,'Ambiguous main README: '+unit)
        path=paths[0]
        before=path.read_bytes()
        text=before.decode()
        require(START not in text and END not in text,'Description is already present; inspect before replacing')
        title,sep,body=text.partition('\n')
        require(title.startswith('# ') and sep,'Missing README title')
        block=START+'\n## Über die Sammlung\n\n'+section(cid,records,date)
        included=[cid]
        children=descendants(cid,records)
        if children:
            block+=f'\n<details>\n<summary>Beschreibungen der MD-Teilsammlungen ({len(children)})</summary>\n\n'
            block+='\n'.join(section(child,records,date,True) for child in children)
            block+='\n</details>\n'
            included+=children
        parents=[str(r['collection_id']) for r in records[cid].get('collection_supercollections',[])]
        if parents:
            block+='\n<details>\n<summary>Übergeordnete Sammlung in museum-digital</summary>\n\n'
            block+='Die folgende Beschreibung betrifft die übergeordnete MD-Sammlung mit ihrem weiter gefassten Umfang.\n\n'
            block+='\n'.join(section(parent,records,date,True) for parent in parents)
            block+='\n</details>\n'
            included+=parents
        if unit=='kurt-schwarz':
            # MD now lists Industrie separately; do not invent a parent relationship.
            block+='\n<details>\n<summary>Weitere MD-Sammlung zu Kurt Schwarz</summary>\n\n'
            block+='Diese Sammlung wird in museum-digital eigenständig geführt. Ihre Beschreibung dokumentiert den Sammlungskontext; sie ändert keine lokalen Objektzuordnungen.\n\n'
            block+=section('973',records,date,True)+'\n</details>\n'
            included.append('973')
        block+='\n'+END+'\n'
        after=(title+'\n\n'+block+body).encode()
        # Removing only our insertion must recover the entire original file.
        require(after.decode().replace('\n'+block,'',1)==text,'Existing README body would change')
        plan.append(dict(unit=unit,root=root,path=path,cid=cid,included=included,before=before,after=after,
                         old_sha=sha(before),new_sha=sha(after),checksum_updates=[],receipt_updates=[]))
    return capture,plan


def csv_replacement(before,rows,fields):
    newline='\r\n' if b'\r\n' in before else '\n'
    output=io.StringIO(newline='')
    writer=csv.DictWriter(output,fieldnames=fields,quoting=csv.QUOTE_ALL if before.startswith(b'"') else csv.QUOTE_MINIMAL,lineterminator=newline)
    writer.writeheader();writer.writerows(rows)
    return output.getvalue().encode()


def main(apply=False):
    capture,plan=prepare()
    # Establish all existing README checksum references before any archive write.
    for p in plan:
        for relative in CHECKSUMS:
            path=p['root']/relative
            if not path.is_file():continue
            before=path.read_bytes()
            reader=csv.DictReader(io.StringIO(before.decode(),newline=''))
            rows=list(reader);fields=reader.fieldnames
            key=next((f for f in ['relative_path','path','final_relative_path'] if f in fields),None)
            if not key:continue
            matches=[i for i,r in enumerate(rows) if r[key]==p['path'].name]
            require(len(matches)<=1,'Duplicate README checksum rows')
            if not matches:continue
            i=matches[0]
            require(rows[i]['sha256'].removeprefix('sha256:')==p['old_sha'],'Stale current README checksum')
            require(int(rows[i]['size_bytes'])==len(p['before']),'Stale README size')
            # Preserve the exact serializer for unaffected manifest rows.
            require(csv_replacement(before,rows,fields)==before,'Unexpected checksum CSV format: '+str(path))
            p['checksum_updates'].append(dict(path=path,before=before,rows=rows,fields=fields,index=i))
        for relative in RECEIPTS:
            path=p['root']/relative
            if not path.is_file():continue
            before=path.read_bytes();data=json.loads(before)
            current=data.get('control_file_sha256',{})
            if p['path'].name in current:
                require(current[p['path'].name].removeprefix('sha256:')==p['old_sha'],'Stale README receipt hash')
            for prefix,relative in [('payload_checksum_manifest','00_MANIFESTS/payload-checksums.csv'),
                                    ('checksum_manifest','00_MANIFESTS/checksums.csv')]:
                matching=[m for m in p['checksum_updates'] if str(m['path'].relative_to(p['root']))==relative]
                if prefix+'_sha256' in data and matching:
                    manifest=matching[0]
                    require(data[prefix+'_sha256'].removeprefix('sha256:')==sha(manifest['before']),'Stale manifest receipt hash')
                    if 'payload_bytes' in data:
                        require(data['payload_bytes']==sum(int(r.get('size_bytes') or 0) for r in manifest['rows']),
                                'Unexpected payload byte total')
            p['receipt_updates'].append(dict(path=path,before=before,data=data))
    print(json.dumps({'readmes':len(plan),'descriptions_fetched':len(capture['collections']),
        'descriptions_used':len({cid for p in plan for cid in p['included']}),
        'checksum_manifests_to_adjust':sum(len(p['checksum_updates']) for p in plan),
        'apply':apply},indent=2),flush=True)
    if not apply:return
    updated=[]
    for p in plan:
        replace(p['path'],p['before'],p['after'])
        changed_hashes={p['path'].name:p['new_sha']}
        delta=len(p['after'])-len(p['before'])
        for manifest in p['checksum_updates']:
            rows=manifest['rows'];r=rows[manifest['index']]
            r['sha256']=('sha256:' if r['sha256'].startswith('sha256:') else '')+p['new_sha']
            r['size_bytes']=str(len(p['after']))
            if 'mtime_ns' in r:r['mtime_ns']=str(p['path'].stat().st_mtime_ns)
            payload=csv_replacement(manifest['before'],rows,manifest['fields'])
            replace(manifest['path'],manifest['before'],payload)
            relative=str(manifest['path'].relative_to(p['root']))
            changed_hashes[relative]=sha(payload)
            manifest['after']=payload
        changed_receipts=[]
        for entry in p['receipt_updates']:
            data=entry['data'];changed=False
            current=data.get('control_file_sha256',{})
            for relative,digest in changed_hashes.items():
                if relative in current:
                    old=current[relative]
                    current[relative]=('sha256:' if old.startswith('sha256:') else '')+digest
                    changed=True
            # Update existing direct manifest references, without adding a hash framework.
            for prefix,relative in [('payload_checksum_manifest','00_MANIFESTS/payload-checksums.csv'),
                                    ('checksum_manifest','00_MANIFESTS/checksums.csv')]:
                field=prefix+'_sha256'
                if field in data and relative in changed_hashes:
                    manifest=next(m for m in p['checksum_updates'] if str(m['path'].relative_to(p['root']))==relative)
                    require(data[field].removeprefix('sha256:')==sha(manifest['before']),'Stale manifest receipt hash')
                    data[field]=('sha256:' if data[field].startswith('sha256:') else '')+changed_hashes[relative]
                    if prefix+'_size_bytes' in data:data[prefix+'_size_bytes']=len(manifest['after'])
                    if 'payload_bytes' in data:
                        old_rows=list(csv.DictReader(io.StringIO(manifest['before'].decode(),newline='')))
                        require(data['payload_bytes']==sum(int(r.get('size_bytes') or 0) for r in old_rows),'Unexpected payload byte total')
                        data['payload_bytes']+=delta
                    changed=True
            if changed:
                payload=(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode()
                replace(entry['path'],entry['before'],payload)
                changed_receipts.append(str(entry['path']))
        require(p['path'].read_bytes()==p['after'],'Final README differs')
        updated.append(dict(unit=p['unit'],readme=str(p['path']),primary_collection=p['cid'],
            included_descriptions=p['included'],old_sha256=p['old_sha'],sha256=p['new_sha'],
            size_bytes=len(p['after']),mtime_ns=p['path'].stat().st_mtime_ns,
            updated_checksum_manifests=[str(m['path']) for m in p['checksum_updates']],
            updated_existing_receipts=changed_receipts))
        print('Updated',p['unit'],flush=True)
    unmapped=set(capture['collections'])-{cid for p in plan for cid in p['included']}
    report=dict(completed_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        request='Pull all ISS MD collection descriptions and include them at the top of each unit README.',
        source_capture=str(CAPTURE),collections_fetched=len(capture['collections']),
        http_requests=len(capture['requests']),requests_sequential=True,request_delay_seconds=capture['request_delay_seconds'],
        updated_units=updated,collections_without_accessioned_unit=[{
            'collection_id':cid,'name':capture['collections'][cid]['collection_name'],
            'description':capture['collections'][cid]['collection_description'],
            'source_url':'https://berlin.museum-digital.de/collection/'+cid} for cid in sorted(unmapped)],
        existing_readme_bodies_preserved=True,wording_preserved=True,
        sqlite_or_object_catalogues_changed=False,masters_changed=False,derivatives_changed=False,
        source_files_changed=False,new_archive_files_or_directories=False)
    (WORK/'md-collection-readme-update.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    main(parser.parse_args().apply)
