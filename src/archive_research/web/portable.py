"""One explicit synthetic handover archive; no credentials or database transfer."""
import io
from pathlib import Path
import tarfile

from django.conf import settings

from archive_research.common import atomic_write, file_lock, read_bytes, require
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
python3 -m venv "$PILOT_DIR/.venv"
"$PILOT_DIR/.venv/bin/python" -m pip install '.[web]'
"$PILOT_DIR/.venv/bin/archive-research" web configure "$PILOT_DIR" --username operator
export ARCHIVE_RESEARCH_PROJECT="$PILOT_DIR/project.toml"
export ARCHIVE_RESEARCH_MACHINE="$PILOT_DIR/machine.toml"
"$PILOT_DIR/.venv/bin/archive-research" web init
"$PILOT_DIR/.venv/bin/archive-research" web storage-check
"$PILOT_DIR/.venv/bin/archive-research" web export
"$PILOT_DIR/.venv/bin/archive-research" web serve
```

Read the newly generated runtime/operator-login.txt locally. Open the loopback
URL, inspect citations, save a synthetic review and correction, then export.
Stop with Ctrl-C; restart and rebuild without changing history. Do not start a
second listener on the same port. Keep runtime private and never publish secrets.
All operator/session state is newly created; record reviewer names remain part
of the synthetic history. Portable source roots are explicit in machine.toml;
no Python edits are needed. Source paths inside records remain relative.

Optional OCR is an explicit CLI operation:
`archive-research web ocr --view report-figure` using the configured
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
    source = Path(__file__).resolve().parent.parent
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
            # Include the installed package, never locate source via checkout parents.
            for path in sorted(source.rglob('*')):
                if path.is_file() and (path.suffix in ('.py', '.html') or path == source / 'web/fixture.json'):
                    add(archive, f'src/archive_research/{path.relative_to(source)}', path.read_bytes())
            from importlib.metadata import distribution
            import json
            dist = distribution('iss-archive-research')
            web_dependencies = [requirement.split(';', 1)[0].strip() for requirement in (dist.requires or [])
                                if 'extra == "web"' in requirement or "extra == 'web'" in requirement]
            packaging = ('[build-system]\nrequires = ["setuptools>=61"]\nbuild-backend = "setuptools.build_meta"\n'
                         '[project]\nname = "iss-archive-research"\nversion = ' + json.dumps(dist.version) + '\nrequires-python = ">=3.11"\n'
                         '[project.scripts]\narchive-research = "archive_research.cli:main"\n'
                         '[project.optional-dependencies]\nweb = ' + json.dumps(web_dependencies) + '\n'
                         '[tool.setuptools.packages.find]\nwhere = ["src"]\n'
                         '[tool.setuptools.package-data]\n"archive_research.web" = ["fixture.json", "templates/research_web/*.html"]\n')
            add(archive, 'pyproject.toml', packaging.encode())
            for name in ('evidence.json', 'assertions.jsonl'):
                add(archive, f'runtime/records/{name}', read_bytes(root / 'records' / name))
            for name, (data, stamp) in media.items():
                add(archive, f'runtime/sample/{name}', data, stamp)
    target = root / 'exports/synthetic-pilot.tar.gz'
    atomic_write(target, stream.getvalue())
    return target
