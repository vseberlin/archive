# Archive research application

One installed Python application owns local extraction, cross-unit retrieval,
evidence packets, assessment and the Django workbench. The
[consolidation plan](../../docs/architecture/archive-django-consolidation-plan.md)
owns scope; private project/machine configuration selects existing data and roots.

```text
src/archive_research/
    cli.py, __main__.py     archive-research / python -m archive_research
    config.py              project, machine and extraction-root configuration
    pipeline.py            shared extract → index → search → packet operations
    contracts.py, sources.py, storage.py, units.py
    extractors/            OCR, PDF text, regions, layout, visual features
    preparation.py, geometry.py, runner.py
    retrieval.py, evidence.py, profiles.py, sift.py
    assessment/            request preparation, transports, response validation
    web/                   Django views, models, templates and synthetic fixture
```

Python 3.11+ and setuptools are required for installation. From the repository,
install into the selected environment with `python -m pip install .`; an offline
installation with dependencies already available uses
`python -m pip install --no-deps --no-build-isolation .`. The console command works
from any directory, without `PYTHONPATH` or imports from `Archive-tools`.

Optional dependencies are declared in `pyproject.toml`: `.[web]`, `.[images]`,
`.[vision]`, `.[embeddings]`. The pinned CPU torch wheels need the matching PyTorch
CPU wheel index; installing this project does not download model weights.
Existing qualified environments can install the package alone with `--no-deps`.
Poppler and Tesseract remain explicitly invoked system executables. Local model
paths are required for optional OpenCLIP/text-region adapters. Core imports and
CLI help use only the standard library; heavy dependencies load on demand.

The CLI and workbench call the same application modules. HTTP reads prepared
receipts and never launches extraction or submits model requests. Old package
imports and `Archive-tools/research_{packet,retrieve,assessment}.py` remain thin
forwarders; reconciliation/accession and completed unit jobs retain their own
legacy entry points. New research implementation belongs here.

## One bounded workflow

```sh
archive-research run --roots /approved/trial/roots.toml \
  --retrieval-selection /approved/trial/retrieval-selection.json \
  --packet-selection /approved/trial/packet-selection.json \
  --output-prefix selected-research --sift
```

Add `--plan /approved/trial/plan.json` to run selected extraction tasks first;
omit it to reuse existing receipts. Add `--model MODEL` to prepare a local model
request. `run` never submits externally. Its output prefix must be unused, and
selected sources and receipts are rechecked at every stage. Individual commands
are `extract`, `index`, `search`, `packet`, `reference`, `assess` and `web`; each has `--help`.
`retrieve` retains the earlier combined index/search interface.

Processing commands accept `--roots`, or `--project` and `--machine` to resolve
`extraction_roots` from the existing project configuration. `assess` takes explicit
`--roots`; `web` uses project/machine configuration. Environment variables
`ARCHIVE_RESEARCH_PROJECT` and `ARCHIVE_RESEARCH_MACHINE` select those same files.
No new registry, metadata authority, database or service is introduced.
See the [workbench guide](web/README.md) for installed launch and review commands.

## Shared image preparation and reference experiment

Unsigned 16-bit grayscale TIFFs use a recorded linear 0..65535 → 0..255 display
conversion before RGB conversion. Signed, float and high-depth multichannel input
is refused pending an explicit decoder/range. Original data is unchanged; this
is neither contrast stretching nor a master conversion. The preparation policy
is fingerprinted in new extraction recipes, so old clipped receipts are not reused
by new extraction runs. Existing selected old receipts remain readable and need
an explicit audit/re-extraction before trusting affected results.

Matching page/frame, crop, DPI, rotation and decoder versions share a checked
`prepared-*.png` and JSON binding. Different extractors reuse that view. Pillow
still decodes the selected source image in memory on its first render; this is
not a streaming TIFF decoder. PDFs and TIFF frame selection use the shared page
contract. Letterbox framing is the default for new visual embeddings; old centre
crop vectors remain in a different model space. Fine details still need crops.

The `faces` extractor accepts a fingerprinted local YuNet ONNX `Source`, uses
native-resolution overlapping tiles plus an overview, and checks orthogonal
rotations. It emits source-coordinate regions and explicit view coverage, with
bounded runtime/views. It does not produce face embeddings, names or clusters.
Regions can be selected as existing `face` evidence views. Documentary caption
assignment and held-out detection accuracy remain unqualified. The model comes
from [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet);
retain its MIT license with the local model.

`archive-research reference` owns the former visual-kNN experiment:

```sh
archive-research reference --roots ROOTS.toml --model-source MODEL-SOURCE.json \
  --work-root EXISTING-WORK-DIRECTORY index-reference --capture CAPTURE --batch 8
archive-research reference --roots ROOTS.toml --model-source MODEL-SOURCE.json \
  --work-root EXISTING-WORK-DIRECTORY evaluate
```

The capture is read locally; only its published previews are downloaded, cached
and hashed. OpenCLIP loads explicit local safetensors without a remote model
resolver. Model hash, dependency versions and framing separate embedding spaces.
SQLite is a resumable projection; `reference-run.json` records coverage and recipe,
and `reference-records.jsonl` exports vectors and provenance on completion.
Missing preview URLs and failures are reported separately. Evaluation measures
catalogue agreement, not independent identification accuracy. Both modes hide
identical preview bytes; `series_hidden` also hides inventory neighbours and
cosine similarity ≥ 0.95. That threshold is a conservative leakage control,
not proof of a duplicate. Museum-digital republications are not independent
archive evidence. Person-name voting is not exposed.

`archive-research reference-diagnostics` reads only the completed projection
(`index.sqlite`) and optionally the capture; it decodes no image and loads no model:

```sh
archive-research reference-diagnostics diagnose --work-root WORK --run LABEL
archive-research reference-diagnostics preview-gaps --capture CAPTURE
archive-research reference-diagnostics review-export --work-root WORK --run LABEL
archive-research reference-diagnostics review-score --review JUDGED.csv
```

`diagnose` adds what catalogue agreement alone cannot show: a prevalence baseline
proposing the eligible pool's most frequent labels at the visual method's per-query
count (all and high tier), precision without the most common labels, results per
collection stratum, pool/own-collection/predicted-collection date baselines, and a
holdout sweep over inventory windows with and without a session proxy (identical
date text within identical collections). `preview-gaps` classifies records without a
selected preview URL. `review-export` writes top-k neighbours with an empty
`judgement` column (`same_photograph`, `same_place_or_object`, `same_type`,
`unrelated`, `unclear`); `review-score` reports hit@k and precision@k and refuses
queries with unjudged top-k rows unless `--allow-partial` (metrics then prefixed
`partial_`). Date baselines are also reported paired on identical queries. The
index is opened read-only and receipts are never overwritten. These remain
diagnostics, not acceptance evidence.

For new archive imagery, `reference ... index-new --selection KEYS.json` imports
verified shared visual receipts in the matching model space; it never scans
masters. `propose` retains receipt provenance and unadopted suggestions. Reference
proposals are not automatically added to a curator packet. The retained
`face_index.py` is a historical draft, not an application entry point; its
identity matching/evaluation commands have not been integrated or run.

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
including `Archive-tools` on its Python path. The wheel includes the web application but does not install the legacy tool;
Pillow, numerical libraries and Django are optional dependencies, and vips is external.

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
archive-research extract \
  --roots /approved/trial/roots.toml --plan /approved/trial/plan.json --limit 2
archive-research extract \
  --roots /approved/trial/roots.toml --plan /approved/trial/plan.json
```

One manual writer starts one fresh worker per page and records progress after
each attempt. Completed and no-text receipts are reused when bindings match;
failed work is retryable. Recipe changes get separate keys. A changed plan is
refused by workers. Source changes withhold old results without overwriting their
receipt. Raw output, processed PNG, word regions, dependency versions, elapsed
time, peak process/child RSS and retained artifact bytes remain readable files.
`run-summary.json` is a rebuildable projection, not review authority. Use
`extract --summary another-run.json` for a separate trial without replacing it.
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

## Source regions and local evidence packets

An `ocr` task can additionally contain `region`, serialized with the existing
`Region` contract (`x`, `y`, `width`, `height`, normalized to the original page).
The renderer applies the declared rotation, selects this region at processing
resolution, and records the crop offset in its affine source transform. OCR word
coordinates still refer to the original page. Cropped and full-page recipes have
different cache keys; existing full-page keys remain unchanged. Region selection
for `pdf-text` is currently refused. Crop receipts contain the selected detail;
include a separate whole-page evidence view for context. `geometry.processed_region`
maps a source region into either a whole-page image or a crop for highlighting.

`archive_research.profiles` owns the combinable `buildings`, `people`,
`equipment` and `tubes` profiles under its existing subject routes. Selection does
not change route overrides, archival identity or curator decisions. Regions are
explicit/manual for semantic objects and captions. Optional text-region discovery
is described below; no automatic person/face or general layout detector runs.

`evidence.build_packet` reads explicitly selected extraction receipts and checks
their source/artifact bindings. The `research_packet.py` CLI adapter resolves
profile guidance from the route owner and writes one readable local JSON packet
into an existing work root. No network, model request, database change or export
permission is involved:

```sh
archive-research packet \
  --roots /approved/trial/roots.toml \
  --selection /approved/trial/selection.json --output local-packet.json \
  --prompt-output local-assessment-draft.json
```

Selection fields:

- `format: "archive-research-selection-1"`, `metadata_adopted: false`, an existing
  `case_key`, a `question`, and a list of distinct `profiles`.
- `evidence`: 1–32 entries with a receipt `key`, `label`, `kind`, `lineage` and
  `attribution`; optional `region` and a subset of the packet's `profiles`.
  Kinds are `scene`, `detail`, `caption`, `inscription`, `document`, `person`, `face`.
  A region filters extracted words by centre without modifying their spelling or
  original coordinates. It highlights the retained image; HTTP does not crop it.
  Whole-page entries may include `discovery_key`, the key of a `text-regions`
  receipt for exactly the same source page. The workbench checks that receipt and
  its artifacts, overlays candidates and serves only already prepared crops.
- Optional `caption_links`: `caption`/`subject` reference zero-based evidence
  positions; `basis` is `explicit-caption` or `proximity`, with `quote`, `position`
  and `note`. Explicit quotes must occur in the selected extracted text; person
  links also require a position. Both remain proposals, never confirmed identity.
- Optional `hypotheses`: `claim`, `supports` and `contradicts`, referencing evidence
  positions. Unsupported or invalid references are refused, not invented.
- Optional `cross_references`: `source`/`target` evidence positions, `basis`
  (`textual` or `visual`), `quote` and `note`. Textual quotes must occur in the
  source evidence; visual links have an empty quote. Both are unreviewed leads.
- Optional `code_readings`: `evidence`, `literal_reading`, and `dictionary` entries
  with `value` and their source `evidence`. Both readings must occur in the selected
  text. Only documented candidates differing by `0/O` or `1/I` are suggested;
  original readings stay intact. This is not a general OCR corrector.
- Optional `omissions`: explicit missing/omitted evidence and limitations.

Packets retain separate OCR variants, receipt digests, source references,
profile guidance, caption/hypothesis provenance and unavailable-source reasons.
Page/OCR variants are grouped as one source; unknown copies/republications are
not automatically deduplicated. Refresh refuses changed receipts/profile guidance,
and rechecks current availability. Media reads verify the prepared-image digest.
`external_submission_authorized` is always false in this local implementation.

Optional `--prompt-output` prepares an assessment draft with instructions, source
coordinates, raw text, hypotheses, cross-references, omissions and a hash-bound
PNG attachment manifest. It refreshes the packet and verifies image bytes, refusing
unavailable/changed evidence. This is a local draft, not an API request: it chooses
no model, uploads nothing and grants no external-processing permission. Discovery
subcrops/enhancements are explicitly omitted unless selected separately as evidence.
The separate assessment transport below adds bounded attachments and a single-call
adapter; live account processing remains unqualified. Evidence is treated as
untrusted content, not model instructions.

`sift_matching.compare_prepared` reuses the existing SIFT/RANSAC thresholds on
caller-verified prepared views without creating another cache directory. It records
input hashes, versions, thresholds, inliers and coverage. Direct orientation only;
geometric failure does not disprove a site match between a drawing and a photograph.

The pilot's optional `research_packet = "local-packet.json"` project setting
selects a flat filename under the configured extraction `artifacts` root.
`/research/packet/` presents it read-only with original-source links and source
regions. It never runs extraction, submits an assessment or saves a review.

## Local visual features, PDF layout and bounded retrieval

These adapters share `Source`, `Page`, `Region`, `Recipe` and `Result` with OCR.
The runner only dispatches work and retains receipts. `visual_features.py` owns local
OpenCLIP inference; `layout.py` owns PDF placements and caption candidates;
`receipts.py` verifies downstream evidence; `retrieval.py` builds and queries a
readable bounded index. The subject profiles remain in `research_routes.py`.

`visual` tasks accept whole pages or explicit regions and require a `settings.model`
Source record with a SHA-256 fingerprint. The model is an already available
ViT-B-32 safetensors file, read through a configured read root. Symlinks are not
followed; bind the canonical cache blob. Inference loads bundled architecture
configuration and local safe tensors, never a remote model name. The qualified
CPU environment uses `requirements-visual.txt`, disables MKLDNN and retains
512-dimensional normalized vectors. Model, preprocessing and dependency versions
are fingerprinted. A successful empty OCR word list means visual processing, not
that the image contains no text. Center-crop preprocessing can omit edge details;
index source-bound detail crops separately.

`pdf-layout` tasks use installed Poppler to locate embedded raster placements and
native text on one selected PDF page. Returned crops retain original page regions,
including rotated and mirrored image placements. Mixed-font caption fragments are
grouped geometrically. Nearby text blocks remain tentative caption candidates.
Full-page rasters, vector drawings and missing native text remain explicit coverage
gaps: this is not a general scanned-newspaper layout model. Raw XML, page previews,
figure crops, candidate text and omissions are retained in flat work artifacts.

`layout.visual_tasks(roots, work, receipt_key, model)` derives explicit visual crop
tasks from a verified layout receipt. The originating receipt digest, figure position
and source region are bound into the recipe and checked before cache reuse.

`archive-research retrieve` accepts a selection containing `keys`,
`visual_queries`, `text_queries` and `limit`. It writes a flat JSON index and search
results under the existing artifact root. Both include coverage and unavailable
evidence. Visual comparison excludes the same archival object and isolates model
spaces; text queries return literal snippets. Optional `--sift` verifies selected
visual leads using the existing geometric comparator. Scores and geometry never
adopt identity, dates, captions or metadata.

```sh
archive-research retrieve \
  --roots /approved/trial/roots.toml --selection /approved/trial/retrieval.json \
  --index-output local-index.json --output local-leads.json --sift
```

This is a bounded, rebuildable index over explicitly selected receipts, not a new
database or a claim of archive-wide coverage. Historical OpenCLIP caches are not
silently mixed with the new model/preprocessing bindings. `refresh_index` rejects
changed receipts, source/artifact bindings or edited index content. HTTP never
starts extraction or indexing. Broader incremental indexing and scanned-page
segmentation require their own measured slices.

## Automatic text-region discovery

For an explicitly selected complete marking line, `extractor: "line-ocr"`
compares local Tesseract LSTM language models and OpenCV Zoo CRNN EN. It accepts
whole image tasks with `settings.lines`: 1–8 labelled convex quadrilaterals in
normalized source coordinates. Selections are manual, not automatic grouping;
separate logos from the line and inspect the retained crop before interpreting
results. Crops use the existing padded affine rectangle and source transform;
this does not correct arbitrary curved-surface or perspective distortion.

Bind every language through `settings.tesseract_models` (language → Artifact)
and the independent recognizer through `settings.crnn_model`. Every model needs
a SHA-256 digest, verified before inference and cache reuse. Tesseract files
must be named `rus.traineddata`/`eng.traineddata` in one explicitly bound existing
directory. No global model installation or automatic download occurs. Default
languages are `rus+eng`; only these two languages and their ordering variants
are currently supported. Explicit custom tessdata uses LSTM mode and ALTO output.

Default line preparations are original, levels and levels-unsharp, with 0/180
degree and original/inverted readings. Each gets Tesseract modes 7/13 and one
CRNN pass: 36 attempts per line. Line images are enlarged toward 64 pixels
high, capped at 3×. CRNN then uses its fixed 100×32 grayscale input and greedy CTC
decoder with digits/lowercase Latin vocabulary; it cannot transcribe Cyrillic.
It returns whole-line text without invented character coordinates/confidence.
Engine names and raw alternatives remain separate. Model/recipe/crop changes
invalidate cache reuse; no dictionary candidate is supplied to recognition.
Timeouts bound scheduling and Tesseract subprocesses, not a running native CRNN
call. Model initialization and preparation are outside the recognition deadline.
Coverage explicitly says selected lines only, even when every attempt completes.

Choose `extractor: "text-regions"` on a whole-page task. It needs an explicit
`settings.model` artifact (`root`, relative `path`, `kind`, `sha256`) in an approved
existing work root. No download or installation occurs in the runner. The
[OpenCV Zoo PP-OCRv3 detector](https://github.com/opencv/opencv_zoo/tree/main/models/text_detection_ppocr)
is Apache-2.0; retain its license and download provenance beside the private model.
This adapter runs the detector through OpenCV DNN and recognizes crops with
Tesseract. It does not install PaddleOCR or evaluate its recognition model.

The default scan uses a whole-page overview and overlapping 736-pixel tiles at
0/90 degrees (160-pixel overlap; at most 64 tiles). Detection polygons map back
to original source coordinates. Overlapping candidates retain pass provenance;
nested fragments can remain separate. Up to 32 regions get padded affine crops
and eight independent Tesseract attempts: 0/180 degrees, original/inverted
polarity, modes 7/11. Enlargement is geometric, with no generated detail. These
defaults and the detector thresholds, model digest and dependency versions form
the recipe. `text_regions.DEFAULTS` lists the accepted bounded settings.

Optional `settings.enhancements` selects distinct recipes, with `original`
required first. The default remains `["original"]`. Available alternatives are
`levels` (1st/99th percentile stretch), `gamma-light` (power .75), `gamma-dark`
(power 1.35), `clahe` (clip limit 2, 4×4 tiles), `unsharp` (Gaussian sigma 1,
amount .75) and `levels-unsharp`. Alternatives use grayscale; each starts from
the same unchanged crop. Photometric changes preserve the crop's coordinates.
Settings and exact recipe parameters are fingerprinted in recipe version 2;
earlier receipts remain readable. Measured levels and saturation fractions are
recorded, with constant-intensity stretching skipped explicitly.

Every chosen recipe receives the configured orientation/polarity/mode attempts
within the existing timeout; seven recipes therefore request 56 attempts per
candidate at default recognition settings. Retained previews can be compared
in the packet, and every reading names its enhancement. These previews precede
the separately recorded OCR rotation, polarity and resize. Check original
strokes for clipping, grain and halos; no variant is automatically preferred or
accepted as a transcription. This is an optional diagnostic, not a claim that
enhancement improves recognition on all sources.

Each receipt retains candidate polygons, crop transforms, raw readings and
coverage: omitted/failed passes, detector limits, unreadable/conflicting regions,
failed recognition and candidates not attempted. Raw ALTO stays in `.regions.json`;
prepared PNG crops are separate artifacts. A completed scan does not prove that
all markings were found. Nonempty or agreeing readings do not establish accuracy,
or whether a marking is a model, serial or batch number. The timeout controls
scheduling and recognition calls; it does not interrupt a running native detector
call. Rendering/model initialization are outside that processing time budget.

An evidence selection can attach this receipt using `discovery_key`. The packet
shows coverage, overlays and candidate crops, including unreadable candidates.
Nothing is rerun during HTTP requests. Manual regions and original OCR remain
available alongside discovery, and all results remain unadopted.

`text_region_evaluation.evaluate(case, receipt, threshold, baseline)` provides a
small diagnostic using one-to-one bounding-box matching. Only fully annotated
examples permit precision. Exact matches among *any* OCR alternatives are an
oracle diagnostic, not selected-transcription accuracy. Keep held-out examples
separate and never claim collection accuracy from a small synthetic trial.

## Verification

Run the synthetic boundary tests from the source root:

```sh
python3 -B -m unittest discover -s tests -p test_archive_research.py -v
python3 -B -m unittest discover -s tests -p test_research_units.py -v
python3 -B -m unittest discover -s tests -p test_positioned_extraction.py -v
python3 -B -m unittest discover -s tests -p test_research_evidence.py -v
python3 -B -m unittest discover -s tests -p test_text_region_evaluation.py -v
# Use the qualified OpenCV/NumPy/Pillow environment for detector geometry tests:
python3 -B -m unittest discover -s tests -p test_text_regions.py -v
```

The core import test uses a fresh interpreter without site packages. The one real
legacy-renderer test additionally requires the existing workstation Pillow/vips;
it renders a generated TIFF in a disposable temporary directory. All other files
and mounts are synthetic or mocked. No archive image, live review, database or
service is involved. Existing visual and photo-contract suites remain separate.


## Scanned figures, caption bands and bounded assessment transport

The `scan-layout` adapter is separate from embedded PDF placement extraction. It
uses the existing OpenCV/NumPy/Pillow environment, local texture candidates and
Tesseract ALTO illustration boxes. Each task binds an existing complete-page
`ocr` receipt through `settings.ocr_origin = {key, receipt_sha256}`. All parent
receipts, source bindings and artifact hashes are checked before reuse and by
retrieval. No OCR is silently repeated. Figure crops map back to original page
coordinates, including embedded PDF rotations; full semantic coverage stays false.

Explicit `ocr` tasks may additionally set `thresholding_method` to 0 (Otsu),
1 (Leptonica Otsu), or 2 (Sauvola). This creates a distinct recipe and preserves
older readings. More returned words do not establish better transcription.
The defaults and old recipe keys remain unchanged when this option is absent.

`caption_regions.caption_tasks(roots, work, key, figures=None,
sides=('below','above'))` proposes geometric caption-band OCR tasks. It preserves
parent receipt/figure/side provenance and refuses altered derived regions. Bands
can include unrelated text or clip captions; their position is not an adopted
caption relationship. Explicit figure selection remains recorded in the plan.

`layout.visual_tasks` also accepts scan-layout receipts. Candidate crops can feed
the same visual index; they remain candidates even when retrieval finds a match.
The bounded development comparison found all eight coarse illustration targets
but nine additional candidates on four pages. This is insufficient for unattended
figure acceptance and is not a held-out accuracy claim. The private handoff owns
exact evaluation evidence and the current selection.

`external_evidence.py` reads explicitly retained reference records. Each binds a
snapshot of the source HTML and its image through existing Source records,
retains URL/access date/credit/reuse terms, validates literal quotations, and
keeps researcher summaries and comparisons distinct from machine transcription.
It performs no network fetch or HTML execution. Evidence selections may attach a
`web_reference` Source binding to such a record; the image source must match.
Search snippets and local museum-digital republications are not independent
confirmation. Reference image-use permission is separate from download access.

`assessment_io.py` prepares bounded JPEG attachments and Responses API JSON with
`store=false`, an explicit model and output-token limit, no tools, and a strict
claim/citation/uncertainty schema. Filesystem locators are omitted from the API
context. Original and encoded image hashes, dimensions, request/packet hashes and
payload sizes are retained. `--omit-image N` keeps that source text/context while explicitly withholding its
image; the request labels it as not attached. The optional HTTP adapter refuses redirects and sends
one exact authorized request without retries. The CLI saves an attempt before
transmission and preserves raw responses; a timeout is an unknown outcome, never
permission to retry. No secret is written to records.

```sh
archive-research assess --roots ROOTS --packet PACKET.json \
  --output TRANSPORT.json prepare --model MODEL --request-output REQUEST.json
archive-research assess --roots ROOTS --packet PACKET.json \
  --output ASSESSMENT.json validate --response RESPONSE.json --transport TRANSPORT.json
```

Both commands above are local. After permission for the exact selected material
and external API processing, the `submit` subcommand takes `--request`,
`--transport`, `--response-output` and `--authorized-request-sha256`. The default
`--provider responses-api` requires `OPENAI_API_KEY` in the process environment.
Alternatively, `--provider codex-chatgpt --codex /absolute/path/to/codex` uses an
existing ChatGPT login without an API key. Both reconstruct the request against live
evidence before sending and refuse existing output paths. Model/account access,
data controls, material rights and an agreed spend/request limit must be resolved
before that action. `store=false` alone is not account-retention qualification.

The Codex adapter supplies the identical prepared JPEG bytes and evidence text,
with an explicit image-order map, schema and model. It disables tools and project
instructions, retains native CLI events/output and labels results `codex-chatgpt`.
Completed turns, absence of tool calls and final-output agreement are checked.
The output-token target is advisory for this transport; API `store=false` and
hard output caps are not CLI settings. Account controls and subscription limits
apply. No automatic retries or model fallback are made. The CLI can internally
resize image inputs; attachment preservation is not a claim about model pixels.

Response validation rejects failures/refusals, incomplete responses, unexpected
model/tool output, unknown/duplicate citations and uncited claims. It checks
citation structure, not whether an image establishes a historical claim. Usage
is retained; actual billing and independent curator accuracy remain separate.
The workbench may display an unreviewed response using paired project settings
`research_assessment_response` and `research_assessment_transport`; mismatched
packet bindings or unavailable evidence withhold it. Existing reviewer records
are never written by this path.

API shape checked against the [Responses reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
on 17 November 2026. The HTTP path remains unqualified by a live response.
Codex saved-login behavior is documented in
[non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).
