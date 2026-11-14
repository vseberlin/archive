# Archive tools

Source lives in `/home/vladimir/archive/Archive-tools` on `archive/scripts`.
The workstation path `/home/vladimir/Archive-tools` is a compatibility link to
this directory. Runtime data and the current private checkpoint remain under
`/mnt/volume/iss-archive-work`; start with that directory's `HANDOFF.md` when
resuming this job.

Six independent tools for archive reconciliation and maintenance, starting with
the workbench. `workbench.py` and the folder-to-collection preparation command
in `search.py` are implemented; four other entry points remain planned. The
workbench uses prepared evidence and the shared meanings below.

## Run the workbench

Requires Python 3.11+ on Linux and Pillow for image previews. PDF previews and
embedded text use Poppler (`pdftoppm`, `pdftotext`). These are already installed
on this workstation. No package installation, database, or background service
is required.

Start a synthetic demonstration in a **new or empty** work directory:

```sh
python3 -B /home/vladimir/Archive-tools/workbench.py \
  --demo --work-root /mnt/volume/iss-archive-work/archive-tools-demo \
  --actor Vladimir
```

Open <http://127.0.0.1:8782/>. Stop with Ctrl-C. Subsequent starts use the same
command **without `--demo`**, preserving saved decisions. `--port` selects a
different local port. The demonstration contains generated drawings, a resized
crop, a deliberately different image, and two-page TIFF/PDF examples; its stated
pHash distance is an illustration, not a computed result.

For a prepared collection, point `--work-root` at its existing review directory:

```sh
python3 -B /home/vladimir/Archive-tools/workbench.py \
  --work-root /absolute/path/to/review-work-area --actor Vladimir
```

Use A/B selectors, linked zoom and pan, overlay, or difference view to inspect
the candidates. When there is only one candidate, B stays empty; the source is
never automatically displayed as its own comparison. Each pane shows its exact
file path. Explicitly selecting the same file on both sides for page/view
comparison shows a warning and cannot be recorded as a different-photo pair.
Select one eligible complete Master. Explicitly classify each
other candidate relative to it, or leave it unresolved. You can also reject the
displayed A/B pair before choosing a Master. Confirmed copy relationships carry
forward when the selected Master changes; previous selections remain provenance.
Save decisions before moving to another object.

## Generalized reconciliation contract

`search.py` compares one or more inspected folders with one or more ready archive
units. `workbench.py` reviews its proposals. Source and archive files are opened
read-only. Writes are confined to the selected work area. Neither command
accessions, replaces, embeds metadata, moves, or deletes archive/source files.

Both Czkawka passes must carry deletion method `NONE` **and** `-Q` (dry-run).
An execution-time allowlist rejects missing guards, destructive methods,
trash/move flags, duplicate overrides and unknown options before the subprocess
can start. Commands use argument arrays without a shell. ExifTool reads metadata;
the existing visual-index SQLite connection uses `mode=ro`.

Preparation requires a fresh work area, separated from every supplied input,
archive unit and prior review. Existing log hard links are rejected. Atomic
saves replace generated work-area outputs; temporary `.comparison-*` previews
are removed after comparison. Those are the only intended file replacement/
cleanup operations, and never operate on source/archive files.

| Evidence | Result |
| --- | --- |
| Source already assigned to an md object | Exclude from new-object discovery without inventing a same-photo or redundant-copy relationship. |
| Genuine TIFF for a catalogued md substitute JPEG Master | Keep a bounded TIFF recovery review; any replacement remains a proposal. |
| Confirmed same photograph, adequately covered by an archive Master | Mark the inspected copy/derivative redundant, with a retained-Master link. Skip image review. |
| Confirmed archive photograph, additional/conflicting/unavailable metadata | Retain metadata as source evidence; keep the archived object resolved. |
| Confirmed same photograph, different source resolution, orientation, polarity or colour profile | Retain the existing archive Master and record the source as redundant. |
| No usable genuine source TIFF, usable JPEG available | Allow the JPEG as the fallback Master candidate. |
| Perceptual or metadata similarity only | Present an unconfirmed comparison; no automatic redundancy. |
| No corresponding object established | Propose a new object when the configured visual search completed; otherwise show a coverage review. Never claim exhaustive absence. |
| Conflicting or stale evidence | Keep unresolved; do not hide the source. |

The goal is genuinely new objects outside the reconciled md archive. New preparations
record `archive_masters_fixed=true` and `review_scope=new_objects_and_tiff_for_jpeg`.
Existing md objects are outside the active review, including explicit prior metadata
assignments; such a binding does not establish identical images or authorize redundancy.
The sole replacement exception is a genuine TIFF for a documented substitute JPEG
Master of an md object. Recognized catalogue evidence is an explicit surrogate,
`curated_5000px_jpeg`, or `curator_confirmed_unreadable_master_replacement`. Arbitrary
JPEG originals, manufactured TIFFs, larger TIFFs, and colour/metadata differences do
not qualify. A reviewed TIFF selection remains a proposal; the archive is unchanged.
The default queue shows cases needing review, while resolved evidence remains
accessible through All review cases. Metadata differences on confirmed archive
copies do not create another mandatory review or falsely set metadata_reviewed.

Failed historical screening results and SIFT `not_supported` pairs remain evidence
but do not enter the active shortlist. Confirmed identity takes precedence over a
failed similarity test. A loose md suggestion never gives the source an md identity:
supported suggestions can be used to determine whether that source is new. Pane A
shows source Master candidates; pane B shows active archive references. The same
archive file cannot occupy both panes in discovery mode. Excluded suggestions remain
available in the existing evidence details, and known md cases are read-only.

Among source representations for a genuinely new photograph, prefer a genuine TIFF;
otherwise prefer the JPEG with the highest available pixel count. A catalogue-
identified TIFF manufactured from JPEG does not outrank a genuine JPEG source.
This is a recommendation: dimensions do not prove native resolution, sharpness,
uncropped content, or layer completeness. An explicit confirmed `covered_by`
relationship can establish adequacy independently of this preference.

Group inspected files only through exact whole-file hashes or confirmed same-
photo relationships. Native raster verification can independently link
an inspected representation to a retained archive Master. Filenames, dimensions, metadata matches, pHash/dHash/aHash,
and Czkawka visual-similarity results never establish identity by themselves. Crops and resized
files become redundant when a confirmed adequate Master covers them.

The retained-Master pixel check uses the existing libvips decoder at native bit
depth. It compares every sample, allowing rotation, reflection and negative/
positive inversion while ignoring ICC-profile differences. Compression and
metadata-only Photoshop resources do not prevent comparison. Additional pages,
layers or unresolved raster differences remain visible. SIFT is discovery and
ranking evidence; a supported SIFT match alone never resolves a case. Different
resolutions do not justify replacing an archive Master; when resampling prevents
exact proof, identity still needs independent confirmation.

Archive identities are qualified by canonical unit path and original record key;
identical record keys in different units remain distinct. Selecting a source
confirmed to an existing archive object retains that object's archive Master,
except for an explicitly reviewed TIFF proposal for a documented substitute JPEG.
A genuinely new object needs an explicit destination unit.
Similarity alone never chooses its destination. No new accession identifier is
minted during preparation.

Confirmed historical metadata assignments retain their original md identity even
when that record has no current selected Master. A former comparison or alias
target is not substituted for that identity. Such sources are outside new-object
discovery without asserting that they depict the current canonical Master.
When an unreadable TIFF leaves a usable JPEG Master fallback, that JPEG's own
stamped archive comparisons are retained. Attaching it to the TIFF case must not
erase a supported archive lead or turn incomplete evidence into a new object.

## Prepare folders against archive units

Repeat `--source` and `--archive` as needed. Overlapping inspected folders are
deduplicated by canonical path. Default source and Master formats include both
TIFF and JPEG. `--against` remains an alias for `--archive`.

This example compares Avantmedia with both TFA and Kurt Schwarz and reuses both
previous preparations. Use a **new** work directory:

```sh
python3 -B /home/vladimir/Archive-tools/search.py \
  --source /media/vladimir/Elements/restore/archive/Avantmedia \
  --archive /mnt/volume/iss-archive/units/tfa \
  --archive /mnt/volume/iss-archive/units/kurt-schwarz \
  --prior-review /mnt/volume/iss-archive-work/avantmedia-305-review \
  --prior-review /mnt/volume/iss-archive-work/avantmedia-tfa-review \
  --index /home/vladimir/archive-visual-index/visual-index.sqlite \
  --czkawka /mnt/volume/iss-archive-work/kurt-schwarz/work/tools/czkawka_cli-12.0.1 \
  --work-root /mnt/volume/iss-archive-work/avantmedia-multiunit-review \
  --threads 2
```

### Run preparation independently of the chat or terminal

Add `--background` to the preparation command above. It launches the same
`search.py` in a detached process and immediately prints its PID and output paths.
The run needs no model calls, chat polling, open terminal, or installed service.
It continues until preparation finishes or fails; it does not survive a reboot.
All ordinary source, archive, prior-review and Czkawka options still apply.
Choose those settings before launching.

Background launch requires a **new, nonexistent work directory** and refuses to
reuse an existing one. Foreground preparation requires a new or empty directory.
Both modes use the same input/output overlap checks before creating any outputs.
Reuse earlier preparations only through `--prior-review` in a new work root.

Inspect these files when you return:

```sh
cat /mnt/volume/iss-archive-work/avantmedia-multiunit-review/preparation-run.json
tail -n 20 /mnt/volume/iss-archive-work/avantmedia-multiunit-review/preparation.log
```

`preparation-run.json` records the command, PID, start/finish times, state and
exit code. `completed` / exit 0 means the preparation and CSV projection finished;
the record includes counts and a link to the existing `preparation-summary.json`.
`failed` / exit 2 includes the error, with details in `preparation.log`.
The launcher returning 0 only means the worker was launched. It is not a
completion result, a coverage guarantee, or a human review decision.

To stop deliberately, send `kill -TERM PID` using the recorded worker PID. The
worker stops an active Czkawka subprocess and records `interrupted`. A power loss
or SIGKILL cannot write a final record; if it still says `running`, check the
recorded process before treating it as active. Do not launch again into that
folder; use a new work root and reuse prior evidence as documented below.
The workbench is opened separately after successful preparation.

### Review the prepared output

For another job, change the folders/units and omit unrelated prior-review inputs.
Optional `--destination /path/to/supplied/unit` explicitly sets the destination
for new-object proposals; otherwise choose it in the workbench. Units are search
references even when their completion receipts lock their original workbenches.
Reviewing proposals never unlocks or modifies those units.

Preparation requires Python 3.11+, Pillow and ExifTool. The visual index and
Czkawka are optional; missing search coverage remains visible. Elements must be
mounted exactly as `/dev/sdf1 /media/vladimir/Elements ntfs3 ro`. Commands check
this condition and never mount or remount media.

Open the resulting review:

```sh
python3 -B /home/vladimir/Archive-tools/workbench.py \
  --work-root /mnt/volume/iss-archive-work/avantmedia-multiunit-review \
  --actor Vladimir --port 8784
```

Use filters for similarities, proposed new objects, metadata questions and
coverage problems; legacy preparations may also contain upgrade proposals.
Confirm **A and B are the same photograph** to establish an uncertain
source/reference pair. The fixed-archive policy selects the retained Master and
classifies covered inspected copies as redundant. **Different photos** rejects a
pair without assigning either photograph's metadata to the other. A separated
source in a grouped case stays pending until reconciled as its own object.

### Reuse before new comparison

1. Import current, stamped whole-file hashes, object/derivative provenance,
   explicit relationships, prior search leads, and saved human decisions.
2. Compare compatible cached pHash, dHash and aHash values and descriptive fields.
3. By default, run Czkawka Gradient and Blockhash for residual sources without
   existing leads or saved Master proposals. Configurable visual passes and an
   optional BLAKE3 exact-copy pass are described below. Temporary comparison
   images stay in the work area; deletion is explicitly disabled. Distances
   remain separate.
4. Run the default full-resolution coverage check on discovered candidate
   Masters, then optional SIFT geometry verification on the remaining unresolved
   candidates. Confirmed copies of retained archive Masters skip SIFT and are
   recorded in `skipped-confirmed.jsonl`. Source metadata and earlier proposals
   remain evidence; explicit identity conflicts remain visible.

### Full-resolution coverage of existing objects

Preparation aims to find objects not already covered by the supplied archive
units. Pixel coverage runs by default, independently of `--sift`, using the
installed libvips decoder, Pillow and NumPy. It checks the actual
source and selected Master, not their previews. Candidate discovery still comes
from prior evidence, hashes, Czkawka and metadata; this is not an all-pairs search.

The retained-archive comparison profile accepts:

- identical native samples after quarter-turn rotations or reflection, with
  matching dimensions, channels and bit depth;
- negative/positive inversion, with at most one code-level difference at every
  unsigned 16-bit sample. Direct comparisons and 8-bit inversion require exact
  equality. Every sample must pass; no proxy or sampling threshold is used.

Supported rasters are grayscale/RGB TIFFs with unsigned 8- or 16-bit samples and
ordinary grayscale/RGB JPEGs. Compression, byte order, ICC profiles and metadata
resources may differ. Explicitly flagged reduced thumbnails up to 512 pixels
are ignored. Additional ordinary pages, masks, alpha, layers, resizing, crops
and unproved pixel differences remain unresolved. Decoder or NumPy unavailability
leaves coverage unresolved. No 16-bit TIFF is converted to 8-bit for this proof.

Successful coverage also verifies the retained Master's recorded SHA-256, or
observes its digest if none was recorded. A mismatch aborts preparation. Once
identity is confirmed, the existing archive Master and metadata stay fixed;
source resolution and metadata differences do not reopen the archived object.
Prior choices remain available as evidence. Human rejected matches are not
reconsidered automatically. Older preparations without `archive_masters_fixed`
keep their legacy policy until explicitly reconciled with the new rule.

`pixel-coverage.jsonl` records each outcome, native sample counts, transformation,
error bounds, input stamps, comparison version and verified Master checksum.
`--prior-review` reuses unchanged comparisons, including cases previously skipped
from the workbench; changed files or comparison versions require a fresh check.
`matches.jsonl`, source dispositions and the preparation summary include the
coverage evidence. An unsuccessful comparison does not prove a different object:
crops, edited versions and uncertain matches still need review. New-object labels
remain proposals within the supplied search scope, never proof of global absence.

Use a **new work root** for a rerun and import the completed preparation with
`--prior-review`. Existing workbench decisions stay intact. All source and archive
files are opened read-only; coverage grants no accession, movement or deletion
authority.

### Configure Czkawka per job

Add these flags to the preparation command to run the requested combination:

```sh
  --czkawka-exact-hash BLAKE3 \
  --czkawka-pass Gradient/16/Lanczos3/12 \
  --czkawka-pass DoubleGradient/32/Lanczos3/12 \
  --czkawka-scope unconfirmed
```

Alternatively use `--czkawka-profile /home/vladimir/Archive-tools/czkawka-profile.example.json`.
Copy that source-only example into your job's work area to keep project-specific
settings. Repeated `--czkawka-pass` flags replace the profile's visual-pass list;
individual CLI options override the corresponding JSON settings. The example
is a starting configuration, not a validated optimum for every collection.

| Setting | Supported values / purpose |
| --- | --- |
| Exact comparison | `off` (default) or `BLAKE3`; compares original file bytes before visual search. |
| Visual algorithm | `Mean`, `Gradient`, `Blockhash`, `VertGradient`, `DoubleGradient`, `Median`. |
| Hash size | `8`, `16`, `32`, `64`; Czkawka's hash-dimension setting. |
| Resize filter | `Nearest`, `Triangle`, `CatmullRom`, `Gaussian`, `Lanczos3`. |
| Maximum difference | `0`–`40`, independently for each pass; zero still means equal perceptual hashes, not proven image identity. |
| Geometry | `off`, `mirror-flip`, `mirror-flip-rotate90`. Optional fifth slash-separated pass field. |
| Scope | `residual` preserves the default reuse-first search; `unconfirmed` also reruns visual comparisons for sources with existing tentative leads. Confirmed sources and saved Master proposals are reused. |
| Proxy edge | `--czkawka-proxy-edge 512`, range 64–4096, default 256. Controls the temporary comparison image, independently of hash size. |
| Preview mode | `--czkawka-preview-mode catalogued` (default) permits catalogue derivatives/embedded thumbnails; `primary` compares primary images/composites and can cost more I/O. Neither certifies TIFF layers. |

Pass shorthand is `ALGORITHM/SIZE/FILTER[/MAX_DIFFERENCE[/GEOMETRY]]`.
Omitted distance and geometry default to 12 and `mirror-flip-rotate90`.
An empty JSON `passes` list disables visual Czkawka passes while allowing BLAKE3.
Without configuration, the existing Gradient/16 and Blockhash/16 visual defaults
are preserved, and no BLAKE3 scan is added.

BLAKE3 is a separate cryptographic exact-copy test. It cannot detect a resized
JPEG, a crop, or a metadata-rewritten TIFF as byte-identical to its original.
The optional pass reads explicit original paths in compatible byte-size buckets,
including selected archive Masters and catalogue-linked derivatives. It never
hashes comparison previews as exact source evidence. Equal source files can be
grouped; equal archive copies can establish redundancy. Existing archive SHA-256
values remain authoritative and are verified for matched archive evidence.
BLAKE3 values are recorded separately and never written into SHA-256 fields.
No extra hash dependency is installed: the configured Czkawka executable does
this read-only work, with deletion disabled.

Changing settings affects a new preparation run. Use a new `--work-root` and
`--prior-review` for an earlier review; do not overwrite a saved queue. Use
`unconfirmed` when retuning to ensure that existing tentative matches do not
prevent the newly chosen visual passes from running. Prior incompatible Czkawka
matches remain historical evidence, while saved human decisions remain intact.

Every run saves `czkawka-effective-profile.json`; each match records its actual
pass settings and discovery-proxy profile. Cache keys include input stamps,
the executable and the settings. A changed profile cannot reuse results as if
they were computed with the new parameters. An unchanged, identically scoped
BLAKE3 result can be reused from `--prior-review` without repeating that pass.
Different algorithms/hash sizes need their own calibrated thresholds; their
distances should not be added together or compared as confidence percentages.
Larger hashes may reduce some false positives while also missing derivatives.
Check known same-photo and known different-photo pairs for each project.

Supported choices were verified with the installed Czkawka 12.0.1 CLI and its
[official command definitions](https://github.com/qarmin/czkawka/blob/12.0.1/czkawka_cli/src/commands.rs).

`--prior-review` accepts an earlier work directory or workbench decisions JSONL.
Unsaved preparation recommendations are not imported as human decisions. Saved
metadata review is reused only while source and target metadata are unchanged.

The explicit adapter for `avantmedia-305-review` preserves its TIFF-only Kurt
Schwarz search scope, prior leads, researched metadata, and saved proposals.
Legacy `assign_source` means a proposed metadata/Master reassignment; it does
**not** assert that the displaced image is the same photograph. Legacy redundant
proposals without a retained Master stay unresolved. Old search matches remain
leads, even when an earlier report called them existing-image matches.

`--source-hashes file.csv` accepts columns `source_path,size_bytes,mtime_ns,sha256`
(`path` is also accepted). Reuse requires the exact current stamp. `--hash-missing`
opts into missing whole-file SHA-256 work, limited to compatible byte-size
buckets and possible source duplicates. This can still involve substantial I/O;
it is disabled by default. Archive digests used for automatic exact coverage are
verified against the current bytes. Newly observed hashes stay in the work area.
No archive catalogue is updated.

A matching catalogued access derivative can establish coverage by its recorded
retained Master. An original-source digest can establish coverage after metadata
embedding only with explicit unchanged-pixels/structure provenance. Plain name,
path, inventory, or source-disposition membership is insufficient.

### Verify candidate matches with SIFT

Add `--sift` to preparation to compare existing candidate pairs using local
features and geometric alignment. This runs after cached hashes, metadata leads,
any configured Czkawka comparisons and full-resolution pixel coverage. It does not search pairs
that those methods never suggested. Use `--czkawka-scope unconfirmed` if current
tentative leads should also receive the configured Czkawka discovery passes.

Run with the installed SIFT environment instead of system Python:

```sh
/mnt/volume/iss-archive-work/archive-tools-venv/bin/python -B \
  /home/vladimir/Archive-tools/search.py --help
```

Use that executable with the usual source/archive/work-root options and add
`--sift --background` for independent execution. The background worker preserves
the selected Python interpreter. SIFT is opt-in and requires OpenCV. The default
full-resolution coverage check also runs without SIFT and does not use OpenCV.

The workstation environment was verified with the versions in
`requirements-sift.txt`. Its initially installed NumPy 2.5.4 crashed with SIGILL
on the AMD Phenom II CPU; NumPy 2.2.6, OpenCV 4.12.0.88 and Pillow 11.1.0 passed
actual feature extraction and matching. To reproduce this environment, install
the requirements into the existing virtual environment:

```sh
/mnt/volume/iss-archive-work/archive-tools-venv/bin/python -m pip install \
  --only-binary=:all: -r /home/vladimir/Archive-tools/requirements-sift.txt
```

SIFT reads primary images/composites at up to 1600 pixels on the longest edge,
with at most 2000 features per image. It tests direct, inverted, mirrored and
mirrored/inverted source views, stopping when a view meets the geometry criteria.
It uses mutual nearest-neighbour ratio filtering, deduplicated point locations,
RANSAC homography, spatial coverage and symmetric alignment error. It does not
certify TIFF layers/pages or transfer metadata between images.

| Option | Default |
| --- | --- |
| `--sift-proxy-edge` | 1600 |
| `--sift-ratio` | 0.7 |
| `--sift-min-inliers` | 12 distinct geometrically consistent points |
| `--sift-min-inlier-ratio` | 0.5 |

The geometry also requires matched points to span at least 15% of one image and
1% of the other, with median symmetric error at most 4 preview pixels. These
are starting thresholds tested on synthetic examples, not calibrated accuracy
claims for this archive. Repeated structures or separate photographs of the
same scene may still align; human review remains necessary.

`supported` pairs rank ahead of inconclusive and `not_supported` pairs before
the existing display shortlist is chosen. Failed geometry, insufficient detail
and unreadable images stay in the saved evidence; they never become human
`different_photo` decisions or proof of absence. Supported geometry never selects
a Master, confirms identity, assigns metadata or authorizes redundancy. Existing
confirmed pairs and human rejections are preserved and excluded from this pass.
Sources confirmed as copies of a retained archive Master skip SIFT, including
their other tentative matches. Their leads and metadata remain available as
evidence. Sources with unresolved identity conflicts or saved Master proposals
still receive comparisons of their unconfirmed pairs.
The workbench displays the SIFT outcome and inlier count with the pair evidence.

Progress starts with the total planned pair count, source count, reusable pair
count and covered sources skipped. Updates show completed/total pairs and an
estimated remaining SIFT duration, measured from newly computed pairs so fast
cache reuse does not distort it. The estimate can vary with image complexity;
it excludes other preparation stages. The summary also records these counts.

`sift-cache/*.npz` stores bounded feature arrays with JSON provenance; no pickle
is loaded. `sift-pairs.jsonl` records pair results, transforms and settings.
`preparation-summary.json` includes SIFT counts and cache reuse. Existing
`matches.jsonl` keeps every candidate's evidence, including candidates outside
the display shortlist. Features and pair results can be reused through
`--prior-review`; source stamps, rendering/extractor versions and settings must
match. Retuning thresholds can reuse features but recomputes pair results.
Prior SIFT output is retained as historical context rather than silently treated
as current confirmation. Use a new work root for a new preparation.

Algorithm reference: [OpenCV SIFT matching and homography](https://docs.opencv.org/4.12.0/d1/de0/tutorial_py_feature_homography.html).

### Unit adapters and explicit relationships

For an already completed Avantmedia TIFF/JPEG preparation, the bounded
`avantmedia_derivatives.py` adapter rebuilds a fresh review under the explicitly
approved delivery-layout assumption. It associates JPEGs in `Nutzungsfiles`,
`Nutzungsderivate` and `Baecker_alleJPGs` with a unique TIFF identifier in the same
delivery; `Auftrag 1614930` uses magazine and frame numbers. Ambiguous suffixes
remain in review. The known SimpleViewer `sandstone.jpg` asset is excluded from
the collection review. These links record the user's assumption, not pixel proof.

```sh
python3 -B /home/vladimir/Archive-tools/avantmedia_derivatives.py \
  --prior-review /mnt/volume/iss-archive-work/avantmedia-multiunit-review-tightened-20261103 \
  --source /media/vladimir/Elements/restore/archive/Avantmedia \
  --work-root /mnt/volume/iss-archive-work/avantmedia-master-review-20261103
```

This reads existing evidence and current file stamps without image comparisons.
Derivatives cannot be selected as Masters. Redundancy requires either a covered
archive Master or a selected source-Master proposal; otherwise the JPEG remains
attached to its unresolved TIFF case. Extra/conflicting/unavailable JPEG metadata
keeps a metadata question visible, including for already-covered images. Existing
TIFF selections and notes carry forward as proposals with their prior provenance;
saved JPEG decisions require explicit reconciliation. The original review and
its caches remain intact. All source files remain read-only and deletion stays
unauthorized. Validate the new work root before switching the workbench.

Synthetic checks: `python3 -B -m unittest -q test_avantmedia_derivatives`.

Supported unit inputs:

- Common `CATALOG.csv` accession layout, including TFA and Kurt Schwarz, with
  selected roles, representation provenance and catalogue-linked derivatives.
- `selected_master_path` catalogues, such as Objektsammlung photographs.
- An `archive-tools-unit-1` JSON manifest prepared outside the protected unit for
  other layouts. Metadata is a dictionary of fields containing value/provenance
  entries; an empty dictionary means no supplied descriptive metadata.

```json
{
  "format": "archive-tools-unit-1",
  "unit_root": "/absolute/archive/unit",
  "objects": [{
    "record_key": "existing-record-key",
    "title": "Existing catalogue title",
    "metadata": {"title": [{"value": "Existing title", "source": "catalogue"}]},
    "master": {"path": "masters/photo.tif", "sha256": "<64 hex digits>"},
    "derivatives": [{"path": "export/photo.jpg", "sha256": "<64 hex digits>"}]
  }]
}
```

Hashes are optional; supplied hashes must be valid. Paths must remain inside the
unit. Optional Master fields include `width`, `height`, `size_bytes`,
`is_surrogate`, `original_source_path`, `source_sha256`, and `basis`.

The photo adapter requires one selected photo file per object. It rejects
multiple selected image members rather than flattening albums or page sets.
Paragon currently requires that further representation adapter. Non-photo
Masters are reported as excluded. TIFF previews/perceptual comparisons inspect
the primary image/composite; they do not certify Photoshop layers or ordered
page completeness. The generic workbench can display explicitly prepared
multi-member/page representations, but this search adapter does not generate
those contracts.

`--relations file.jsonl` imports explicit, confirmed, stamped relationships:

```json
{"format":"archive-tools-relation-1","confirmed":true,"relation":"same_photo","basis":"Human review with evidence reference","source":{"path":"/incoming/photo.tif","size_bytes":123,"mtime_ns":456},"other":{"path":"/archive/unit/masters/photo.jpg","size_bytes":789,"mtime_ns":101112}}
```

Use actual observed stamps. Relations are `same_photo`, `different_photo`, or
`covered_by` (the other member adequately covers the source). Source-to-source
same-photo relations group candidates; source-to-archive relations preserve the
existing object identity. These files describe established evidence, not a way
to promote perceptual similarity automatically.

### Metadata and outputs

Preparation extracts descriptive EXIF/IPTC/XMP through read-only ExifTool, reads
unit catalogue fields, and retains prior review/research evidence. Original
values, field names, and provenance remain visible. No creator/event roles are
inferred. Arbitrary sidecars and additional metadata databases require explicit
adapters; they are not automatically discovered or assigned to photographs.
Failed extraction is unavailable evidence, not proof of absent metadata.

Image identity and metadata differences are independent. A redundant JPEG can
carry an additional caption, date or rights statement; preserve it as source
evidence. For an existing reconciled object, the archive metadata stays fixed
and this does not create a mandatory review. New-object metadata questions remain
for review. Field selection and embedding belong to later authorized accession.

Preparation writes:

- `workbench-input.json`: review cases, recommendations, evidence and destinations.
- `source-dispositions.jsonl` / `.csv`: **preparation snapshot** covering every
  inspected TIFF/JPEG, including sources skipped from the workbench.
- `skipped-confirmed.jsonl`: sources covered by retained archive Masters, with
  source metadata and prior proposals preserved as evidence.
- `matches.jsonl`: retained comparison evidence, including candidates beyond the
  display shortlist; prior negative pairs are suppressed from active matching.
- `preparation-summary.json`: file/case counts, unit scope, exclusions, coverage,
  prior-evidence reuse and input stamps. Case counts are not source-file counts.
- Metadata/hash/Czkawka caches and logs inside the same work directory.

After human saves, `decisions.jsonl` and its `dispositions.csv` projection are the
current review state. The preparation snapshot is deliberately unchanged; combine
its skipped rows with reviewed cases for the complete current source picture.
Every record keeps `deletion_authorized=false`. Redundancy relative to a proposed
source Master is explicitly `redundant_to_proposed_master`, not accessioned
coverage. Use a new work directory with `--prior-review` to prepare again.
Existing prepared manifests and decisions are never overwritten by preparation.

## Input and output

The work directory contains `workbench-input.json` with this structure:

```json
{
  "format": "archive-tools-workbench-1",
  "title": "Collection under review",
  "read_only": false,
  "read_roots": ["/absolute/source/root"],
  "completion_receipts": [],
  "objects": [{
    "id": "existing-object-id",
    "title": "Display label, not an inferred catalogue title",
    "metadata": {},
    "candidates": [{
      "id": "existing-candidate-id",
      "label": "Source TIFF",
      "eligible": true,
      "complete": true,
      "members": [{
        "path": "/absolute/source/root/photo.tiff",
        "size_bytes": 123456,
        "mtime_ns": 1790000000000000000,
        "pages": 1
      }]
    }],
    "matches": []
  }]
}
```

- Replace sample paths, sizes and nanosecond modification times with observed
  values. Preserve existing object and candidate IDs. Candidate IDs are unique
  within their object. Member order is significant. One selected candidate may
  represent a complete PDF/TIFF or an ordered group of files.
- A collection adapter must establish `eligible` and `complete`; the workbench
  does not infer completeness or image quality. Declare source roots explicitly.
  Source roots must not contain the writable work directory. Sources must be
  regular files with canonical paths, without symlinks.
- A candidate with `reference_object_id` represents an existing selected archive
  Master. Several source review cases may point to the same retained Master;
  they cannot assign different Masters to that archive identity. Unchosen archive
  references remain references, not unresolved source files or redundant copies.
- `metadata: {}` means descriptive metadata unknown. Retain known values and
  their provenance in that dictionary; additional source fields are preserved
  in the saved evidence. Candidate-specific metadata may be supplied separately
  in each candidate's `metadata`. Selecting an image does not assign its
  metadata to an object. This first workbench displays metadata evidence and
  records review notes; it is not a metadata editor.
- Optional member `sha256` is an existing lowercase whole-file digest. Optional
  `matches` entries have `a` and `b` candidate IDs and may include `algorithm`,
  `distance`, `profile`, and source evidence. They never authorize a decision.
- List a unit's actual absolute completion-receipt paths in
  `completion_receipts`, under declared read roots. An existing receipt freezes
  editing. Conventional `accession-receipt.json`, `accession-completion.json`,
  and `publication.json` in the work directory also freeze editing, even if
  malformed. Use `read_only: true` for already completed units.

The only persistent outputs are `decisions.jsonl` (authority),
`dispositions.csv` (rebuildable summary), and `.workbench.lock` (local write
lock). Saved records include the original prepared object/candidate evidence,
selected Master, previous Master IDs, redundant candidate IDs, rejected pairs,
reviewer, timestamp, and note. All decisions have `deletion_authorized: false`.
The CSV includes explicit retained-Master links and rejected-pair IDs.

Validate a prepared work area without writing:

```sh
python3 -B /home/vladimir/Archive-tools/workbench.py --work-root /path/to/review --check
```

Exit status is 0 for valid, available inputs, 1 for unavailable/stale source
files, and 2 for invalid input or execution errors. Missing/outdated CSV is
reported separately and can be repaired without reading its old schema:

```sh
python3 -B /home/vladimir/Archive-tools/workbench.py --work-root /path/to/review --rebuild-csv
```

Edits use a revision check and local file lock. File size and modification time
are checked before previews and before saving decisions about those files.
Unavailable candidates may remain unresolved while a note or another valid
selection is saved. These checks do not rehash every source or prove that a file
whose bytes changed while preserving its stamp is unchanged. Rejected pairs
are suppressed only while their prepared evidence remains current.

Changing prepared object evidence after saving requires explicit reconciliation;
the workbench refuses to silently reinterpret old decisions. There is no migration
command for already edited prepared inputs. The preparation adapter above preserves
the compared catalogue/evidence and retains prior review context. This output is
not yet a legacy accession input.

Previews render in memory. TIFF page selection reads image directories/composite
views; it does not inspect or validate Photoshop layer payloads. Unsupported or
unreadable images report a preview error. No preview is used to certify source
completeness, quality, or byte equality.

## Verification

```sh
cd /home/vladimir/Archive-tools
python3 -B -m unittest -v test_workbench test_search test_contract test_czkawka_config
node --check workbench.js
```

For all tests including actual SIFT extraction and background execution:

```sh
/mnt/volume/iss-archive-work/archive-tools-venv/bin/python -B -m unittest -v \
  test_workbench test_search test_contract test_czkawka_config test_sift test_pixel_coverage test_preparation_guards
```

Regression tests use synthetic temporary files. They cover false-match rejection,
explicit redundancy, stale sources, missing sources, Master replacement,
provenance, competing saves, completion locks, CSV recovery, protected paths,
HTTP save boundaries, and TIFF/JPEG/PDF previews. The comparison layout and
canvas interactions adapt the existing archive prototype; decision handling is
new and does not depend on that checkout at runtime.

## Shared meanings

| Term | Meaning |
| --- | --- |
| **Different photo** | Human review establishes that the two compared files show different photographs. Record a rejected match between that pair, even when their perceptual hashes have a small distance. This decision does not establish why the algorithm proposed the match. |
| **Redundant copy** | A copy or derivative of the same photograph for which an adequate high-resolution TIFF or JPEG Master has been identified and retained. This includes resized and cropped versions. Record which Master covers it. |
| **Master** | The selected representation retained for an archival object. A resolved object has exactly one Master. For photographs, prefer a genuine TIFF, otherwise the highest-resolution usable JPEG. The recommendation does not itself prove quality or completeness. |
| **Unresolved** | There is insufficient evidence to decide identity, redundancy, or the Master. Keep the candidate and the reason available for review. |
| **Metadata unknown** | No descriptive metadata is known for the photograph: for example, no established subject, title, creator, date, or location. Technical facts such as path, dimensions, format, and hashes remain available. If some descriptive facts are known, retain them and leave the rest unknown. |

“Unresolved former master” is not a separate category. A former Master is
reviewed using the same meanings as any other candidate; its previous selection
remains provenance. Replacing a Master does not by itself resolve the identity
of the displaced file.

## Matching and decisions

- pHash, aHash, dHash, and Czkawka visual results propose comparisons. A small distance
  can occur between completely different images; it is never sufficient to
  select a Master, declare redundancy, or assign descriptive metadata.
- Save a **different photo** decision for the compared pair, so subsequent
  matching can suppress that same rejected pair. Preserve the paths and file
  identity/freshness evidence used for the decision; changed or uncertain
  inputs require review before reusing it.
- **Different photo** is a relationship between two photographs. It says
  nothing about whether either has redundant copies elsewhere.
- **Redundant copy** records a relationship to a retained Master. A crop may
  show the same photograph while losing content; inspect whether the Master
  adequately preserves what the candidate contains before declaring redundancy.
- Exact whole-file checksums and perceptual hashes are separate evidence.
  Preserve algorithm and rendering-profile information with perceptual hashes.
  Do not treat a close visual match as byte equality.
- Descriptive metadata needs its own evidence. Do not transfer it from another
  photograph merely because a similarity search proposed that match.
- An unreadable file remains unresolved unless independent evidence establishes
  its identity and a sufficient surviving Master.
- Redundancy is a reconciliation decision. Deletion requires a separate,
  explicit, path-specific instruction.

## Toolset

| Script | Job |
| --- | --- |
| `workbench.py` | Implemented: compare candidates and save human Master, redundancy, different-photo, and unresolved decisions. |
| `inventory.py` | Report source, index, hash, format, and coverage information, including gaps and errors. |
| `search.py` | Implemented: prepare source-to-collection visual and metadata comparisons using cached hashes and Czkawka. |
| `inspect.py` | Inspect selected files, provide comparison previews, and compute explicitly requested missing hashes. Report TIFF page/layer coverage separately. |
| `accession.py` | Accession an explicitly authorized selection and generate its derivatives. |
| `check.py` | Check agreement among archive files, catalogues, decisions, manifests, and indexes. |

The four other entry points remain planned. `archive_common.py`, `matching.py`,
`catalogues.py` and `consolidation.py` are internal helpers for the two implemented
commands, not additional workflows. Use explicit collection adapters where existing formats differ.
Retain established object identifiers and readable decision records. SQLite
indexes remain rebuildable. Source collections stay read-only during review.

The meanings above describe photograph comparisons. Document and album adapters
must preserve ordered pages and completeness; a page count or a preview alone
does not establish a complete representation.

## Metadata research support index

[`visual_research.py`](visual_research.md) prepares disposable 1024 px research
previews and visual-neighbour reports for the current provisional new-object
cases. It reuses the workbench scope rules and local catalogue adapter, preserves
metadata evidence, and makes no identity, metadata-adoption or accession decisions.
Its model dependencies and generated artifacts belong in the separate work area.

## Focused metadata proposal review

When source-bound model proposals exist, the workbench opens their selection by
default. `?filter=model_proposals` shows one photograph beside editable title,
description, photograph date, location, photographer, subjects, inscriptions and
uncertainty/notes. Zoom, pan, Save and Save and next remain available. Other
filters retain the existing reconciliation view.

Saved edits live in the existing `decisions.jsonl` row under `metadata_proposal`,
with reviewer, time and exact candidate/source binding; `dispositions.csv` also
projects the edited proposal. This is work-area proposal storage, not catalogue
adoption or embedded metadata. Metadata saves preserve all Master/identity
decisions, review flags and existing notes. Original `model-proposals.json`
research remains unchanged by editing. A separate revision includes research
changes, so stale tabs cannot silently overwrite newer evidence or decisions.
Missing/changed sources and closed/read-only objects prevent saving.

The optional `consolidated_proposal` in an existing research record supplies
reviewed editorial overrides for the form, with `consolidation_note` explaining
their basis. Empty overrides deliberately clear superseded guesses. Original
model/OCR evidence stays intact; raw OCR is never automatically promoted into
these overrides. Saved human edits take precedence over suggested values.

Completed reference checks now feed the editable proposal and queue title through
the same `suggested_fields` projection. Supported identities supply the title;
direct INPUT observations supply the description; location and photograph date
keep their separate assessment status. Unresolved photograph dates remain blank.
Reviewed editorial corrections still take precedence. Existing saved fields stay
unchanged unless the reviewer chooses “Use latest research proposal” and saves.
Supported and contradicted candidates are omitted from the unconfirmed hypotheses
list; full findings remain in the original research record.

## Bounded reference research

Current runner: `reference_batch.py` (v11). The older runner under
`work-area/visual-similarity` is retained history. The versioned
`berlin-photo-reference-sources.json` contains the 19-source website registry;
use `--registry /home/vladimir/archive/Archive-tools/berlin-photo-reference-sources.json`
explicitly to select it. The existing Downloads default is preserved, and
existing plans still require their frozen script and registry hashes.

v8 preserves an explicitly discovered `image_url` alongside its `page_url`.
Article images retain captions, gallery context and observed resolution links;
related-content sections and footer images are excluded before selection. Each
round can download up to eight unseen photographs per page, within the existing
12-image pool and 18-attempt limits. Seen images do not consume those slots.
Pools of up to six are compared in full; larger pools receive a visual shortlist
before the selected full-size images reach comparison. Text ranking only orders
retrieval. These limits are bounded coverage, not an exhaustive website search.

Requested native detail crops are generated before that round's comparison.
The next discovery receives the retained reference photographs as well as INPUT,
crops and textual history. An unresolved requested research goal does not stop solely
because the model says `needs_more_references=false`. Receipts distinguish
`identified`, `search_exhausted` (within the attempted sources), `round_limit`,
`usage_limit`, and retrieval/search/comparison/integration failures. A successful
earlier comparison remains available after a later failure; `--retry-failed`
checks `stop_reason` as well as status, and batch status exposes retryable counts.

URL provenance separates a model-proposed URL, a URL in search results, a URL
requested by a web tool, and a successfully fetched page. An open action alone
is never reported as successful retrieval. Public-network guards still apply.

Use a fresh prefix after this schema change. `--compare-effort high` must be
passed to **prepare**; discovery, visual selection and comparison use the saved
setting. Passing a different effort to **run** does not override the plan.
The default remains low until bounded trials justify broader cost changes.

To separate comparison quality from retrieval, prepare an isolated one-object
control with `--discovery-mode direct --object-id OBJECT_ID --blind-pilot`,
`--reference-page-url PAGE_URL`, `--reference-image-url IMAGE_URL`, and
`--compare-effort high`. Supply the registry explicitly as above. This downloads
the named reference and compares it without web discovery. Named-object
pilots never attach results to live proposals. Prepare an adaptive pilot of the
same object under a different prefix to test retrieval separately.

v9 selects the largest observed variant of the discovered photograph and retains
`discovered_image_url` separately. It never constructs a larger-image URL.
Commons categories use the existing media API. File-description pages ending in
`.jpg` are not treated as image files. Linked originals and WordPress originals
retain priority; otherwise declared sizes outrank unknown dimensions.
Empty Commons file lookups fail explicitly. Search-generated relevance text is
kept separately and cannot become a documentary source title.
A fresh caption-blind call records visual matches and contradictions. Up to two
paired INPUT/reference details are read from guarded originals; a second blind
call inspects those details before the caption-informed assessment. Missing crops
are recorded as limitations. The raw model assessment remains in the receipt;
claims lacking a distinctive blind match are downgraded unless documentary evidence
supports them. Historical context alone cannot populate the photograph-date field.

`--research-goal identification|location|dating` is frozen at prepare (default:
identification). A supported requested field ends research even if an unrelated
field remains unknown. Follow-up discovery targets the outstanding visual feature,
not generic pictures of the same site. Visual calls count toward reported usage
and between-call limits. These extra checks can increase per-comparison cost;
validate with named-object pilots before scaling. Fresh v9 prefixes are required.

v10 addresses both follow-up reviews of commit `655bda5`:

- Layout classes such as `no-sidebar` retain article photographs. Actual related
  sections remain excluded; picture variants share a candidate and video sources
  are ignored. Failed originals fall back only to observed variants, within the
  existing attempt/byte limits. Receipts distinguish delivered variants, fallback
  failures, downloaded dimensions and original availability.
- Existing INPUT detail crops reach blind verification with neutral banners.
  Missing/duplicate blind assessments become insufficient; unknown IDs are dropped.
  Documents-only rounds skip the blind call. Crops use verified downloaded pixels
  without enlargement; a downloaded derivative is not described as the original.
- Image-citing claims require a cited visual match, even when a document is also
  cited. Hypotheses and displayed conclusions use the final assessments. Raw output
  survives cached reruns. The workbench displays final fields for older receipts too.
  Visible INPUT dates remain tentative time clues, never supported exposure dates.
  Rejected images are excluded from retained evidence.
- Tool-requested image URLs have separate provenance. Successful retrieval does
  not establish association with a proposed page. Downloaded candidates omitted
  from comparison remain queued; adaptive and portal modes inspect them before
  another search. Round/budget limits leave remaining candidates explicitly listed.
- Default item and average allowances are 150,000 tokens for low/medium adaptive
  work and 500,000 for high adaptive work. Other modes default to 30,000 or 120,000
  respectively. Explicit overrides apply. These between-call thresholds are not
  hard billing caps; reasoning still defaults to low. Goal completion reports
  `identified`, `located`, or `dated`.

The v10 validation used fresh prefixes. Regression tests, saved-response replay and a real
derivative download under an injected original failure verify these changes;
they do not establish a new model-recognition accuracy estimate.

v11 addresses the reviews in `e679b1c` and `a0e187e`:

- `--max-rounds` counts discovery rounds. Bounded inspection batches drain downloaded
  candidates without consuming the next search. Features, pending-page documents and
  actionable follow-up questions survive inspection. Token, query and download limits
  still apply; each discovery has at most 12 candidates, with a finite overall batch
  ceiling recorded in the plan. Receipts distinguish `discovery_rounds` from the
  comparison batches in `rounds`.
- General page text alone cannot establish a supported claim. It needs a visually
  matching photograph from that page, or an explicit reviewer binding to INPUT.
  Documentary evidence can remain independent of an incidental rejected photograph.
- A reference photograph's date cannot date INPUT, even when it depicts the same
  building. `photo_record` requires a reviewed photograph binding; unbound dates move
  to limitations. Visible time clues remain tentative. Overview banners read `INPUT`;
  native detail crops have distinct numbered labels.
- Layout modifiers such as `sidebar-right` retain article images. Oversized HTTP
  responses are rejected before reading the body when Content-Length is provided;
  the byte limit still applies without that header. Oversize failures are cached for
  24 hours within the prefix, allowing observed smaller variants to be tried.

Prepare a fresh v11 prefix. An optional `--document-bindings /absolute/private/file.json`
at **prepare** freezes reviewer-supplied evidence links in the plan. The file is a JSON
list with this structure (use the exact object ID, preview hash and retrieved record URL):

```json
[
  {
    "object_id": "OBJECT_ID_FROM_PLAN",
    "preview_sha256": "SHA256_FROM_PLAN",
    "record_url": "https://museum.example/photograph-record",
    "fields": ["depicted_object", "photograph_date"],
    "explanation": "Reviewer matched the photograph inventory mark to this record."
  }
]
```

Bindings describe evidence links, not catalogue adoption. They apply only to delivered
documents with that exact URL and listed fields; they do not fetch a record automatically
or establish an answer without its contents. For `photograph_date`, bind the exact
photograph, not merely its subject. Bindings are incompatible with `--blind-pilot`, and
model output cannot create them. Keep the file and plans outside Git.

The batch still stops when an individual image reaches its token threshold, or the
batch-average threshold is exceeded after at least three items. This cost safeguard
is unchanged; use `--limit 3` for a measured pilot. No new recognition-accuracy claim
follows from the offline regression checks.

`reference_batch.py` v7 uses subject-specific routing for adaptive visual web discovery.
The shared `research_routes.py` contract covers buildings/places, machinery and
industrial interiors, objects/artworks, events, people/activities, documents/signs,
and an unresolved fallback. Initial grouping uses existing descriptions without a
model call. Discovery reports a primary route, up to three secondary routes,
confidence and reason; it can revise automatic grouping from the actual image.
Category guidance changes source preferences and comparison criteria, while broad
discovery remains available. Each image/call retains its own fresh context.

The JSON registry's `selection_policy.subject_routes` owns preferred source IDs for
these groups. Source `best_for` and `caution` fields guide specialist selection;
machinery searches include technical collections and, when relevant, mill manuals.
Building sources remain available for installation-site research. Discovery uses a
matching preferred source within the existing query budget and may search elsewhere.
Changing registry mappings requires a new frozen plan, not a Python code edit.

Image retrieval prioritizes each asset's own filename/caption, excludes labelled
logos and binds object-slug assets when available. Page text stays separate from
image captions. Revisited pages do not advance into footer pictures simply because
their useful images were seen. Evidence cited for specific live hypotheses is
retained before generic type references, together with associated documents.

The workbench offers a research-group filter and a collapsed research-route editor.
Reviewer overrides save alongside existing proposal fields in the decision record,
separately from catalogue metadata. Automatic grouping can be restored. Plans freeze
the effective routing and shared helper hash; subsequent edits require a new plan.
An explicit override wins over a model's regrouping suggestion. Grouping confidence
is about research strategy, never confidence in an object identification.

A fresh call
receives the positive, correctly oriented research image and may follow new
candidates across public websites. The JSON registry guides verification without
restricting discovery. Each round allows six search queries and eight web actions,
four returned pages, six new individual reference images and up to two retained
images. At most two rounds run; comparisons have no web access. URL provenance,
guarded fetching, receipts, source stamps and unadopted proposals are retained.
Missing search provenance is an integration error, never a silent no-match.

Native overview/details use the saved, visually checked page/polarity/rotation
recipe on the protected read-only source. Research images have a maximum edge
of 1800 pixels; original TIFFs and layers are not modified. HTML extraction handles
lazy images and srcset, skips placeholders, and deduplicates WordPress sizes.
There is no contact-sheet rejection gate in adaptive mode. New crops and compact
comparison/retrieval history reach the next adaptive search. Named-object pilots
remain isolated from the workbench; their receipts record the exact input view.
For consumption measurement, `--max-item-tokens 0 --max-average-tokens 0` disables
usage cutoffs while retaining round, search, download and timeout bounds.

The previous workflow is available with `--discovery-mode portals` for controlled
comparisons. The following details describe that retained path, which combines
the visual shortlist/retrieval work from
commit `384404f` with registry-driven source searches, documentary evidence and
recovery fixes. The maintained implementation lives here in `Archive-tools`;
runtime plans, downloaded evidence and receipts stay in `visual-similarity`.

The JSON registry is executable search configuration. Its subject routes,
priorities, query templates and budgets select two initial sources, at most four
sources and six primary search queries over at most two rounds. Commons has
direct API retrieval; Denkmaldatenbank uses its native search and observed record
links, including full-size photo links. The monument WFS is geographic data,
not a substitute for record text or photographs. Other selected portals use
bounded domain searches from the JSON templates. A second, unresolved round may
use one wider query within the same source/query limits. Every model-returned
record URL must occur in actual web-tool results before the controlled fetcher
can use it. Unknown/private hosts and unsafe redirects remain rejected; exact
media URLs observed in retrieved HTML may use public CDN hosts.

Each round records explicit source/query pairs and avoids pairs already tried.
Second terms receive their own searches; evidence qualifiers such as
`(Hypothese)` are removed from catalogue query names. The scheduler reserves
room for wider search before selecting another portal, including with a
three-source cap. A missing supported event-provenance field is reported as
`integration_error`, never as an empty search. The inspected CLI events expose
`item.results`; `action.sources` is also accepted when explicitly present.

Visual discovery receives the photograph and can propose new candidates.
Original guesses, reviewer identifications, reviewed OCR clues and consolidated
proposals remain separately labelled, unadopted context. No raw OCR dump enters
the prompt. Named Wikidata entities must have one exact matching label before
coordinate/category retrieval is used; even then they remain candidate leads.

Named candidates are stored separately as `candidate_queries`. Structured
reviewer-supplied artwork names are retained as unverified search candidates.
Crop refinement merges new clues with existing candidates instead of replacing
them. A comparison's explicit follow-up question keeps query priority through
refinement, so a date search does not silently revert to identifying the object.
Generic suggestions from a pixel-only shortlist supplement the candidates;
they do not override named candidates. Only a completed comparison establishes
a new prioritized follow-up question. Candidate artwork queries also precede
speculative setting/place queries when choosing a broader search.

The candidate pool interleaves sources and retains different views from a record,
deduplicating images rather than record pages. A labelled contact sheet shortlists
up to four plausible references, reserving space for an institutional candidate
when the visual shortlist supports one. It never forces an irrelevant image into
the comparison. A conditional second round carries compact contradictions,
rejected candidates and unresolved questions. It may obtain real detail crops
from the source with the existing exact read-only mount, stamp and checked recipe
guards. A small clue-extraction call reads those crops before the second retrieval,
so newly legible inscriptions can change the next queries. This call is included
in usage accounting. Preview crops are never enlarged to claim new detail.

Evidence distinguishes reference photographs, catalogue text and archival
documents. HTML records survive without images; PDFs retain relevant excerpts and
page numbers (text extraction covers at most the first 40 pages, using existing
`pdftotext`). Scanned PDFs without extractable text are explicitly access-limited.
Claims can cite delivered documents or compared photographs. Object identity,
location, event and photograph date are separate assessments; object creation and
reference-photo dates cannot automatically date the input photograph.
Directly visible descriptions belong in `observations` and cite `INPUT`.
They need no external citation, but cannot establish a supported named identity,
location, event or date. Those assessments still require compared images or
documentary evidence. A generic scene description therefore does not abort a
follow-up search merely because it has no external reference.

Preparation and runs require an explicit new prefix. Old v1/v2/v3 plans are retained
and cannot be resumed through the changed schema. A fresh plan freezes the
registry, script/helper hashes, settings, preview hashes and prior clues.
Normal plans exclude the earlier researched pilot; named selections and
`--pilot-only` are isolated evaluations that never attach results to the live
proposal sidecar.

Example isolated evaluation (the commands below are not automatically scheduled):

```sh
python3 -B reference_batch.py prepare --prefix reference-v3-pilot \
  --object-id VASSET-9D6310E06E61F11CEBF389E626FF2030 \
  --object-id VASSET-FCE76B5E42A1D38194E43D6D6B7BFB38 \
  --object-id VASSET-65B9F846C4B9C4A4A31344BDC48D11FE
python3 -B reference_batch.py run --prefix reference-v3-pilot --limit 1
python3 -B reference_batch.py benchmark --prefix reference-v3-pilot
```

Select 6–8 images for the full pilot, including known identities, unresolved
examples and difficult access cases. `--blind-pilot` withholds prior hypotheses
and reviewer hints. Optional `--expected /work/path/expected.json` supplies
independently verified expectations as `{"objects": {"VASSET-…": {...}}}`;
these are never sent to the model. The report requires human scoring of correct
identifications, useful discoveries, unsupported claims, preserved uncertainty
and token use. Earlier AI findings are comparison baselines, not ground truth.

All model calls use fresh ephemeral contexts, no shell and low reasoning by
default. Web tools are enabled only in bounded source-search calls.
`--no-web-search` explicitly records selected non-native portals as unsearched.
The default 30,000-token item/average thresholds are checked between calls;
they are stop thresholds, not hard billing caps. Discovery/search/shortlist calls
time out at 150 seconds, comparisons at 300. Web calls/queries are monitored and
over-budget results rejected. Interrupted calls may omit usage; receipts flag it.

Successful calls and retrieval checkpoints are reused on continuation. Interrupted
calls get a new attempt filename on a subsequent explicit run; complete failed
calls need `--retry-failed`. No in-run model retry loop exists. All attempt logs
and receipts remain. HTTP failures expire: five minutes for transient/403 errors,
one day for 404/410; a failed URL is not requested repeatedly within one process.
Empty successful searches are `no_results`, distinct from `access_limited`.
A provenance integration failure is counted separately as `integration_error`.
A later failed round cannot erase an earlier valid assessment. A single-worker
lock prevents concurrent runs under the same prefix.

Normal runs attach only `reference_check` to the existing proposal sidecar.
Concise conclusions and image/document source links use the existing workbench.
Original AI/OCR evidence, consolidated suggestions, reviewer edits, Master
selections and catalogue/source files remain intact; `metadata_adopted=false`.

Offline checks: `python3 -B -m unittest test_reference_batch -q`. They use the real
helper/schema modules, mock model/network calls and never mutate archive data.
A live Denkmaldatenbank retrieval smoke on 2026-11-09 returned catalogue text,
full-size photo URLs and a visually inspected downloadable reference image.
This verifies retrieval, not identification accuracy or all twelve portals.
