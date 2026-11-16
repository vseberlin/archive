"""Bounded picture review helper. Uses existing decisions/API; no archive writes.

Rendering is in memory only. Saved dispositions remain proposals, with no
Master selection, drain registration, accession, deletion, or schema changes.
"""
import collections
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
PLAN = ROOT / 'picture-review-20260921.json'
BASE = 'http://127.0.0.1:8766'


def guard():
    parts = subprocess.check_output(['findmnt', '-rn', '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS',
                                    '-M', '/media/vladimir/Elements'], text=True).split()
    assert parts[:3] == ['/dev/sdf1', '/media/vladimir/Elements', 'ntfs3']
    assert 'ro' in parts[3].split(',') and 'rw' not in parts[3].split(',')


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return json.load(response)


def save_manual(decision):
    """Record an explicitly inspected comparison with existing review helpers.

    This uses the existing general per-file decision builder, which supports
    human/CLI comparisons as well as the automatic comparison UI. Its ownership,
    source, registered-file, completed-case and Master guards remain in force.
    """
    import paragon_workbench as app
    import retained_candidates as retained
    with app.connect(False) as con:
        con.execute('BEGIN IMMEDIATE')
        obj = retained.get_case(con, decision['case_key'])
        previous = obj.get('decision') or {}
        assert not previous.get('selected_master_path') and not previous.get('source_version_pair')
        old = {f['path']: f for f in previous.get('files', [])}
        path = decision['path']
        if old.get(path, {}).get('state', 'unresolved') != 'unresolved':
            raise ValueError('Manual comparison changed since review: ' + path)
        assert path not in obj['registered_drain_paths']
        for source in obj['sources']:
            retained.verify_asset(source)
        ref = decision['manual_reference']
        asset = retained.evidence()['assets'][ref['path']]
        retained.verify_asset(asset)
        stamp = Path(ref['path']).stat()
        assert stamp.st_size == ref['size_bytes'] and str(stamp.st_mtime_ns) == str(ref['mtime_ns'])
        if asset['source'] == 'retained':
            assert ref['path'] not in retained.evidence()['drain_registered']
            states = [f['state'] for r in retained.saved_reviews(con).values()
                      for f in r['files'] if f['path'] == ref['path']]
            assert states and all(s in {'retain', 'selected_master', 'carrier_section', 'member'} for s in states), states
        choices = {p: dict(state=f['state'], member_target=f.get('member_target', '')) for p, f in old.items()}
        choices[path] = dict(state=decision['state'], member_target='')
        relationship = previous.get('relationship', dict(predicate='unresolved', carrier=''))
        review = retained.build_review(obj, dict(action='retain', file_choices=choices,
            metadata=previous.get('proposed_metadata', obj['proposed_metadata']),
            metadata_reviewed=previous.get('metadata_reviewed', False),
            relation=relationship['predicate'], parent=relationship['carrier'],
            retention=previous.get('retention', {}), note=previous.get('note', ''),
            same_object_confirmed=previous.get('same_object_confirmed', False),
            coverage_confirmed=previous.get('coverage_confirmed', False)), 'Codex, authorized by Vladimir')
        for n, file in enumerate(review['files']):
            if file['path'] != path and file['path'] in old:
                review['files'][n] = copy.deepcopy(old[file['path']])
            elif file['path'] == path:
                target = copy.deepcopy(old.get(path, {})) | file
                target['note'] = "Reviewed by Codex under Vladimir's 21 September rules. " + decision['note']
                target['similarity_evidence'] = dict(source_path=path, source_sha256=file['sha256'],
                    comparison_target=dict(path=ref['path'], inventory=ref.get('inventory', ''),
                        target_source=asset['source'], sha256=asset.get('sha256', ''),
                        size_bytes=stamp.st_size, mtime_ns=stamp.st_mtime_ns),
                    evidence_report=PLAN.name, basis=decision['note'],
                    comparison_is_exact_byte_copy=False, deletion_authorized=False)
                review['files'][n] = target
        if previous:
            review = copy.deepcopy(previous) | {k: review[k] for k in ['files', 'actor', 'reviewed_at', 'action']}
        for p, file in old.items():
            if p != path:
                assert next(f for f in review['files'] if f['path'] == p) == file
        for key, value in previous.items():
            if key not in {'files', 'actor', 'reviewed_at', 'action'}:
                assert review[key] == value
        retained.record_review(con, review)
        con.commit()
        retained.export_decisions(con)
        assert retained.readjson(retained.DECISIONS) == retained.readable_decisions(con)
        return previous, review


def save():
    guard()
    plan = json.loads(PLAN.read_text())
    results = plan['results']
    done = {r['path'] for r in results if r['result'] == 'saved'}
    token = get('/retained/api/cases')['token']
    count = 0
    # Keep source representatives before recording any inspected local copies.
    ordered = sorted(plan['decisions'], key=lambda d: d['state'] == 'redundant')
    for decision in ordered:
        path = decision['path']
        if path in done:
            continue
        obj = get('/retained/api/case?key=' + urllib.parse.quote(decision['case_key'], safe=''))
        previous = obj.get('decision') or {}
        old = {f['path']: f for f in previous.get('files', [])}
        if old.get(path, {}).get('state', 'unresolved') != 'unresolved':
            results.append(dict(path=path, result='skipped_changed', state=old[path]['state']))
            continue
        payload = dict(case_key=decision['case_key'], previous_review=previous.get('reviewed_at', ''),
                       source_path=path, file_state=decision['state'],
                       comparison_path=decision.get('comparison_path', ''),
                       note="Reviewed by Codex under Vladimir's 21 September rules. " + decision['note'])
        request = urllib.request.Request(BASE + '/retained/api/decision',
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'X-Review-Token': token}, method='POST')
        try:
            if decision['state'] == 'redundant' and decision.get('manual_reference'):
                previous, review = save_manual(decision)
                old = {f['path']: f for f in previous.get('files', [])}
            else:
                with urllib.request.urlopen(request, timeout=30) as response:
                    review = json.load(response)['review']
        except urllib.error.HTTPError as error:
            results.append(dict(path=path, result='error', message=error.read().decode()))
            PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
            raise
        new = {f['path']: f for f in review['files']}
        assert new[path]['state'] == decision['state']
        assert review.get('selected_master_path', '') == previous.get('selected_master_path', '')
        for other, file in old.items():
            if other != path:
                assert new[other] == file, (other, 'unrelated file changed')
        for key, value in previous.items():
            if key not in {'files', 'actor', 'reviewed_at', 'action'}:
                assert review[key] == value, (key, 'metadata changed')
        results.append(dict(path=path, result='saved', state=decision['state'],
                            reviewed_at=review['reviewed_at']))
        PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
        count += 1
        if count % 10 == 0:
            print('Saved', len([r for r in results if r['result'] == 'saved']), flush=True)
    print('Saved totals', dict(collections.Counter(r['state'] for r in results if r['result'] == 'saved')), flush=True)


def render(items, columns=3, width=510, height=400):
    guard()
    sheet = Image.new('RGB', (columns * width, height * ((len(items) + columns - 1) // columns)), '#efefeb')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 17)
    for n, item in enumerate(items):
        x, y = n % columns * width, n // columns * height
        with Image.open(item['path']) as original:
            im = ImageOps.exif_transpose(original)
            if item.get('trim_white'):
                rgb = im.convert('RGB')
                difference = ImageChops.difference(rgb, Image.new('RGB', rgb.size, 'white'))
                bbox = difference.convert('L').point(lambda value: 255 if value > 18 else 0).getbbox()
                if bbox:
                    im = im.crop(bbox)
            if item.get('rotate'):
                im = im.rotate(item['rotate'], expand=True)
            if item.get('crop'):
                cx, cy = item.get('center', [.5, .5])
                cx, cy = int(im.width * cx), int(im.height * cy)
                im = im.crop((max(0, cx-width//2+6), max(0, cy-(height-55)//2),
                              min(im.width, cx+width//2-6), min(im.height, cy+(height-55)//2)))
            im.thumbnail((width - 12, height - 52))
            im = im.convert('RGB')
            sheet.paste(im, (x + (width-im.width)//2, y + 48 + (height-52-im.height)//2))
        draw.text((x+6, y+3), item.get('label', Path(item['path']).name)[:65], font=font, fill='black')
        draw.text((x+6, y+24), item.get('sub', '')[:68], font=font, fill='#333333')
    output = io.BytesIO()
    sheet.save(output, format='JPEG', quality=85)
    sys.stdout.buffer.write(output.getvalue())


if __name__ == '__main__':
    if sys.argv[1] == 'save':
        save()
    elif sys.argv[1] == 'render':
        args = json.loads(sys.stdin.read())
        render(**args)
    else:
        raise ValueError('Use save or render')
