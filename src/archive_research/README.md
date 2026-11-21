# Archive research application

One installed Python application owns local extraction, cross-unit retrieval,
evidence packets, assessment and the Django workbench. Private project/machine
configuration selects existing data and roots. This guide describes the public
interfaces and contracts; development plans and operating records remain local.

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

## Design contracts

- **Identity and evidence:** preserve existing unit, object and representation
  identifiers. Paths, timestamps and device/inode numbers locate or check files;
  they do not define archival identity. Identical bytes do not establish that two
  records describe the same object. Bind findings to their source, member,
  page/frame and region, keeping observations, hypotheses and adopted metadata
  distinct.
- **Processing:** extractors return versioned, source-bound receipts and explicit
  coverage. Recipes bind relevant settings, dependencies and model hashes. Local
  model files are explicit inputs; installing the application does not download
  weights. HTTP requests read prepared results and never start processing or
  model submissions.
- **Authority:** human-readable source bindings, research and review records are
  authoritative. SQLite and search/vector indexes are rebuildable projections.
  Research suggestions do not select Masters, adopt catalogue metadata or
  authorize accession, publication or deletion. The retained-tools guide records
  the [archive invariants](../../tools/archive-preservation/README.md#current-rules).
- **Safe writes:** validate temporary output on the destination filesystem before
  replacement. Never fall back to copying over a live file when atomic replacement
  fails. Report uncertain durability if synchronization fails after replacement;
  do not blindly repeat an already applied write. Atomicity, durability and writer
  exclusion require separate checks.
- **Portable deployment:** make roots and environments explicit. Detect case,
  Unicode, reserved-name and path-length collisions without silently renaming or
  merging sources. Preserve encodings, timestamp meanings, coordinate units and
  representation order. Verify effective native filesystem access separately from
  application login/roles; do not translate Unix permission bits into assumptions
  about another platform. Qualify locking, replacement and database recovery on
  the actual storage. Current Unix-specific dependencies still need isolation and
  qualification before another platform is supported.

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

### Immutable results and prepared search (0.4)

New successful extraction retains exact receipt and artifact bytes before updating
the current source binding. `result-<digest>.json` describes computation independently
of collection membership, canonical UID mapping or logical order. Exact historical
receipt snapshots keep those original bindings. `computation-<digest>.json` is only
a disposable reuse lookup: repeating the selected task repairs a missing lookup
without rerunning valid extraction. A lookup write failure reports saved evidence
with a warning. No legacy corpus is converted automatically and no UID is allocated.

`archive-research evidence --roots ROOTS --snapshot SHA` reads one retained snapshot.
It verifies retained bytes; it does not claim today's source file is available.
Normal selected evidence access still verifies its current source. Neither content
reuse nor byte equality merges archival objects.

`index` explicitly refreshes a flat search projection from selected receipt keys,
catalogue CSV snapshots/field mappings and existing finding history. It preserves
literal wording, provenance, unresolved identities and available/offline coverage.
Queries read this projection without reopening the corpus or running extraction.
Text search requires a strict majority of distinct query terms (both terms for a
two-term query), after removing common name titles. Coverage ranks first, then
exact wording above adjacent name/initial, long-word compound/stem and optional
OCR/spelling alternatives. Mixed letter/number query terms remain required;
canonical UID queries require the exact identifier. Names with initials are
ambiguous retrieval leads, not person identities: a common surname alone cannot
satisfy a multi-term name query, and context must be reviewed. Explicit author or
Bearbeiter fields/labels and credited creators receive a small boost. OCR alternatives are tried even
when the exact word exists elsewhere. These are bounded heuristics, not a German
linguistic model or a calibrated identity score.

Results expose matched/missing terms, literal field offsets and the matching
method. Snippets center on the densest local match and safely highlight original
text, including OCR errors. Existing projections need no rebuild for these query
changes. Page-level hits remain; block-level indexing is deferred.
Visual search keeps same-document candidates and
reports incompatible model spaces. Corrections require an explicit refresh.
Existing neutral UIDs can be mapped explicitly; there is no production allocator.

Catalogue selections map literal CSV columns to descriptive fields, including
creators, dates, places, subjects, relationships and inscriptions where present.
Optional `path_fields` name unit-relative representation columns; the catalogue's
parent path and named root bind each representation to its existing object.
There are no basename, title or fuzzy identity joins. `group_objects: true`
combines representation rows only when their existing object key and selected
descriptions agree; differing accounts and rows without an object key stay
separate. Row numbers, row checksums and the catalogue snapshot remain recorded.
An optional `object_metadata` mapping (`source`, `object_field`, `fields`) joins
an explicitly selected object table by its unique existing key. It cannot
overwrite catalogue fields; ambiguous supplementary keys refuse the refresh.

The workbench reads these same prepared entries for each comparison side.
Existing catalogue wording, attribution and differing accounts remain separate
from observations and reviewed findings. A missing link is explicit; neither OCR
nor online source availability is required to display retained descriptions.
`refresh_catalogues` replaces only selected catalogue entries, preserving
extractions, findings and feedback, and validates all inputs before returning a
replacement. The caller must publish it atomically under the existing research
lock. Coverage distinguishes CSV rows from grouped descriptive records; neither
is an inferred count of distinct archival objects or whole-archive coverage.

`extract --chunk-size 2 --worker-timeout 900` processes a bounded chunk with one
cached visual encoder per model space and saves per-item progress. Worker timeout
or interruption preserves the completed prefix; retry reuses verified results.
Limits are 1–16 items per chunk and 1–7,200 seconds per worker. Chunk size defaults
to one. Model files remain explicit local inputs; no download or HTTP processing
is introduced. Synthetic checks establish behavior, not recognition accuracy.

The optional project `research_index` names the prepared search JSON in the existing
artifact root. Research search then combines catalogue text, extraction and findings.
The ordinary case view presents findings and citations first; `?inspect=1` expands
the same evidence details. `/research/evidence/SHA/` opens an authenticated retained
snapshot with an explicit historical-source qualification.

Unsigned 16-bit grayscale TIFFs use a recorded linear 0..65535 → 0..255 display
conversion; 16-bit MinIsWhite files are inverted (Pillow does this only for 8-bit)
and the observed value range is recorded in the transform step. Greyscale sources
are stored as 8-bit `L` prepared views; consumers needing RGB convert on read
(render policy version 3). Signed, float and high-depth multichannel input
is refused pending an explicit decoder/range. Original data is unchanged; this
is neither contrast stretching nor a master conversion. The preparation policy
is fingerprinted in new extraction recipes, so old clipped receipts are not reused
by new extraction runs. Existing selected old receipts remain readable and need
an explicit audit/re-extraction before trusting affected results.

Cache keys (version 2) bind unit/object/representation identity, member/page
geometry and, with a stored SHA-256, content instead of locator: touching, moving,
copying or relabelling an identical file keeps its receipts. On reuse at a new
locator or stamp the stored hash is verified once and the receipt is refiled with
the current page (`rebound_from` keeps the previous binding); nothing is
re-extracted. Without a stored hash the locator and stamp remain bound. Receipts
written under version-1 keys are found and refiled. Unfinished work (deadline,
error passes or recognizer errors) is retried; deterministic limits are reused.
OCR and text-region recipes bind the SHA-256 of each Tesseract traineddata file.

Matching page/frame, crop, DPI, rotation and decoder versions share a checked
`prepared-*.png` and JSON binding. Different extractors reuse that view. Pillow
still decodes the selected source image in memory on its first render; this is
not a streaming TIFF decoder. PDFs and TIFF frame selection use the shared page
contract. Letterbox framing is the default for new visual embeddings; old centre
crop vectors remain in a different model space. Fine details still need crops.

The `faces` extractor accepts a fingerprinted local YuNet ONNX `Source`, uses
native-resolution overlapping tiles plus an overview. Orthogonal rotations are
searched on the whole-image view; tiles run upright (`tile_rotations`) plus the
orientation in which the overview found faces, so large masters stay within the
view budget. A budget stop is final and reused; a timeout is retried. It emits source-coordinate regions and explicit view coverage, with
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
This example describes the root format using placeholders. Select existing,
approved directories; this is not an operational configuration:

```toml
[roots.archive]
path = "/path/to/archive"
access = "read"

[roots.source]
path = "/path/to/protected-source"
access = "read"
# Configure mount_target, mount_source, mount_fstype and mount_options
# from the verified deployment requirements where mount checks are needed.

[roots.artifacts]
path = "/path/to/research-output"
access = "work"
min_free_bytes = 1073741824
```

Names are logical locators; root paths are deployment configuration. Read and
work roots cannot overlap. Work roots cannot overlap protected workstation media.
Root/member symlinks, path traversal, absent mounts and insufficient configured
free space are refused. A mountpoint directory that remains after an unmount is
not a valid mounted destination. Checks repeat when paths are used.
Deployment-specific protected device/target/filesystem/read-only safeguards cannot
be weakened by omitting a setting or choosing a broader read root. Locations on
guarded volumes require their verified mounts. Other compatible locations use
their explicit mount constraints. Filesystem names and mount paths are not part
of object identity.

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

### Document layout and structured OCR quality trials

Recognition, reading order, geometry and complete available-page coverage are
indexing-quality requirements, not just workbench presentation requirements.
Qualify bounded reports against retained source images before scaling. A successful
processor or nonempty text does not establish completeness. Keep old results and
measure critical dates, identifiers, formulas and units separately from average
character error. Region detection must be checked against full-page readings:
report forms can be misclassified as figures or tables.

OCR-only `settings.image_preparation` can select `rgb-v1`, `gray-v1`,
`gray-up2-v1`, `gray-close2-v1` or `gray-up2-close2-v1`. These are opt-in trials,
mutually exclusive with the earlier contrast normalization. Grayscale uses Pillow
luminance conversion without stretching; 2x resizing uses Lanczos with the inverse
source transform rescaled. PNG DPI is explicitly set to render DPI times scale.
Dark-ink 2x2 grayscale closing inverts the foreground, dilates then erodes with
complementary anchors to avoid a pixel translation, and inverts back. It may also
join neighbouring marks, so compare real passages before choosing it. The existing
`thresholding_method=2` requests Tesseract's internal Sauvola; grayscale input still
undergoes internal thresholding. Rendering at 400 dpi does not constitute rescanning
the source or recovering detail absent from its embedded images. Each derivative
retains its own recipe, source coordinates and original unmodified input.

OCR recipe v4 retains ALTO blocks/lines and word `block_index`, `line_index` and
`word_index`, alongside source coordinates. Search preserves those block boundaries
instead of joining nearby rows across columns. This cannot repair a block that
the recognizer itself merged across captions; those need separate region OCR.
Optional `settings.normalization = "local-red-v1"` is an explicitly selected
contrast trial for faded coloured paper, using existing NumPy/OpenCV dependencies.
It writes a separate same-size image: red channel, Gaussian background sigma
`30 * dpi / 300`, gain 5, offset 230, clipped to 8 bits. Its recipe fingerprints
dependencies and the transform records the operation. It is not a default or a
universal improvement; compare against untreated input. Preserve the original
source, prepared render and each page's declared rotation.
`clockwise` is an additional editorial rotation: PDF embedded rotation is already
applied by the renderer. Do not copy a workbench display rotation into that setting.
Inspect the prepared image and total transform before comparing OCR variants.

For an explicit language-model comparison, set `TESSDATA_PREFIX` only for the
bounded extraction process and its workers. The directory must contain every
requested `<language>.traineddata`; the existing recipe records their SHA256
fingerprints, so `fast` and `best` results cannot share a cache key. Keep page
preparation, language order and PSM fixed. ALTO output is requested with
`tessedit_create_alto=1`, without depending on a distribution-specific
`configs/alto` file in that model directory. Do not replace system language files
or infer an archive-wide accuracy improvement from a small development sample.

`document-layout` is a separate optional adapter for DocLayout-YOLO 0.0.4.
Its `settings.model` is a fingerprinted local `Source`, never a model-hub name.
The bounded recipe records CPU/native-PyTorch execution (MKLDNN disabled), input
size, confidence, region limit and dependency versions. Model regions and crops
are proposals; detection does not establish article order, completeness or a
caption relationship. Recipe v2 blocks Python socket connections during import
and inference, including upstream connectivity/analytics hooks. The upstream implementation is AGPL-3.0; the selected
DocStructBench model card declares Apache-2.0. Keep the download URL, revision,
hash and licences with the work inputs. Do not install the training stack or
replace an existing OpenCV/torch runtime to run a workbench request.

The legacy helper
`extractors.document_layout.ocr_tasks(roots, work, key, psms=(4,6), languages='deu')`
derives ordinary OCR tasks from the selected layout receipt. Strongly overlapping
same-class detections and near-identical differently labelled text detections are
suppressed for OCR/assembly but preserved in the raw layout. Tables are included
as literal OCR text; cell/column semantics remain unverified. Near-identical
illustration/abandon detections are logged as suppressed image-text proposals,
with a reminder to inspect for omitted labels. This is not a proof of non-text.
Each crop binds its `layout_origin` (receipt and block). A different
OCR mode creates another receipt without rerunning detection. Prepared crops reuse
the verified full-page image, preserving the direct renderer's pixels and affine
mapping. PSM 4 assumes a column, PSM 6 a uniform block; neither is universally best.

`document-text` assembles one explicitly selected OCR receipt per retained text
block. Settings include `layout_origin`, `ocr_origins`, a complete `block_order`,
and optionally a fingerprinted annotation `Source`. Annotations bind the layout
digest, order, article groups and caption links; assistant inspection is not an
operator review. Assembly maps crop words/lines through their full transforms,
creates unique IDs and explicit reading order in ALTO 4.4, and retains structured
JSON with the originating receipts. Text remains unqualified outside measured
passages; the result status is deliberately partial. No PDF is rewritten.

`document_text.propose_order` can anchor regions to a fresh whole-page OCR
receipt and insert missing headings geometrically. Supplying that receipt as
`order_origin` makes the proposed order reproducible and checked before reuse.
Unanchored regions are listed; the proposal inherits whole-page segmentation
errors and does not claim verified article order.

For report caption grids, `document_text.geometric_order` proposes order using
horizontal whitespace cuts between coherent regions, then vertical gutters.
`order_method: "geometric-cuts-v1"` validates that declared order and cannot be
combined with `order_origin`. Document-text recipe v2 retains block/line/word
associations through assembly. No geometric proposal proves semantic order;
full-width headings, neighbouring columns, bottom captions and footers need
explicit coverage checks. Layout recipe v4 also permits the contrast trial and
records table eligibility; old layout receipts remain readable.

Search and the workbench preserve assembled line/block boundaries. Existing
native/whole-page receipts remain available. Explicit selection, not OCR confidence,
chooses the displayed result; a saved workbench OCR cache keeps precedence.
`extractors.document_evaluation` measures selected-region character/word errors
and article-order inversions separately. Checked development examples are not
independent acceptance. Fine-tuning requires demonstrated layout failures and a
separate evaluation set; it is not triggered by an OCR error count.

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

## Optional R&D region trial

The `rd_reports` profile preserves word-positioned text/captions and routes formula,
table and illustration blocks separately. `scientific-region` accepts an explicit
source-page crop and a fingerprinted local PaddleOCR-VL-1.5 manifest. Formula
LaTeX, raw model output, token-comparison diagnostics and inert table HTML remain
unreviewed feature sidecars; no replacement OCR words or inferred word boxes are
created. `scientific-table` converts a retained recognition receipt's HTML or
OTSL grid to inert HTML without repeating inference. Empty cells and rectangular
row/column spans are preserved as proposed; malformed grids are refused.
Handwriting flags skip model recognition; automatic handwriting detection remains
unimplemented. OCR with `character_boxes: true` retains ALTO and character hOCR
from the same pass. Element/count token hypotheses and below-baseline character
geometry propose inline crops, including inside automatically detected captions.
OCR omissions still cause missed candidates. Caption/header/floating hOCR lines
are retained; zero-area character boxes preserve text but cannot supply a crop.

`scientific_tokens` validates simple, case-sensitive element/count sequences,
retaining alternatives for OCR confusions. It does not parse general equations,
charges or grouped chemistry. Unique agreement of both engines lowers review
priority; a confusion repair additionally needs subscript geometry. Raw readings
remain unchanged, and agreement is not acceptance. The search projection retains
engine-labelled normalized proposals and their disagreement evidence; a uniquely
agreed reading also has plain and Unicode fields. No catalogue value is adopted.

Recognized number/unit expressions stay in text. Unknown units and number-led
damaged formulas must not be silently declared quantities. Form classification
uses explicit form wording, preferably from full-page context. `ruled-table`
keeps forms as key/value proposals and otherwise tests closed ruled cells with
OpenCV, then runs Tesseract per cell, retaining empty cells and source coordinates.
A detected grid is still an unreviewed table hypothesis; unruled tables need
confirmation before invoking vision. Cropping away context can defeat routing.
Closed ruled regions can also contain multiple logical rows. Original OCR words
inside the detected extent but outside all detected cells are retained in
`unassigned_source_words`, with an explicit incomplete-extraction warning. Zero
unassigned words does not prove completeness, because source OCR can omit text.

One model is loaded per bounded extraction worker and reused for its crops;
receipts separate loading, input preparation and generation time. Use the existing
bounded `extract --chunk-size` option, not an indefinite model service. Formula
pixel rendering is now optional (`render_check: true`) and is not the default
comparison. The private `rd-token-profile-report-20261120.md` records the bounded
trial, including misses, failed controls and CPU cost; full indexing stays blocked
on material-specific validation.

The local optional runtime uses Transformers 4.55.4, tokenizers 0.21.4,
huggingface-hub 0.36.2, sentencepiece 0.2.1, protobuf 6.33.1 and einops 0.8.1 alongside the existing
CPU vision dependencies. Models are pinned/downloaded separately, all model files
are verified, and inference uses local files with Python network hooks denied.
There is no server, external submission or automatic dependency/model installer.

## Optional German spelling layer

The `spelling` extractor derives text from an immutable `ocr` or `document-text`
receipt. Install the optional `spelling` extra (`symspellpy==6.9.0`); there is no
automatic installer or network lookup in extraction. Supply fingerprinted Sources
for `dictionary` (German `word frequency` lines) and `custom_lexicon` (JSON with
`terms`, `names`, and optional `provenance`), plus the exact `ocr_origin`.
`protected_regions` optionally identifies formulas, handwriting or other text
that must not be changed. Character-OCR formula/quantity membership is inherited
when word sequences agree, falling back to region protection otherwise.
The same page and crop must be bound on both receipts. The lexicon is reused
within the existing bounded worker. No new service or review subsystem is needed.

Processing order:

1. Rejoin alphabetic line-break hyphenation within one OCR block, including OCR
   dash variants and surrounding parentheses. Retain every original word index,
   raw spelling and separate source box. Never join across block/column boundaries,
   missing line metadata, or continuation words such as `und`/`oder`.
2. Preserve known German words, archive terms, listed names, acronyms, units,
   identifiers and protected regions. Validate unknown compounds using exact
   dictionary parts (minimum four letters, at most four parts, explicit linking
   forms). Keep the compound intact; no global `lookup_compound` or
   `word_segmentation` rewrite is used.
3. Retrieve **all** SymSpell suggestions through distance 2. Exactly one distance-1
   candidate may replace an ordinary-text token in the derived layer, preserving
   initial capitalization and punctuation. Candidate frequency only orders the
   suggestions; it never hides alternatives. Protected-name/unit/acronym targets,
   mixed case, ambiguous distance-1 results, distance-2 results, unknown words and
   unjoined fragments remain review items. SymSpell uses Damerau OSA distance,
   including adjacent transpositions. Listed names are protected; this is not NER.

`feature.corrected_tokens` contains `raw_indices`, `raw_texts`, `regions`, status
and suggestions. `feature.corrected_text` is searchable on an explicitly refreshed
projection; original receipt words and ALTO remain literal. Search evidence retains
the complete mapping and review status. The later review stage is represented by
`review_items`; no automatic model/remote dispatch or workbench diagnostics are
introduced. Automatic changes are derived processing, not catalogue adoption.

The private trial's `spelling-trial-report-20261120.md` records the pinned German
100k frequency list, editable archive lexicon, checked-caption results and limits.
The list must include domain/historical forms: general-dictionary uniqueness alone
can wrongly change valid `gedampft` into `gedämpft`. Expand the custom lexicon from
checked sources, not unreviewed OCR. Unknown compounds and names still require
material-specific validation before full indexing.

`scientific_document.correction_proposal` separately guards explicit proposed edits using
protected source regions and technical-token checks, retaining original OCR.
It is not an automatic spelling-correction engine. Caller-selected regions must
also protect units/names/identifiers whose OCR contains only letters. Rendering
supports Matplotlib MathText only; unsupported LaTeX/mhchem stays unrendered.
Foreground image similarity is an uncalibrated mismatch signal, never automatic
acceptance. Table markup validity does not certify spans or cell text.

### OCR material presets

`archive-research ocr-profiles` lists the four explicit version-1 presets owned by
`profiles.py`: `clean_print`, `historical_newspaper`, `rd_reports` and `brigadebuch`. These are
extraction settings and scope checks, distinct from research-subject guidance.
They are candidate recipes, not certificates of recognition accuracy.

Add a versioned selection to each existing extraction-plan task:

```json
{
  "id": "selected-page-ocr",
  "extractor": "ocr",
  "ocr_profile": {"name": "clean_print", "version": 1},
  "page": "the existing source-bound Page record goes here",
  "settings": {"languages": "deu+eng", "clockwise": 0}
}
```

The `page` placeholder must be replaced by the ordinary Page object, not a path.
The existing `extract --plan ...` command executes the plan. No web request starts
OCR, and no preset writes into the live search index or reviewer records.

Their ordinary-print component uses 300-DPI grayscale Tesseract, PSM 3 for a whole-page baseline and PSM 6
for an explicitly bound text crop. Language selection, rotation and bounded timeout
remain explicit; traineddata contents and tool versions enter each receipt. The
preset does not download models or silently choose fast versus best language data.
The local validation run uses the retained best German/English files.

Clean print supports native PDF extraction and OCR. Newspapers and R&D additionally
support existing `document-layout` and `document-text` stages. Newspaper layout uses
the original image; R&D retains the tested `local-red-v1` layout preparation. Both
require explicit reading-order evidence for assembly; a geometric order remains
an unreviewed proposal. Inspect a PDF text layer before selecting it: nonempty text
alone never suppresses OCR or establishes accuracy.

For regional tasks, use the existing layout helper with one selected reading:

```python
ocr_tasks(roots, "artifacts", layout_key, psms=(6,), languages="deu+eng",
          ocr_profile={"name": "rd_reports", "version": 1})
```

Carry that same profile/version into the assembly task. Existing layout receipts
may be reused when their preparation matches; every contributing OCR receipt must
have the selected profile. The assembly adapter already requires all selected
text blocks exactly once and verifies crop coordinates and origin bindings.

Preset conflicts (different DPI, thresholding/upscaling experiments or PSM), unknown
versions and specialist recognizers are refused before a batch starts. Unprofiled
plans retain their old semantics and cache keys. Profile identity is part of each
new recipe/cache key. Raw ALTO and coordinates remain unchanged by postprocessing:
spelling is a separate, still-to-be-qualified stage. Formula and handwriting
recognition are outside these presets; no table-cell accuracy is claimed.

### Material extraction and separate downstream processing

The three material profiles describe page preparation and extraction context.
Their existing `ocr_profile` selection is the text-recognition component, not a
command to send every extracted part through OCR. The shared `document-layout`
receipt retains regions, crop artifacts, source coordinates and proposed labels.
Text and visual processing consume that evidence independently. Changing a text
recipe does not rerun layout or a photograph's visual features.

Prepare a bounded downstream plan from an existing layout receipt:

```sh
archive-research document-parts --roots /absolute/roots.toml \
  --layout-key <retained-layout-key> --profile historical_newspaper \
  --output newspaper-parts.json
```

The output is a new flat file in the configured work root; existing selections
are never overwritten. It contains the ordinary finite extraction `tasks` plus
an accounting of **every** detected part, including unresolved and duplicate
proposals. This command performs no inference or index refresh. The plan can be
executed using the existing `extract --plan /absolute/work/newspaper-parts.json`.
An empty task list is an explicit no-eligible-work result; do not execute it.

| Part | Processing |
| --- | --- |
| Ordinary text, titles and captions | Existing positioned crop OCR; correction/search are later independent consumers |
| Photographs/illustrations | Existing visual feature adapter, only with `--visual-model /absolute/model-source.json` |
| Tables | Retained specialist input; ordinary OCR does not establish rows/cells |
| Formulas and handwriting | Retained source crops; recognition remains deferred |
| Unknown/ambiguous labels | Retained without automatic processing |

The optional model JSON is an existing fingerprinted `Source` record. No weights
are downloaded. Visual tasks bind the exact layout block and render from the
original page; they do not embed a contrast-normalized layout crop. Changing the
block, crop, source or parent receipt fails validation. Caption/article links and
reading-order information already present in extraction are retained as proposals;
missing associations are not inferred by proximity or silently accepted.

Routing uses `ordinary-text-v1`: duplicate text is accounted for and text with
at least half its area inside a detected table, figure, formula or handwriting
region is withheld from generic OCR. This conservative geometric rule is an
unqualified candidate. Inline formulas can still occur within otherwise ordinary
text. The plan retains specialist regions for downstream protection; it never
enables spelling correction automatically. Detector mistakes remain visible:
a table mislabelled as plain text still needs extraction correction/qualification.

For an ordinary-text assembly, pass the plan's `text_scope` into the existing
`document-text` settings and into `geometric_order` or `propose_order`. Supply the
selected OCR origins and an explicit order basis as before. Assembly requires
every eligible ordinary-text block exactly once and lists `unassembled_parts`;
specialist omissions must not be described as full-page text coverage. Existing
unscoped tasks, OCR presets and retained assemblies keep their prior semantics
and cache identities. Clean print's full-page baseline remains available; a
complex page requires a retained region extraction before using this routing.

Benchmark extraction separately from its consumers. The existing evaluation
manifest accepts `layout_readings` (the same `id`/`receipt` fields as text
readings), `part_regions` (`region` and `kind`) and `part_reference_complete`.
`part_score` reports one-to-one IoU region coverage and material-kind confusion;
partial annotations leave precision undefined rather than treating unannotated
regions as false positives. CER/WER and critical-token scores remain separate
text measurements. Photo retrieval, caption association and reading order still
need their own acceptance checks. Optional `scorecard_name` selects a flat JSON
output name. The same final-test freeze/operator-reference gates apply to region
evaluation. No diagnostic score automatically qualifies a profile or chooses a
fallback.

### Bordered-table recovery from retained page evidence

`document-tables` prepares one `document-layout` refinement task from a retained
layout and a whole-page positioned OCR/native-text receipt. It does not run
recognition. Execute that task with the ordinary finite extraction runner, then
use its resulting layout key with `document-parts`:

```sh
archive-research document-tables --roots /absolute/roots.toml \
  --layout-key <original-layout-key> --reading-key <positioned-text-key> \
  --output table-plan.json
archive-research extract --roots /absolute/roots.toml \
  --plan /absolute/work/table-plan.json --summary table-run.json
archive-research document-parts --roots /absolute/roots.toml \
  --layout-key <refined-layout-key> --profile historical_newspaper \
  --output table-separated-parts.json
```

Use a new output filename; plan preparation refuses overwrite. The current
refinement is `bordered-tables-v3` / `document-layout` recipe 7. Version 2 / recipe 6
remains reproducible with its original text-band behavior. Version 3 proposes
logical rows for supported ruled property/value tables: a label with values in
at least two other columns starts a row; nearby label/unit continuation lines
share its cells. Uncertain or sparse row starts retain the text-band fallback.
Each grouped cell retains line word indices, raw line breaks and source geometry;
original text-band cells and all unassigned words remain available. This groups
text without correcting spelling, units or values. It reuses the
existing OpenCV ruling detector and positioned words. Closed frames need either
repeated numeric alignment and recurring whitespace gutters, or long supported
column rules plus numeric rows. A box alone cannot establish a table. Explicit
forms and photographic regions are excluded. Unbordered/broken-frame cases and
insufficient evidence remain unresolved; no vision fallback runs automatically.

Original region records are retained unchanged. New table containers are appended;
an already detected matching table receives separate structural evidence. A
near-identical table/figure conflict stays recorded and is withheld from visual
processing, rather than being treated as a photograph. Existing non-table parts
keep their original exact dependency bindings, so their OCR and visual features
can be reused. Scoped text assembly accepts that verified lineage and excludes
table contents while listing every unassembled part. Refinement cannot silently
alter a retained crop or stack on another refinement.

Each proposed table has source-coordinate cells, exact raw OCR word indices,
header/context text, unassigned words, gutter-crossing warnings and an inert HTML
preview. All source words inside the table remain accounted for, either assigned
once or explicitly unassigned. Rows are either retained geometric text bands or
explicit logical-row proposals with original text bands retained. Neither is a
verified transcription: grouped labels, merged headers and spans still need review.
`no_source_words` never asserts that a source cell is blank. Column boundaries
are explicitly in prepared-page normalized coordinates; cells are transformed
back into source-page coordinates, including rotated PDFs. Numeric OCR errors
are preserved, not repaired by the table parser.

The newspaper and CCD-report examples are development regressions. Their region
and structural checks do not establish general table recognition accuracy, and
no output is adopted or automatically selected in the workbench/index.

### Mixed albums and selected handwritten lines

`brigadebuch` separates material extraction from recognition. A `document-layout`
task can bind `settings.region_annotation` to a hashed local Source containing
`format: archive-material-regions-1`, `metadata_adopted: false`, its exact `page`,
and ordered `blocks`. Each block has `index`, `label` and normalized source
`region`; handwritten blocks may have explicit `lines` with unique `id` and
contained `region`. Optional `caption_links` bind `text_block` to `image_block`
with `status: proposed`. These are prepared annotations, not automatic detection
or independently verified segmentation. Unselected page content stays unprocessed.

```sh
archive-research document-parts --roots /absolute/roots.toml \
  --layout-key SELECTED_LAYOUT_KEY --profile brigadebuch \
  --handwriting /absolute/handwriting-selection.json \
  --output mixed-album-parts.json
```

The selection JSON contains `backend` and, for learned methods, `model_manifest`
as a fingerprinted Source. Supported backends are `dehtr-v2` (ONNX CPU),
`trocr-small-de` (local Transformers/safetensors) and `tesseract-control` (deu,
PSM 7). The manifest pins repository, revision and each local model file's name,
relative path, SHA-256 and byte size. Download model data explicitly before
processing; the adapter checks all files and denies Python network connections.
It neither downloads models nor executes model-supplied code. Public TrOCR model
files are briefly staged in native `/tmp`; source images stay in the work root.

Run the resulting plan through the existing `extract` operation in an environment
containing the selected backend. Ordinary text goes to its own OCR component,
photographs remain source-bound crops (or explicitly selected visual tasks), and
only annotated handwritten lines reach handwriting recognition. Reuse the same
processor for handwriting in other materials by supplying its bound layout part;
it has no dependence on Brigadebuch catalogue descriptions.

Each reading retains the exact line crop, literal text, source coordinates,
backend, timings and truncation warnings. No word boxes, calibrated confidence,
automatic correction or automatic fallback decision are invented. Results are
partial: even perfect selected lines do not establish whole-page coverage.
Independent references, names/dates, line omissions and fallback benefit must be
measured before widening scope. Existing evaluation accepts one entry per selected
handwritten part, with its corresponding reference lines and alternative receipts.
Compare whole splits using summed errors/reference length, not mean page rates.

The workbench can display an alternative raw reading beside each selected line;
both are escaped text and neither changes the proposed finding or saved review.

### Retained CTC scores and derived handwriting decoding

The `mannheim-onnx` backend binds UB Mannheim's `german_handwriting` model
(Zenodo 7933463) after conversion on a compatible machine. The manifest requires
`model.onnx`, `config.json` and `export-proof.json`, all hashed. It uses the
existing ONNX Runtime with NumPy/SciPy/Pillow center normalization, no local
Kraken or PyTorch imports. Selected lines must normalize to at most 7,000 pixels;
wider lines fail explicitly. Model outputs remain unreviewed proposals.

`tools/export_mannheim_onnx.py` exports the pinned public model and synthetic
fixtures on a compatible CPU; `tools/verify_mannheim_onnx.py` checks exact input
pixels, probability tolerance and identical greedy text in the destination
runtime. Synthetic equivalence is not handwriting accuracy qualification. Keep
the public model's CC-BY-SA-4.0 attribution and the bundled Apache-2.0 license for
the adapted Kraken normalization code. The native `mannheim-kraken` backend
retains its conservative AVX/SSE4.1 gate; do not run the incomplete local Kraken
environment. DWDS corpus support remains pending permitted data access.

An explicit `line_preparation: "baseline-straight-v1"` selects recipe 4 with a
matching baseline manifest/proof. It assumes a horizontal two-point baseline
inside each existing rectangular line region, then uses Kraken's straight
baseline path: no rotation, grayscale, LANCZOS scaling to height 120, padding and
inversion, with a separate 4,096-pixel normalized width limit. Uniform blank tensors cause explicit abstention before recognition;
no invented blank-line text or synthetic model scores are emitted. This does
not detect baselines or rectify curved/sloping writing. Prior center-normalized
recipes keep their identities. Export fixtures with `--preparation baseline-straight`
and verify them locally with `--prefix mannheim-baseline`; nonblank fixtures also
match Kraken's full `rpred` baseline pipeline.

For `dehtr-v2` or `mannheim-onnx`, explicitly add `retain_scores: true` to the selection.
This creates a separate recipe with per-line, hashed NPZ score artifacts; existing
greedy recipes and readings remain unchanged. The score bundles contain numeric
logits and the exact alphabet, never pickled objects. Re-decoding must reproduce
the parent greedy text before a derived result can be published.

Build a downstream task with
`extractors.handwriting_decode.task_for(roots, work, key, mode, **settings)` and
place it in an ordinary extraction plan. The key selects the retained-score
handwriting receipt. Modes are `beam`, `lexicon` and `confusions`; the latter two
require `dictionary` and `custom_lexicon` as hashed Sources. The dictionary is
UTF-8 `word count` lines; the custom JSON contains `terms` and `names` arrays.
It must be frozen independently of the scored reference answers.

The `handwriting-decode` adapter uses bounded CTC prefix beam search, retaining
separate blank/nonblank masses, and exact CTC forward probabilities for complete
candidates. Defaults: beam 32, 12 classes per frame within 12 log units, eight
retained candidates, lexical weight 0.8 and at most 64 candidates for confusion
rescoring. The soft frequency-based word preference is not a trained contextual
language model. Unknown names remain legal; known words can still win incorrectly.

Confusions cover n/u, m/nn/un, e/i, r/v, cl/d, umlauts, ß/B/fs and bounded word
splits/joins. A candidate changes one token (up to two substitutions) or one word
boundary; original candidates remain available. Ornate capitals can receive
alternatives from model scores, but this adds no capital-shape recognizer or
Kurrent model. Candidate scores/margins are not calibrated confidence. All results
remain partial, separately bound proposals, with parent text and candidate lists.

Compare greedy and all three modes on identical frozen lines: summed CER/WER,
line improvements/regressions, restored/damaged reference characters and words,
spacing, labeled names/dates, candidate coverage and decoding time. Previously
inspected validation pages are regression evidence, not an untouched acceptance
set. Do not automatically replace raw readings or widen processing from this
small comparison alone.
