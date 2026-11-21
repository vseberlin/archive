"""Prepare/apply the explicit 16-case local inspection plan; never copy originals."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'archive_research.web.settings')
import django
django.setup()

from django.conf import settings
from archive_research.common import atomic_write, checked_member, file_lock, read_bytes, require
from .records import check_binding, rebuild, snapshot, _snapshot, _project, validate_records

EXPECTED = {f'P{i:02}' for i in range(1, 9)} | {f'D{i:02}' for i in range(1, 5)} | {f'L{i:02}' for i in range(1, 5)}


def prepare(plan, image_python=None, additions=False):
    if additions:
        keys = [c['key'] for c in plan['cases']]
        require(1 <= len(keys) <= 8 and len(set(keys)) == len(keys) and
                all(re.fullmatch(r'[A-Za-z0-9_-]+', key) for key in keys),
                'Select 1–8 distinct, explicitly named research cases')
        evidence, _, _, revision = snapshot()
        require(not set(keys) & {c['key'] for c in evidence['cases']},
                'Existing cases and reviews cannot be replaced')
        view_keys = [v['key'] for c in plan['cases'] for v in c['views']]
        existing_views = {v['key'] for c in evidence['cases'] for v in c['views']}
        require(len(view_keys) == len(set(view_keys)) and not set(view_keys) & existing_views,
                'New source-view keys must be unique and must not overwrite existing previews')
    else:
        require({c['key'] for c in plan['cases']} == EXPECTED and len(plan['cases']) == 16,
                'Require the existing 16 named evidence-review cases')
    root = settings.PILOT_ROOT
    roots = {name: Path(path) for name, path in settings.MACHINE.get('source_roots', {}).items()}
    require(roots == {'archive': Path('/mnt/volume/iss-archive'), 'elements': Path('/media/vladimir/Elements')},
            'Require the exact declared read-only roots')
    cases = []
    for case in plan['cases']:
        views = []
        for proposed in case['views']:
            original = proposed['source_binding']
            path = checked_member(original, list(roots.values()))
            name = next(name for name, base in roots.items() if path.is_relative_to(base))
            source = {**original, 'root': name, 'path': str(path.relative_to(roots[name]))}
            view = {k: v for k, v in proposed.items() if k != 'source_binding'}
            view.update(root=name, source=source, preview=None, rotation=0, page_count=1,
                        intended_use=case['intended_use'], region_xywh=proposed.get('region_xywh', [0, 0, 1, 1]),
                        transcription_method='Prepared source observation; whole page/image shown',
                        alt=proposed['label'])
            if not proposed.get('unavailable_reason'):
                require(re.fullmatch(r'[A-Za-z0-9_-]+', view['key']), 'Unsafe view key')
                target = root / 'cache' / f"real-{view['key']}.png"
                if path.suffix.lower() == '.pdf':
                    require(not view.get('preview_crop'), 'PDF crop preparation requires a positioned extraction receipt')
                    info = subprocess.run(['pdfinfo', '-f', str(view['page_sequence']), '-l', str(view['page_sequence']), str(path)],
                                          check=True, capture_output=True, text=True, timeout=30).stdout
                    view['page_count'] = int(re.search(r'^Pages:\s+(\d+)', info, re.M)[1])
                    rotate = re.search(r'Page(?:\s+\d+)?\s+rot:\s+(-?\d+)', info)
                    view['rotation'] = int(rotate[1]) % 360 if rotate else 0
                    subprocess.run(['pdftoppm', '-f', str(view['page_sequence']), '-l', str(view['page_sequence']),
                                    '-singlefile', '-scale-to', '1600', '-png', str(path), str(target.with_suffix(''))],
                                   check=True, capture_output=True, timeout=90)
                else:
                    # Image preparation uses the selected interpreter, outside HTTP.
                    region = view['region_xywh'] if view.get('preview_crop') else [0, 0, 1, 1]
                    from archive_research.contracts import Region
                    Region(*region)
                    clockwise = proposed.get('preview_clockwise', 0)
                    require(clockwise in (0, 90, 180, 270), 'Unsupported crop rotation')
                    code = ('from PIL import Image,ImageOps;import sys,json;'
                            'im=Image.open(sys.argv[1]);r=json.loads(sys.argv[3]);'
                            'assert im.getexif().get(274,1)==1 or r==[0,0,1,1],"Oriented crop needs source mapping";'
                            'im=ImageOps.exif_transpose(im);w,h=im.size;'
                            'im=im.crop((round(r[0]*w),round(r[1]*h),round((r[0]+r[2])*w),round((r[1]+r[3])*h)));'
                            'im=im.rotate(-int(sys.argv[4]),expand=True);im.thumbnail((1600,1600));'
                            'im.convert("RGB").save(sys.argv[2],"PNG")')
                    subprocess.run([image_python or sys.executable, '-B', '-c', code, str(path), str(target),
                                    json.dumps(region), str(clockwise)],
                                   check=True, capture_output=True, timeout=90)
                    view['rotation'] = clockwise
                    if view.get('preview_crop'):
                        view['transcription_method'] = 'Manually selected source-bound crop; original page and coordinates retained'
                checked_member(original, list(roots.values()))
                st = target.stat()
                view['preview'] = {'root': 'cache', 'path': target.name, 'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}
            views.append(view)
        cases.append({**case, 'views': views, 'synthetic': False})
        print('Prepared', case['key'], len(views), 'views', flush=True)
    target = root / ('cache/research-additions-prepared.json' if additions else 'cache/real-prepared.json')
    packet = {'cases': cases}
    if additions:
        packet['expected_revision'] = revision
    atomic_write(target, json.dumps(packet, ensure_ascii=False, indent=2).encode() + b'\n')
    print('Prepared packet:', target)


def apply():
    require(settings.PROJECT.get('mode') == 'local_review', 'Local review must be explicitly configured')
    root = settings.PILOT_ROOT
    prepared = json.loads(read_bytes(root / 'cache/real-prepared.json'))['cases']
    require({c['key'] for c in prepared} == EXPECTED and len(prepared) == 16, 'Wrong prepared scope')
    for case in prepared:
        for view in case['views']:
            check_binding(view['source'])
            if view['preview']:
                check_binding(view['preview'])
    with file_lock(root / 'records/.research.lock'):
        evidence_path = root / 'records/evidence.json'
        assertions_path = root / 'records/assertions.jsonl'
        evidence = json.loads(read_bytes(evidence_path))
        previous_bytes = read_bytes(assertions_path)
        history = [json.loads(line) for line in previous_bytes.splitlines() if line.strip()]
        existing = {c['key'] for c in evidence['cases']}
        require(not (existing & EXPECTED), 'Real cases are already loaded; do not overwrite saved reviews')
        for case in evidence['cases']:
            case.setdefault('synthetic', evidence.get('synthetic', True))
        evidence.update(synthetic=False, cases=evidence['cases'] + prepared)
        added = []
        for case in prepared:
            added.append({'case_key': case['key'], 'key': f"{case['key']}-assessment", 'revision': 0,
                          'subject': case['key'], 'relation': 'research_assessment',
                          'value': case['prepared_observation'], 'scope': case['question'],
                          'citation_keys': [v['key'] for v in case['views'] if not v.get('unavailable_reason')],
                          'uncertainty': case['limit'], 'review': 'unreviewed', 'reviewer': None,
                          'reviewed_at': None, 'supersedes_revision': None,
                          'reason': 'Prepared source-based observation for local review; no curator answer supplied.',
                          'synthetic': False, 'metadata_adopted': False})
        # Both readable files are updated while readers are excluded. An interrupted
        # update fails validation rather than presenting partially loaded cases.
        atomic_write(assertions_path, previous_bytes + b''.join(json.dumps(r, ensure_ascii=False).encode()+b'\n' for r in added))
        atomic_write(evidence_path, json.dumps(evidence, ensure_ascii=False, indent=2).encode()+b'\n')
    print('Loaded 16 unreviewed real cases; prior history retained. Revision:', rebuild())


def apply_additions():
    """Append the explicit prepared sample, retaining every existing review byte."""
    require(settings.PROJECT.get('mode') == 'local_review', 'Local review must be explicitly configured')
    root = settings.PILOT_ROOT
    packet = json.loads(read_bytes(root / 'cache/research-additions-prepared.json'))
    cases = packet['cases']
    require(1 <= len(cases) <= 8, 'Bounded prepared selection required')
    with file_lock(root / 'records/.research.lock'):
        evidence, _, _, revision = _snapshot()
        require(revision == packet['expected_revision'], 'Reviews changed since preparation; prepare again')
        existing = {c['key'] for c in evidence['cases']}
        require(not existing & {c['key'] for c in cases}, 'Existing cases and reviews cannot be replaced')
        previous = read_bytes(root / 'records/assertions.jsonl')
        added = []
        for case in cases:
            require(case.get('synthetic') is False, 'Real local research cases required')
            for view in case['views']:
                check_binding(view['source'])
                if view['preview']:
                    check_binding(view['preview'])
            proposals = case.get('prepared_assertions', [])
            require(1 <= len(proposals) <= 8, 'Each job needs 1–8 explicit evidence-based findings')
            require(len({p['key'] for p in proposals}) == len(proposals), 'Duplicate finding key')
            for proposal in proposals:
                require(set(proposal) == {'key', 'subject', 'relation', 'value', 'scope',
                                          'uncertainty', 'reason', 'citation_keys'},
                        'Supply finding fields only; reviewer/history fields are assigned by the operation')
                require(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', proposal['key']) and
                        all(isinstance(proposal[k], str) and proposal[k].strip()
                            for k in ('subject', 'relation')), 'Invalid finding identity or relationship')
                require(proposal.get('reason', '').strip(), 'A supporting rationale is required')
                added.append({**proposal, 'case_key': case['key'], 'revision': 0,
                              'review': 'unreviewed', 'reviewer': None, 'reviewed_at': None,
                              'supersedes_revision': None, 'synthetic': False, 'metadata_adopted': False})
        evidence = {**evidence, 'cases': evidence['cases'] + cases}
        evidence_bytes = json.dumps(evidence, ensure_ascii=False, indent=2).encode() + b'\n'
        assertion_bytes = previous + b''.join(json.dumps(r, ensure_ascii=False).encode()+b'\n' for r in added)
        captured = validate_records(evidence_bytes, assertion_bytes)
        atomic_write(root / 'records/assertions.jsonl', assertion_bytes)
        atomic_write(root / 'records/evidence.json', evidence_bytes)
        _project(captured)
    print('Added', len(cases), 'research jobs;', len(added), 'unreviewed findings; existing history retained.')


def main(argv=None):
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('action', choices=['prepare', 'apply', 'prepare-additions', 'apply-additions'])
    parser.add_argument('--plan')
    parser.add_argument('--image-python', help='Interpreter with Pillow for image preparation; defaults to this interpreter')
    args = parser.parse_args(argv)
    if args.action in ('prepare', 'prepare-additions'):
        require(args.plan, '--plan is required')
        prepare(json.loads(Path(args.plan).read_text()), args.image_python, args.action == 'prepare-additions')
    elif args.action == 'apply-additions':
        apply_additions()
    else:
        apply()


if __name__ == "__main__":
    main()
