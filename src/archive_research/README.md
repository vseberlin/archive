# Archive research boundary

The [consolidation plan](../../docs/architecture/archive-django-consolidation-plan.md)
now has a shared boundary and a bounded page extraction runner.
The source layout is `src/archive_research`; `pyproject.toml` packages only this
namespace. Python 3.11 or newer is required. Runtime imports use only the standard
library; processing explicitly invokes existing Pillow, Poppler and Tesseract.
Setuptools is a build-time dependency. Nothing runs on import.

From the source checkout, use `PYTHONPATH="$PWD/src"` to import the package.
An installed wheel exposes the same API without adding the legacy tools to the
environment. Existing photo/research commands keep their original entry points,
dependencies, output formats and caches.

## Explicit roots

`Roots.from_toml(path)` loads a caller-selected machine file. It has no default
path, environment fallback, implicit home directory or automatic root creation.
This example describes the root format. The private trial has its own explicit
roots file and does not write into existing visual outputs:

```toml
[roots.archive]
path = "/mnt/volume/iss-archive"
access = "read"
mount_target = "/mnt/volume"

[roots.elements]
path = "/media/vladimir/Elements"
access = "read"
mount_target = "/media/vladimir/Elements"
mount_source = "/dev/sdf1"
mount_fstype = "ntfs3"
mount_options = ["ro"]

[roots.artifacts]
# Select an existing, explicitly approved research output directory.
path = "/mnt/volume/iss-archive-work/visual-similarity"
access = "work"
mount_target = "/mnt/volume"
min_free_bytes = 1073741824
```

Names are logical locators; root paths are deployment configuration. Read and
work roots cannot overlap. Work roots cannot overlap protected workstation media.
Root/member symlinks, path traversal, absent mounts and insufficient configured
free space are refused. A mountpoint directory that remains after an unmount is
not a valid mounted destination. Checks repeat when paths are used.
The current Elements device/target/ntfs3/read-only safeguard cannot be weakened by
omitting a setting or choosing a broader read root. Volume paths require the
volume mount. Other compatible locations use their explicit mount constraints;
filesystem names and mount paths are not part of object identity.

These checks do not qualify storage privacy, durability, executable compatibility
or SQLite migration. Configuration neither authorizes writes nor grants access
to new material. The caller still owns task authorization and concurrency control.

## Source, page and result records

`Source` carries existing unit/object/representation identifiers, named root and
relative path, size/mtime binding, optional stored SHA-256 and historical source
path and source role. It never derives new identities from the current mount
location. Supply identifiers and member order from authoritative existing records.
`units.selected_members` qualifies only Trafo, FEB and Bibliothek mappings: it
requires catalogue/manifest agreement, preserves gaps and existing representation
IDs, and refuses duplicate witnesses or positions. It does not select Masters or
infer equivalence between PDF pages and TIFF members.

`Page` separates one-based representation member position, physical PDF page
(one-based), TIFF frame (zero-based), single image (index zero), optional logical
page and printed label. Source width, height, coordinate units and top-left origin
are explicit: integer pixels, or fractional PDF points for native PDF text.
Unknown dimensions
or member order need evidence before constructing a page; filenames are not proof.

`Region` uses normalized x/y/width/height from the top-left of source imagery.
`Transform` records processed dimensions and, when known, a six-coefficient affine
map from processed pixels back to the page's source coordinate units, plus transformation descriptions.
Unknown inverse transforms are `None`; consumers must withhold source highlights.
Nonlinear deskew is not implemented by this first affine contract.

`Recipe.create(name, version, **parameters)` captures JSON settings immutably.
Adapters must include relevant model fingerprint, dependency versions, languages,
resolution, preprocessing and requested coverage. Its `cache_key(page, region,
transform)` includes the bound source, member/frame, recipe and geometry; changing
one extractor's recipe does not invalidate another extractor. This is a reusable
key function used by the page runner below; it is not an identity registry.

`Result` records complete/partial/error/changed_source outcomes, elapsed time,
artifacts, region/transform and explicit limitations. `record()` returns readable
JSON-compatible data; `from_record()` validates it on reload. Results always carry
`metadata_adopted=false`. Completion describes that extractor operation only, not
all pages, archival completeness, human review or factual correctness. Empty text
is not interpreted as a blank page.

`roots.source(source)` checks a regular file and its recorded size/mtime.
It **does not recalculate a stored hash**. An explicitly bounded
`roots.source(source, verify_hash=True)` additionally verifies that hash and checks
for replacement during reading. A size/mtime check alone cannot detect a rewrite
that preserves both values. `roots.artifact(artifact)` checks location/type and
refuses hard-linked output; the producing extractor owns its reported digest.
No source data or review record is written by these APIs.

## Existing visual renderer adapter

`archive_research.visual.preview` takes an explicit `renderer` callable. For the
existing tool, use `visual_research.make_preview` and its `VERSION`, with actual
Pillow/vips versions. The source checkout caller can import that module by also
including `Archive-tools` on its Python path. The core wheel does not install the
legacy tool, Pillow, vips, numerical libraries or Django.

```python
from archive_research.visual import preview
from visual_research import VERSION, make_preview

# roots, page, row and versions come from the caller's checked existing records.
wrapped = preview(roots, page, "artifacts", row, {},
                  renderer=make_preview, version=VERSION,
                  dependencies=versions)
legacy_output = wrapped.legacy       # original row/output shape, unchanged
research_record = wrapped.result.record()
```

The wrapper checks source/root bindings before and after the call, preserves
existing identities and reviewer fields, and requires an existing real `previews`
directory. It supports only the first image/TIFF frame; PDF or later-frame requests,
multi-member rows and mismatched identities are refused. Select an explicit member
first. The original function still owns rendering, preview names and cache behavior.
No new result file is automatically written or attached to a workbench.

The legacy recipe does not fully report EXIF orientation, so this adapter records
processed dimensions and an **unknown inverse transform**. It produces a visual
preview only. The positioned extraction path below uses its own explicit verified
transform. Failed legacy preview calls raise.

## Bounded positioned extraction

`preparation` inspects explicit PDF pages/TIFF frames/images. PDF processing uses
300 dpi by default; image processing retains native dimensions. Orthogonal PDF
and editorial rotations are supported. Nontrivial image EXIF orientation is
refused pending a verified mapping. No deskew or inferred layout is applied.
`pdf_text` retains Poppler bounding-box XHTML, including pre-existing embedded
OCR whose provenance may be unknown. `ocr` retains Tesseract ALTO with word boxes
and machine confidence, using `deu+eng` by default. Native and new OCR receipts
are separate. `geometry` maps both back to normalized original-source regions.

Prepare a private `archive-research-plan-1` JSON with `metadata_adopted: false`
and an explicit `tasks` list. Each task contains a unique `id`, a serialized
`Page`, `extractor` (`pdf-text` or `ocr`) and optional `settings`: `dpi`,
`clockwise`, `languages`, `psm`, `timeout`. No recursive discovery is performed.

```sh
PYTHONPATH="$PWD/src" python3 -B -m archive_research \
  --roots /approved/trial/roots.toml --plan /approved/trial/plan.json run --limit 2
PYTHONPATH="$PWD/src" python3 -B -m archive_research \
  --roots /approved/trial/roots.toml --plan /approved/trial/plan.json run
```

One manual writer starts one fresh worker per page and records progress after
each attempt. Completed and no-text receipts are reused when bindings match;
failed work is retryable. Recipe changes get separate keys. A changed plan is
refused by workers. Source changes withhold old results without overwriting their
receipt. Raw output, processed PNG, word regions, dependency versions, elapsed
time, peak process/child RSS and retained artifact bytes remain readable files.
`run-summary.json` is a rebuildable projection, not review authority.
File stamps guard reuse; they are not a new full-content verification claim.

No-text output is `partial`, never proof of a blank page. A successful extractor
does not certify transcription accuracy, complete object coverage or review.
The initial 16 November trial covers 13 tasks across three units and synthetic
edge cases. Its bounded follow-up covers the existing 16-case pilot selection
and remaining pages in the same three trial PDFs. Source-to-highlight plots were inspected; the user confirmed the requested browser word highlight.
Collection transcription accuracy and a larger agreed scope remain open. No global indexing job is installed.

`visual.existing_leads` projects a bounded set of existing neighbour results
after checking their manifest digest. It preserves existing identities and model
information and reruns no model. The workbench checks original and preview
bindings before serving either side. An offline original is shown as unavailable.

## Verification

Run the synthetic boundary tests from the source root:

```sh
python3 -B -m unittest discover -s tests -p test_archive_research.py -v
python3 -B -m unittest discover -s tests -p test_research_units.py -v
python3 -B -m unittest discover -s tests -p test_positioned_extraction.py -v
```

The core import test uses a fresh interpreter without site packages. The one real
legacy-renderer test additionally requires the existing workstation Pillow/vips;
it renders a generated TIFF in a disposable temporary directory. All other files
and mounts are synthetic or mocked. No archive image, live review, database or
service is involved. Existing visual and photo-contract suites remain separate.
