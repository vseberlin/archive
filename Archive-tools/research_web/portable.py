"""One explicit synthetic handover archive; no credentials or database transfer."""
import io
from pathlib import Path
import tarfile

from django.conf import settings

from archive_common import atomic_write, file_lock, read_bytes, require
from .records import _snapshot, bound_bytes


README = '''# Portable synthetic research pilot

This package contains invented source media, readable research records and the
small application. It contains no accounts, credentials, database, OCR cache or
real archive material. It is a technical demonstration, not curator acceptance.

Use a compatible Linux machine with Python 3.13, venv and GNU tar. Poppler is
needed for fixture generation/tests; optional Tesseract is only used by the
explicit OCR command. Extract with GNU tar so the PAX nanosecond file timestamps
remain identical to the evidence bindings. Do not re-save the sample files.

From the extracted folder, with an absolute path in PILOT_DIR:

```sh
PILOT_DIR="$(pwd)/runtime"
python3 -B Archive-tools/research_web/configure.py "$PILOT_DIR" --username operator
python3 -m venv "$PILOT_DIR/.venv"
"$PILOT_DIR/.venv/bin/python" -m pip install -r Archive-tools/research_web/requirements.txt
export ARCHIVE_RESEARCH_PROJECT="$PILOT_DIR/project.toml"
export ARCHIVE_RESEARCH_MACHINE="$PILOT_DIR/machine.toml"
cd Archive-tools
"$PILOT_DIR/.venv/bin/python" -B -m research_web.bootstrap init
"$PILOT_DIR/.venv/bin/python" -B -m research_web.bootstrap storage-check
"$PILOT_DIR/.venv/bin/python" -B -m research_web.bootstrap export
"$PILOT_DIR/.venv/bin/python" -B research_web/manage.py runserver 127.0.0.1:8785 --noreload
```

Read the newly generated runtime/operator-login.txt locally. Open the loopback
URL, inspect citations, save a synthetic review and correction, then export.
Stop with Ctrl-C; restart and rebuild without changing history. Do not start a
second listener on the same port. Keep runtime private and never publish secrets.
All operator/session state is newly created; record reviewer names remain part
of the synthetic history. Portable source roots are explicit in machine.toml;
no Python edits are needed. Source paths inside records remain relative.

Optional OCR is an explicit CLI operation:
`python -B -m research_web.bootstrap ocr --view report-figure` using the configured
venv/environment. Missing Tesseract is reported; it does not prevent manual
citation review. OCR cache is disposable and never overwrites review text.
Prepared images may use clockwise page rotations of 0/90/180/270 degrees;
recorded regions remain on the unrotated page. A read_only case in evidence.json
can be browsed/exported but rejects review writes. No background jobs run.

Record second-operator/machine task completion and failures separately. A local
relocation test does not demonstrate independent human usability or acceptance.
Actual PDF viewers determine #page fragment support. Real-data import, archive
mutation, migration and catalogue adoption are outside this package.
'''


def build_package():
    root = settings.PILOT_ROOT
    source = Path(__file__).resolve().parent
    stream = io.BytesIO()
    def add(archive, name, data, mtime_ns=0):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o600
        info.mtime = mtime_ns // 1000000000
        info.pax_headers = {'mtime': f'{mtime_ns // 1000000000}.{mtime_ns % 1000000000:09d}'}
        archive.addfile(info, io.BytesIO(data))
    with file_lock(root / 'records/.research.lock'):
        evidence = _snapshot()[0]
        require(evidence.get('synthetic') is True and all(c.get('synthetic', True) for c in evidence['cases']),
                'Portable packages are synthetic-only; real source material cannot be bundled')
        media = {}
        for view in evidence['cases'][0]['views']:
            for field in ('source', 'preview'):
                binding = view[field]
                media[binding['path']] = (bound_bytes(binding), binding['mtime_ns'])
        with tarfile.open(fileobj=stream, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
            add(archive, 'README.md', README.encode())
            for path in sorted(source.rglob('*')):
                if path.is_file() and path.suffix in ('.py', '.html', '.md', '.txt'):
                    data = (b'See [portable setup and limits](../../README.md).\n'
                            if path == source / 'README.md' else path.read_bytes())
                    add(archive, f'Archive-tools/research_web/{path.relative_to(source)}', data)
            add(archive, 'Archive-tools/archive_common.py', (source.parent / 'archive_common.py').read_bytes())
            example = source.parents[1] / 'docs/architecture/archive-research-record-example.md'
            add(archive, 'docs/architecture/archive-research-record-example.md', example.read_bytes())
            for name in ('evidence.json', 'assertions.jsonl'):
                add(archive, f'runtime/records/{name}', read_bytes(root / 'records' / name))
            for name, (data, stamp) in media.items():
                add(archive, f'runtime/sample/{name}', data, stamp)
    target = root / 'exports/synthetic-pilot.tar.gz'
    atomic_write(target, stream.getvalue())
    return target
