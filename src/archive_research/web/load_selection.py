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
from .records import check_binding, rebuild

EXPECTED = {f'P{i:02}' for i in range(1, 9)} | {f'D{i:02}' for i in range(1, 5)} | {f'L{i:02}' for i in range(1, 5)}


def prepare(plan, image_python=None):
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
                        intended_use=case['intended_use'], region_xywh=[0, 0, 1, 1],
                        transcription_method='Prepared source observation; whole page/image shown',
                        alt=proposed['label'])
            if not proposed.get('unavailable_reason'):
                require(re.fullmatch(r'[A-Za-z0-9_-]+', view['key']), 'Unsafe view key')
                target = root / 'cache' / f"real-{view['key']}.png"
                if path.suffix.lower() == '.pdf':
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
                    code = ('from PIL import Image,ImageOps;import sys;'
                            'im=ImageOps.exif_transpose(Image.open(sys.argv[1]));im.thumbnail((1600,1600));'
                            'im.convert("RGB").save(sys.argv[2],"PNG")')
                    subprocess.run([image_python or sys.executable, '-B', '-c', code, str(path), str(target)],
                                   check=True, capture_output=True, timeout=90)
                checked_member(original, list(roots.values()))
                st = target.stat()
                view['preview'] = {'root': 'cache', 'path': target.name, 'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}
            views.append(view)
        cases.append({**case, 'views': views, 'synthetic': False})
        print('Prepared', case['key'], len(views), 'views', flush=True)
    target = root / 'cache/real-prepared.json'
    atomic_write(target, json.dumps({'cases': cases}, ensure_ascii=False, indent=2).encode() + b'\n')
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


def main(argv=None):
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('action', choices=['prepare', 'apply'])
    parser.add_argument('--plan')
    parser.add_argument('--image-python', help='Interpreter with Pillow for image preparation; defaults to this interpreter')
    args = parser.parse_args(argv)
    if args.action == 'prepare':
        require(args.plan, '--plan is required')
        prepare(json.loads(Path(args.plan).read_text()), args.image_python)
    else:
        apply()


if __name__ == "__main__":
    main()
