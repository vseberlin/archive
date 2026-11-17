# Archive research, extraction and workspace consolidation plan

Created: 14 November 2026. Current checkpoint: 16 November 2026.

## Continuity consolidation — 16 November 2026

The central hub now owns the sole private checkpoint at
`/home/vladimir/archive-local/HANDOFF.md`. It is capped at 900 words;
historical evidence and unit notes remain private beside their work.
See [continuity](archive-continuity.md). This supersedes earlier handoff
locations below and does not start the extraction or migration phase.

## Home consolidation completed — 15 November 2026

Following the planning checkpoint, the user approved consolidating the archive
working folders into `/home/vladimir/archive-local`. Source now lives at
`/home/vladimir/archive-local/source`; the private pilot and recreated environment
are in `research-pilot/`; visual/restoration indexes are under `indexes/`.
Earlier home-based preservation/provenance work and loose databases are retained
under `retained/`, with no disposal classification. On 16 November, the separately
approved home cleanup relocated the large inactive Paragon recovery tree into a
verified volume tar bundle; referenced `archive-core-v2-preservation` and active
pilot/index state remain local. This is retention relocation, not the planned
runtime or extraction migration. The hub KEEP README owns current locations.
The KEEP README and private
migration receipt in that home root record the exact locations. Historical source
paths in evidence and receipts were preserved. Accessioned holdings, protected
USB originals and volume-based work remain in place.

This is the approved home-layout change, not implementation of the extraction
pipeline or the later volume consolidation. Existing code/index consumers,
inactive service definitions, routing and current operating documentation now
use the real new paths. Old home aliases and home-facing navigation symlinks were
removed. No indexing or shutdown service was started; new bulk work still requires
adequate free space. The private handoff records validation and current runtime.

Necessary Linux runtime remains on home ext4 in this single folder. The
portability TODO below remains binding: NTFS quirks must not become a permanent
blocker. Retained environments and old work are not authorized cleanup targets.

## Current agreed direction and checkpoint

The user requested this consolidated plan and a local commit before starting
new changes, then authorized the stage-1 tools/source/consumer/storage audit on
16 November, followed by the next package/configuration slice. That bounded audit is complete; the
[private report](/mnt/volume/iss-archive-work/tools-inventory-20261116.md) records
coverage, unresolved mappings, storage estimates and verification limits. The
[tool map](archive-application-structure.md#extraction-and-support-inventory--16-november-2026)
owns reusable routing. Stage 2 now delivers the standard-library
`src/archive_research` package, explicit TOML root mapping and an opt-in wrapper
for the existing visual preview function; see its
[API guide](../../src/archive_research/README.md). The subsequent end-to-end
authorization delivered a bounded stage-3/4 implementation: explicit selected
members, positioned native PDF text and ALTO OCR, per-page resume, and workbench
page search/retained image leads. The private
[trial report](/mnt/volume/iss-archive-work/positioned-extraction-trial-20261116/report.md)
records 13 tasks, measured resources, static alignment checks and remaining
acceptance limits. Explicit private project configuration connects the trial;
one manual loopback pilot runs. No broad indexing, migration, dependency
installation or disk cleanup occurred. Existing visual/photo commands retain
their formats. The user confirmed the requested browser word highlight after browser-control
errors. Collection transcription accuracy remains unverified. The user then directed
mounting Elements; the exact protected read-only mount was verified. Follow-up
processing stays within the already selected sources and pages.
The earlier pilot proposal and delivery history below remain context.

The already-delivered local pilot is checkpointed separately as `6ebe63b`.
It includes login, 16 real evidence cases, 51 readable previews plus one explicit
unreadable TIFF, source citations, independent review/correction history and
optional physical-object references. The synthetic demonstration remains separate.
The last verification left all real observations unreviewed and unadopted.
A physical-object reference is optional on every real case: existing inventory
reference/link, last recorded location, source/person/date and relation to the
pictured object. Empty is permitted and does not assert survival or loss.
Saving a reference must continue to preserve the research review outcome.
Book publication information, source lineage, missing report sections and
PDF-versus-printed-page distinctions remain in private evidence; the handoff
links its focused research evidence guide.

Build a shared extraction foundation and archive-wide page/region search, with
one maintained source repository and existing authoritative unit decisions.
Reuse the visual-feature work through adapters before changing its internals.
Working modules must not become one monolithic extractor script. The eventual
workspace should have real canonical locations, without permanent compatibility
symlinks back to scattered old directories.

**Portability is an explicit requirement. NTFS-specific limitations must not
become a permanent product or deployment blocker.** Retaining necessary Linux
runtime components in `~` is a temporary workstation accommodation, not an
architectural dependency on this user's home directory or on NTFS.

## Observed baseline for the next phase

The 16 November audit supersedes the older storage observations below: home
has substantial free space after cleanup and the required Elements mount is
absent. The later trial starts one manual pilot; consult the handoff for current
socket ownership. The private report contains fresh
catalogue/index counts and the proposed current-root map. All 14 unit catalogues
and their named mapping tables were inventoried; exact PDF/TIFF alignment, full
payload readability, complete page/frame counts and destination runtime properties
remain unproven. Keep these limits separate from completion of the inventory.

Read-only inspection on 15 November 2026 found:

- Fourteen units under `/mnt/volume/iss-archive/units`, with heterogeneous
  catalogues, ordered Master representations, derivative and provenance records.
  Existing mappings are inputs; completeness across every unit is not yet audited.
- `work-area/document-index/document_index.py` matches its current runtime copy.
  The index has 42,087 document records, largely from Elements, including 25,350
  PDFs: 5,758 header-extracted, 19,548 skipped-known-unit and 44 error records.
  It is a source-discovery index, not a complete index of accessioned archive pages.
- PDF extraction defaults to three pages and allows at most ten; one combined
  text value represents a document. Native text and OCR lack retained word boxes.
  Known-unit skipping is appropriate for reconciliation discovery, not research
  coverage. Preserve that existing consumer behavior while adding explicit full
  research coverage rather than silently changing its meaning.
- Extraction reuse is keyed by content hash without a complete processing recipe.
  Raising a page limit alone would not safely replace old cached results. Page
  coverage and recipe-aware invalidation must be introduced together.
- The pilot OCR command reads reduced prepared previews, uses English and emits
  plain text. It is a bounded demonstration, not the full-resolution archive OCR
  implementation. Existing Tesseract 5.5.0 has `deu`, `eng` and `osd`; Poppler has
  bounding-box extraction. No new model availability or accuracy is assumed.
- Visual code already records some extractor versions and processing recipes;
  multiple entry points and differing contracts remain to be reconciled.
- `/home` is ext4, about 99% used with 1.9 GB available. `/mnt/volume` is currently
  NTFS/FUSE with about 4.5 TB available. Source is about 32 MB; the home visual
  index is about 312 MB, legacy restoration directory 1.2 GB and pilot 142 MB.
  These are observations, not cleanup targets or a complete disk-use census.
  The document index already occupies about 6.3 GB on the volume.
- `/mnt/volume/archive-system` is currently a navigation/setup repository whose
  links do not relocate their targets. Mount device names have changed between
  observations; operational checks must use verified identity, not a stale note.

## Storage now, consolidation later

For now retain the canonical source checkout
`/home/vladimir/archive-local/source` and the necessary Linux-sensitive runtime
in its consolidated private home location:
Python environments/executables, private credentials/configuration and the live
pilot account/session database and review records. The subsequent authorized home
move is recorded above; further volume migration remains separate from the
extraction work. No blanket exemption permits
unbounded new model downloads, renders or OCR output into home.

Future bulky indexes, extraction artifacts, models, renders and temporary
processing output should use explicitly configured volume locations after a
bounded destination/space check. Existing volume-backed work stays where it is
until its consumers are mapped. A temporary split must be visible in one machine
configuration; tools must not silently fall back to `~` if a configured volume
is absent, full or unsuitable. Runtime output is always excluded from Git.

Long-term intended real layout beneath `/mnt/volume/archive-system`:

```text
source/        the actual maintained Git checkout, if/when relocated
work/          readable research records and operational receipts (handoff: archive-local)
indexes/       rebuildable document, visual and provenance projections
artifacts/     reusable OCR, regions/crops and extraction results
cache/         disposable renders and model caches
environments/  recreated compatible runtimes
config/        private machine configuration and named source-root mappings
```

This is a proposed destination, not directories to create now. The eventual
source remains one repository with its history, not a second editable clone or
a nested duplicate authority. Update the current setup repository's ownership
and navigation documentation when that move is actually undertaken.
Accessioned holdings already live at `/mnt/volume/iss-archive`; relocating those
units is a separate, explicitly scoped operation because evidence uses their
paths. Protected Elements sources remain read-only and outside workspace moves.

Qualify storage before moving active runtimes: effective access protection
(0700/0600 checks for the current Linux deployment, native effective-access
checks on other platforms), independent-process file locks, atomic replacement and file/directory
sync, SQLite transactions/recovery in the selected journal mode, timestamps,
filename handling, executable requirements and representative small-file costs.
A passing smoke test does not certify power-loss safety. Existing database use
on the volume does not prove all these properties. If a requirement fails,
keep that component on compatible storage behind configuration; do not weaken
privacy/source checks or abandon portability. Filesystem replacement or volume
repartitioning is outside this plan's implementation scope.

## Portability guardrails — binding from design onward

Agreed 16 November 2026. Cross-platform implementation and acceptance may come
later; these constraints apply to new design and changes now. They belong to
this plan, not a second framework, registry or metadata authority.

1. **Keep identity independent of the filesystem.** Preserve explicit object
   and representation IDs. Do not use case differences, paths, drive letters,
   inode/device numbers, symlinks, hard-link relationships, ownership or
   modification times as essential object identity. These may be locators,
   provenance or local change/race observations. A content hash identifies bytes,
   not necessarily an archival object: identical bytes do not merge holdings.
   Existing path-shaped identifiers remain opaque identifiers; remapping a root
   must not silently rewrite them or break citations. Do not introduce a parallel
   identifier system to achieve portability.
2. **Preflight names and paths without changing sources.** Detect destination
   collisions involving case or Unicode normalization, reserved names/characters
   and path-length limits. Preserve original names and paths as provenance.
   Do not silently normalize, truncate, rename, merge or overwrite conflicting
   entries. Any necessary destination-name mapping must be explicit and
   reversible and must preserve object identity. Windows naming constraints
   illustrate why source filenames alone are insufficient as portable storage
   keys; see [Microsoft's naming rules](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file).
3. **Separate application authorization from disk protection.** Application
   roles govern application operations. Direct filesystem access, credentials,
   databases, exports and backups have a separate protection responsibility.
   Login does not prevent someone with disk access from reading or altering files;
   filesystem restrictions do not replace application authorization. State the
   intended users and access requirements for each component and verify both
   boundaries independently.
4. **Specify security outcomes, not translated permission bits.** For example,
   require that only designated accounts can read credentials, then verify that
   outcome with the target platform's native security model. Do not translate
   Unix modes directly into Windows permissions or assume Unix parent-directory
   traversal rules apply there. Check effective access, inheritance and relevant
   child files, not merely a successful permission-setting call. The current
   Linux mount's failed chmod probe does not establish that NTFS or Windows
   cannot protect files; Windows uses
   [security descriptors and ACLs](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights).
5. **Never weaken a failed atomic write into a live-file copy.** Prepare and
   validate a temporary file on the destination filesystem, then use a supported
   replacement operation. Missing capabilities, cross-filesystem replacement,
   access errors or open-file conflicts must produce explicit failures. Never
   silently fall back to copying over, truncating or deleting the live target.
   Failure before replacement must leave the old live file intact. If replacement
   succeeded but subsequent synchronization failed, report uncertain durability;
   do not claim that nothing changed or blindly retry an already applied action.
6. **Treat atomicity, durability and concurrency separately.** A successful
   rename is not proof of power-loss safety, a multi-file transaction or writer
   exclusion. Qualify replacement, file/directory synchronization, locks and
   database recovery on the actual target filesystem. Do not infer capabilities
   from an OS name or filesystem label alone. If a required guarantee is absent,
   fail explicitly or retain that component on a configured compatible location;
   never report a weaker operation as equivalent. See the relevant
   [Python filesystem interfaces](https://docs.python.org/3/library/os.html#os.replace).
7. **Make deployment locations and OS operations explicit.** No implicit cwd,
   home-directory, drive-letter or permanent-symlink dependency; no fallback to
   another root when the configured one is absent, full or unsuitable. Keep
   platform-specific mounting, file access, locking, permission and process
   handling behind small, explicit boundaries. Retain the current exact protected
   Elements mount check on this workstation until an equivalent deployment
   protection is explicitly defined and tested. Recreate environments from
   specifications rather than copying executable environments between platforms.
8. **Keep records and verification portable.** Specify encodings, timestamp
   meanings/time zones, coordinate units, ordering and schema versions. Filesystem
   enumeration order must not determine representation/page order. Keep durable
   records readable and caches rebuildable. Add focused conformance checks for
   root remapping, changed timestamps with unchanged content, filename collisions,
   inaccessible/redirected sources, competing writers, full disks and interrupted
   or refused replacement. Real target-platform acceptance remains necessary;
   mocks or one workstation's smoke tests cannot certify another deployment.

### Current implementation limits

The existing writer already has no copy-over-live fallback. Source size/mtime
checks and inode comparisons serve local change/race detection; they are not
archival identity. However, mtime and locator fields currently participate in
cache keys. Portable relocation/rebinding and content/recipe-based cache reuse
must be addressed explicitly without weakening changed-source rejection or
altering existing IDs. The runner and filesystem code also use Unix-specific
facilities such as `fcntl`, `resource`, `findmnt` and file-open flags. Isolate and
qualify those implementations as portability work proceeds. Do not describe
standard-library-only code as automatically cross-platform. These are tracked
implementation gaps, not permission to redesign the archive or rewrite current
source bindings during this documentation change.

## Package and extractor contract

The user approved consolidation into one installed application on 17 November
2026. Its implementation now lives under `src/archive_research`, installed by
`pyproject.toml` with the `archive-research` command:

```text
src/archive_research/
    cli.py, config.py, pipeline.py
    contracts.py, sources.py, storage.py, units.py
    extractors/       OCR, text regions, PDF/scan layout, visual features
    runner.py         isolated selected-task workers and resumable receipts
    retrieval.py, evidence.py, profiles.py, sift.py
    assessment/       request preparation, transports and validation
    web/              Django application, templates and synthetic fixtures
tests/                contracts and installed application behavior
```

CLI and HTTP adapters share package operations. The bounded `run` command can
extract, index, search and prepare a packet/request; it never submits externally.
HTTP consumes prepared artifacts. Legacy research script paths and old import
names forward to canonical implementations. The active package imports no legacy
tools and needs no checkout-specific `PYTHONPATH`. Reconciliation/accession and
completed unit jobs stay in their existing scopes.

Core imports have no third-party runtime dependency. Optional web/image/vision/
embedding requirements are declared in `pyproject.toml`; heavy libraries load
only for selected operations. Existing pinned environments and local model files
are preserved. A normal wheel includes templates and invented fixture data.
The Django app label stays `research_web`, preserving existing SQLite table and
permission identities. No schema migration or new service/database is introduced.

The runner records progress after each isolated task, reuses matching completed
receipts and rejects changed bindings. Existing source/page/result schemas,
output formats, review authority and explicit root constraints are unchanged.
See the [application guide](../../src/archive_research/README.md) for commands
and dependency boundaries. This packaging step does not qualify new operating
systems, archive-wide processing or extraction accuracy.

Each extractor declares its input kind, implementation/model version, settings,
dependencies and output kind. A result must identify:

- Existing unit/object identity, representation/member, page/frame and optional
  region, with named root plus relative location and the verified source binding.
  Preserve historical absolute source paths as provenance; do not regenerate
  existing identities merely because a workspace or mounted root moved.
- Input dimensions, coordinate units/origin, page sequence, printed page label
  when supported, rotation and all crop/scale/deskew transforms needed to map
  output coordinates back to the source. PDF pages, TIFF frames and logical page
  order must be explicit; they are not interchangeable counters.
- Extractor/model identity and version or model fingerprint, language, resolution,
  preprocessing settings, requested coverage, dependencies and processing time.
- Output files, machine confidence/limitations and per-page completion, partial,
  failure or changed-source information. Blank text is not proof of a blank page.

Keep durable results readable outside SQLite, with rebuildable search projections
and compatible schemas. Existing indexes need not be merged into one database.
A shared contract and source mapping matter more than a universal table.

Candidate extractors and distinct output meanings:

| Extractor | Output and boundary |
| --- | --- |
| PDF text | Words and layout positions; retain native/embedded-OCR provenance where known |
| OCR | Machine text, lines, word positions and confidence, bound to exact source imagery |
| Layout | Text, caption, table and illustration regions with reading-order information |
| Image/region extraction | Embedded images or prepared crops with page and transform lineage |
| Visual features | Existing perceptual/local features and comparison results, not object identity |
| Embeddings | Versioned image/page/crop vectors for retrieval; no automatic relationship decision |
| Face detection | Face regions for comparison, not automatic identity assignment |
| Object/building/shape detection | Region/shape proposals, with model limitations and source coordinates |
| Captioning | Generated descriptions distinct from source-printed captions and reviewed metadata |

Implement only the first required extractors; the remaining rows are extension
points, not scheduled installations or model runs. Handwriting, historical type,
small stamps and marginalia need representative evaluation; a generic OCR pass
cannot be assumed sufficient for all archive material.

## Global mappings, complete coverage and positioned OCR

Build the global view from unit catalogues and representation manifests:
`unit -> existing object -> selected representation -> ordered member/page`.
Include accessioned PDFs, TIFF-only material, mixed representations and retained
contextual documents with explicit roles. Do not indiscriminately index software
files, exports, caches or manifest copies as independent historical evidence.
Record unmapped or inconsistent cases rather than inventing their correspondence.

Import established PDF/TIFF page mappings and Master decisions, including gaps,
duplicate views, spreads and incomplete excerpts. Missing mappings remain review
leads; filenames, file extensions, similar text and embeddings are insufficient
proof. A global projection must not reselect Masters or merge cross-unit objects.
Reuse stored hashes for unchanged sources. Reuse processing for verified identical
content where appropriate without conflating distinct holdings or republications.

Prefer a verified selected TIFF scan for detailed OCR where available. Use an
appropriate full-resolution, source-bound processing image, not the UI thumbnail;
record preparation as research output, not as a new preservation derivative or
Master. PDF-only Masters remain valid inputs. Extract usable embedded PDF text
with positions; inspect coverage per page so one text-bearing page cannot hide
image-only pages elsewhere. Preserve old embedded OCR separately when rerunning.

ALTO is the preferred initial retained OCR format, subject to the bounded trial;
hOCR is an acceptable alternative if better supported by the proven path. Choose
one canonical output first. Retain raw output and explicit coordinates; a UI
adapter can normalize them without creating a competing editable text authority.
Confidence is a machine score, not proof. A generated caption, literal source
caption, OCR transcription and human correction must remain distinguishable.

Resume at page/member or region granularity. Cache reuse must include source
binding/content, page/region, processing transforms, extractor/model version,
language and settings/coverage. Retain completed work through interruptions;
retry failed or invalidated work selectively. Bound concurrency and memory, allow
realistic large-TIFF timeouts, and record partial results honestly. Text extraction,
OCR, embeddings and captions must not rerun merely because an unrelated extractor
changed. Updating a crop invalidates its dependents, not every result for the unit.

## Research UI, evidence and quality acceptance

Search across units and return exact page/region hits that open the bound image
with correct highlights. Preserve unit/object identity, source role, page order,
printed labels, lineage, missing coverage and current OCR status. Keep reviewer
text and historical citations intact across extraction refresh; an old region
must not silently point at a changed source or processing result.

Report numbers, inventory references, names and product types can suggest
cross-unit links. Repeated images require visual comparison as well as text.
Search similarity, repeated captions and co-occurrence remain proposals, not
independent corroboration or confirmed same-specimen identity. Review does not
adopt catalogue metadata, change Master selection or authorize accession.
Physical-object references continue to use the existing optional case fields.
Existing real-material export restrictions remain until sharing permission is
explicitly established; portable software is distinct from redistributable data.

## Next-phase TODO and ordered execution

- [x] **0 — Commit this plan before implementation.** Preserve the delivered pilot
  as a separate baseline. Keep one canonical plan and private handoff; no push.
- [x] **1 — Audit sources, consumers and storage.** Inventory all 14 unit mapping
  formats, PDF/TIFF coverage, existing extractors/index consumers, environment
  requirements, path-derived identities and absolute paths. Produce bounded
  coverage/error counts and a proposed root map. No broad rehash or OCR run.
  Include expected output volume, home-space needs and destination suitability.
  Completed 16 November at bounded metadata/static-inspection depth; see the
  private audit above. Storage capacity was checked, not runtime qualification.
  Unresolved page counts/order/alignments are recorded for adapters and the trial;
  no global complete-coverage or source-readability claim was made.
- [x] **2 — Establish the package/configuration boundary.** Introduce the smallest
  shared source/page/region/result contract and explicit root configuration.
  Wrap existing visual code; preserve existing outputs and supported CLI behavior.
  Verify missing-volume failure, changed-source rejection and dependency isolation.
  Delivered 16 November: standard-library contracts/roots, optional legacy-preview
  wrapper, synthetic refusal/identity/recipe tests and unchanged legacy preview
  output. The core wheel imports without optional dependencies. No real source
  processing, live configuration deployment or storage qualification occurred.
- [x] **3 — Prove page mappings and positioned extraction.** Use a bounded sample
  across a TIFF sequence, PDF-only report, mixed book/photo material, rotated page,
  small label/marginal text and missing/unreadable source. Measure text/region
  accuracy, coordinate transforms, coverage, time, peak memory and disk output.
  Demonstrate interruption/resume and recipe-aware cache invalidation. No large
  batch until the exact-source-to-highlight flow is visually checked.
  Implemented and trialled on 16 November: 6 text-bearing results, 5 explicit
  no-text partial results and 2 expected synthetic failures. Full-resolution
  processing, native/new OCR separation, orthogonal transforms, measured resources,
  resume and selective recipe invalidation are verified. Three static alignment
  figures passed visual inspection; the user confirmed the requested browser
  word highlight. A paired diagnostic of two source snippets records spelling
  and insertion errors separately; upright preparation improves that small sample.
  This closes bounded engineering acceptance, not collection transcription quality.
- [x] **4 — Connect archive-wide evidence to the workbench.** Show page hits and
  related-image leads across units through the existing case/citation interface.
  Verify source attribution, retained reviewer edits, correction history and
  partial-result presentation. Do not add automatic metadata adoption.
  Implemented bounded search across Trafo/FEB/Bibliothek and six retained image
  leads. Automated requests check attribution, stale-link/source refusal, review
  byte preservation and partial results; existing review/history behavior passes.
  The requested browser word highlight is user-confirmed. Elements was mounted
  read-only on explicit user instruction; its existing bound image leads are
  checked again before use. Broader independent-operator acceptance remains open.
- [ ] **4a — Prepare web-assisted multimodal hypothesis assessment before curator review.**
  Added to the plan on 17 November at the user's request. Retrieve local and
  external evidence, compare actual source images, and prepare cited competing
  hypotheses for a bounded OpenAI assessment. Follow the
  [implementation and acceptance scope below](#web-assisted-multimodal-hypothesis-assessment).
  Local implementation is delivered; real-material API submission still requires explicit
  permission for the selected material. Preserve independent curator answers
  before exposing model assessments when evaluating accuracy.
  Profiles, source-bound regions, crop OCR, embedded/scanned figures, tentative
  caption bands and readable multimodal packets are implemented separately.
  A retained institutional web reference has actual-image comparison and lineage;
  local museum-digital duplicates are not counted as independent evidence.
  Local-only and web-enriched requests are prepared, with bounded transport,
  exact-request submission checks, citation validation and unreviewed presentation.
  Live API/account qualification, permitted external material, independent curator
  evaluation and further detector accuracy remain open.
- [ ] **5 — Expand coverage with measured limits.** Process the agreed archive
  scope resumably, report complete/partial/error coverage and reuse existing valid
  work. Keep discovery-index consumers compatible. Add layout, crop, embedding
  or caption extractors only for demonstrated needs with separate evaluation.
  Bounded follow-up covers the seven remaining pages of the same three trial
  PDFs, an upright OCR comparison and all readable existing pilot source views.
  The private trial directory owns exact scope and progress. The user then
  explicitly approved Bibliothek's two remaining objects/eight PDF pages as
  the first whole-unit expansion. Completed: all 51 readable pilot views and
  Bibliothek's four selected files (12 PDF pages and one JPEG). The combined
  91-task run has 53 text-bearing, 36 no-text partial and two deliberate synthetic
  failure results. Other units are outside that run. The private
  [follow-up report](/mnt/volume/iss-archive-work/positioned-extraction-trial-20261116/followup-report.md)
  records coverage/quality limits and a measured 6-second snapshot-read cost;
  address repeated mount/receipt-read overhead before materially larger search.
  The 17 November continuation reuses this bounded corpus in a 138-receipt
  text/visual index, adds 21 visual extractions and evaluates four scanned pages.
  It proves cross-reference flow, not collection completeness. Original run
  summaries remain unchanged; the handoff links current selections and limits.
- [ ] **6 — Consolidate bulky storage after qualification.** Resolve the audited
  consumers, stop writers, make consistent database copies, preserve journals/
  WAL correctly and retain verified recovery material. Switch configuration to
  real destination paths; verify search, citations and resume. Recreate runtimes
  from specifications when moving them. No two active writable authorities and
  no permanent compatibility symlinks. Cleanup/deletion remains separately scoped.
  The bounded work-volume probe passed process lock exclusion and atomic replace/
  sync smoke checks, but requested 0700/0600 modes became 0755. Private runtime
  therefore stays on compatible home storage; database migration was not tested
  or attempted after this failed prerequisite. The probe is not durability proof.
- [ ] **7 — Prove portability beyond this NTFS workstation.** On another compatible
  location/machine, rebuild environments and projections, remap roots without
  Python edits, open exact page regions and reproduce retained review history.
  Verify source guards and permission failures. Filesystem-specific behavior
  belongs in deployment configuration, never essential object identity or data
  semantics. NTFS quirks must not become a permanent blocker. Consolidate the
  remaining home-resident components when compatible storage is ready.

Continue through unfinished TODO items in bounded slices, without an obligation
to stop after every small slice. Follow the user's end-to-end preference once implementation
is requested, within the agreed scope and existing archive protections. This
checkpoint includes the stage-3/4 bounded extraction/workbench trial and the
user-confirmed source-word highlight. Revisit concrete
storage/dependency/migration scope when its audit supplies the needed facts;
do not infer approval for reformatting, archive relocation or destructive cleanup.

The earlier five delivery stages below describe the original pilot programme;
the current TODO above governs the next extraction/consolidation phase.

## Web-assisted multimodal hypothesis assessment

Planned on 17 November 2026 for preparation before curator review. Extend the
existing source/page/region contracts and case/citation interface. Start with a
few contrasting existing cases, positioned OCR and retained visual leads; agree
the exact selection before processing. A new vector database, service or general
agent framework is not needed for the first demonstration.

### Subject profiles and implementation order

On 17 November the user authorized adding these profiles and proceeding with
implementation. Reuse the existing `Archive-tools/research_routes.py` subject
routing as their owner; a source image may have several profiles and each region
retains its original page/context. Profile selection is research guidance, not
classification or a new archival object/identity.

| Profile | Extract and retain | Compare against | Limits |
| --- | --- | --- | --- |
| Buildings | Signs, numbers, facade/roof details, neighbours; scene and detail regions | Whole scenes and detail crops, with documented place/period | Similar architecture and repetitive windows are not identity; failed geometry is inconclusive |
| People | Person/face regions, group position, literal captions, names, event/workplace text | Explicit caption-position mappings and documentary records | Detection supplies no name; no biometric identification or face-based identity matching; unordered names remain unassigned |
| Equipment | Whole apparatus plus plates, controls, connectors and assemblies | Manuals, catalogues, technical photos and reports | Keep maker, model, serial and inventory numbers separate; a component plate may not describe the whole machine |
| Tubes | Literal markings, logos, base/pins, envelope and visible internals | Documented type dictionaries, datasheets and identified references | Preserve uncertain OCR and alternative code readings; type is not specimen identity; dimensions need a scale |

Implement source-bound regions and explicit caption links first, then local
profile-guided evidence packets using existing extraction receipts. Captions
linked by proximity remain tentative; naming a depicted person requires explicit
documentary support and curator review. Retain OCR variants separately and never
silently normalize ambiguous tube codes. The first slice uses selected/manual
regions; automatic person/face or layout detection is a later measured adapter.

Reuse the existing OpenCLIP `ViT-B-32` / `laion2b_s34b_b79k` and SIFT + RANSAC
implementations. Test PaddleOCR only against a representative Tesseract baseline;
test PyMuPDF when embedded-image/position extraction is needed (a scanned page
may still require layout segmentation). Test DINOv2-small only if the retrieval
comparison demonstrates missed useful matches. Check CPU compatibility, memory,
licensing and source-region mapping before adding a dependency/model. No model
installation or bulk run follows automatically from this plan.

The approved first implementation is local packet assembly, explicit region and
caption provenance, crop OCR with source coordinates, profile-specific guidance,
read-only pilot presentation and synthetic acceptance. Use available existing
pilot evidence for bounded validation; unavailable protected sources remain
unavailable. External searches/submission and automatic detectors are subsequent
slices with the existing source-use and external-processing boundaries intact.

The user subsequently paused tube OCR and selected buildings. The bounded building
slice retains the end-to-end purpose: extract scene/detail/sign evidence, retrieve
local photographs and document/newspaper passages, link them with explicit source
and page/region references, then prepare a multimodal OpenAI assessment draft for
curator review. Local units come first; published copies are not independent
corroboration. Record search coverage, unreadable/unindexed material and conflicting
clues. The first building demonstration uses manually selected details, existing
OCR/PDF text and SIFT/RANSAC, with quoted textual and visual cross-references in the
existing packet. Prompt preparation is local; external submission, automatic
building segmentation and collection-wide retrieval remain separate work.

The user then confirmed the wider objective: locally process applicable archived
representations, maintain text/visual/identifier indexes connected by shared
source/page/region references, and make cross-references useful independently of
OpenAI. Multimodal assessment consumes selected retrieved evidence. Keep extractor,
layout, retrieval and assessment implementations in separate modules; the runner
coordinates them without becoming another large research script.

The approved next bounded slice integrates existing OpenCLIP into the shared page
runner, extracts PDF raster placements and tentative nearby captions with Poppler,
and builds a readable local text/visual index over selected receipts. It introduces
no database or new service. Whole-page scans remain unsegmented coverage gaps;
automatic person/building-detail detectors and archive-wide indexing remain later
measured work. Reuse pinned existing model/dependency choices and record actual
runtime qualification. Candidate relationships, scores and geometry remain
unadopted; high similarity cannot distinguish same exposure from related views.

On 17 November the user authorized continuing the plan end to end until an
intervention is necessary. The local continuation adds a separate scanned-page
layout adapter, source-bound caption bands, reuse of selected OCR receipts,
measured expansion of the local index, retained external-reference provenance,
and bounded API transport/response validation. No new model, database or service
is required for these slices. The existing selection and failed/partial readings
remain visible; the private handoff owns exact run plans and evaluation results.
A coarse development check found eight illustration targets and nine extra
candidates on four scanned pages. This is a demonstrated quality limit, not
permission for unattended collection-wide acceptance. The user subsequently
approved the selected seven archive image views and two bounded assessments,
and requested the existing ChatGPT Pro account route. The optional Codex adapter
uses saved ChatGPT authentication, identical prepared images and source text,
strict output validation and retained native CLI results. External-reference
image 7 remains omitted; its selected text/context is included in the enriched
comparison. This approval does not extend to other material or bulk submissions.
API hard token caps and `store=false` are not CLI settings; the adapter records
that distinction. No accession, deletion or storage reformatting is authorized.

On 17 November the user additionally authorized general automatic text-region
discovery and coverage reporting after a missed tube marking. Implement a separate
detector over the whole view plus overlapping detail tiles, retain detections
without readable OCR, and record all bounded orientation/polarity recognition
variants. Report missing/failed passes, unreadable/conflicting regions and limits;
no detections is not proof of no text. Use the existing OpenCV environment and
an explicitly fingerprinted PP-OCRv3 detector model if real inference qualifies;
this is a PaddleOCR-model detection/Tesseract-recognition comparison, not a claim
to run the complete PaddleOCR SDK. Preserve raw images, manual regions and notes.
Evaluate region discovery separately from identifier reading on varied synthetic
and selected real examples, keeping development and held-out examples distinct.
No full-archive batch or external submission is authorized by this slice.

The bounded implementation now uses OpenCV DNN with a fingerprinted PP-OCRv3
detector, separate Tesseract crop readings and packet coverage/candidate display.
Seven selected real/synthetic scans completed; both annotated tube markings were
located without supplied crops, but readings conflict. The held-out synthetic
rotated marking was found with one extra fragment; the blank control had no
detections. These are diagnostic results, not collection accuracy. Full PaddleOCR
recognizer comparison, curator-labelled evaluation, general layout/person adapters
and external research remain unimplemented or unqualified. The private current
handoff links the exact trial and evaluation report.

Current sequencing: record observed findings and human-confirmed transcriptions
with their exact source crop, raw alternatives and still-tentative identifier
meaning; do not overwrite machine receipts or retroactively change frozen scores.
Prioritize a bounded extractor-quality pass before expanding RAG: recognition on
varied labelled markings, duplicate/fragment handling and explicit failure coverage,
then PDF figure/caption and identifying-detail extraction. Keep local retrieval as
an end-to-end check using these outputs. Resume broader RAG once a small agreed
evaluation demonstrates usable source-bound evidence and visible uncertainty;
finishing every possible extractor is not a prerequisite. New models remain
conditional on measured need and workstation compatibility.

For tubes, the immediate domain-specific gap is historical manufacturer marking
knowledge. Collect source-cited OSW/HF/WF type, production-date, batch and specimen
numbering rules by tube family and documented period. Catalogue publication dates
are evidence dates, not automatically scheme start/end dates. Retain edition
disagreements and unknown serial-allocation rules. Prioritize complete short type
inscriptions near logos and long number lines, including prefixes/suffixes, with
source coordinates and independent OCR variants. Never use alphabet-only decoding
to manufacture a desired prefix. Construct retrieval alternatives from maker/logo,
full candidate type and separately observed number; add date constraints only
when supported. Treat uniqueness as a hypothesis until factory allocation/reset
rules establish its scope. The private trial now records initial manufacturer
catalogue references and a focused short-marking OCR diagnostic; historical
serial decoding and automatic rule-based ranking remain pending.

Use the accessioned TFA photographs as the first local tube-reference corpus.
Select a bounded, varied sample by catalogue type, maker and photograph date;
inspect whole objects and native-resolution marking regions from their selected
Masters. Keep catalogue identifications, visible transcriptions and OCR guesses
separate. Photograph dates do not establish manufacture dates. Distinguish same
photograph, same tube type and same physical specimen; repeated images count as
one source. Use verified examples to evaluate short-type and long-number reading
before expanding retrieval. A catalogue keyword shortlist is neither a confirmed
tube count nor authorization for full-unit processing.

The next recognition comparison must cover Cyrillic as well as Latin markings,
whole-line context versus detected fragments, and separate logo/detail comparison.
Keep raw recognition independent from catalogue-informed candidate ranking; show
catalogue-supported type hypotheses even when OCR is incomplete. Human-read
numbers and prefixes remain source-bound observations, never retroactive machine
successes. Test contrast, border and line-mode variants before adopting them;
compare a second compatible recognizer on fixed labelled crops if failures remain.
Measure exact identifier accuracy and confident errors, not non-empty output.

The bounded comparison now supports explicit complete-line quadrilaterals,
fingerprinted Russian/English Tesseract models and an independent local OpenCV
CRNN recognizer for Latin/digits. Model files and licenses stay in existing
private work storage; no extra Python runtime is required. Manual line selection
is labelled as such and does not establish automatic grouping coverage. CRNN
cannot confirm Cyrillic. The private handoff records measured outcomes and
remaining unresolved readings; implementation is not recognition acceptance.

For unclear markings, compare a bounded set of reproducible image preparations:
unaltered source crop; levels/gamma adjustment; local contrast normalization;
and mild sharpening as a separate optional variant. Correct line orientation
and preserve context before enhancement. Derive each variant from the same
native-resolution source crop, avoiding successive destructive transformations.
Keep the original alongside every variant and record parameters, source mapping
and OCR output. Check for clipped strokes, amplified grain and sharpening halos;
reject invented or obscured character detail. Enhancement is a reading aid, not
new evidence. Select recipes by exact readings on varied labelled crops, not by
visual crispness or success on one already-known identifier. Optional bounded
enhancement recipes and retained preview/reading comparison are implemented;
the private handoff owns trial results. Collection-wide benefit remains
unqualified; the earlier standalone contrast retry did not resolve the numeric
example. Original-only preparation remains the default.

1. **Retrieve local evidence.** Assemble the research question, competing
   hypotheses, original/reverse images, native text, separate OCR variants,
   catalogue descriptions and related page/visual hits. Retain source bindings,
   page/region coordinates, transforms, uncertainties and missing coverage.
   Search the relevant archive-unit catalogues, selected Masters, related images,
   reports, manuals and existing indexes first. The user identifies the WF
   museum-digital records as publications of this local archive: resolve those
   hits back to local records, preserving their publication links and lineage.
   Do not re-retrieve them as independent external corroboration. Distinguish
   material not yet indexed/searched from material established as absent.
2. **Find external candidates.** Search distinctive inscriptions, names, product
   numbers and visual clues, using the existing visual-reference registry where
   applicable. Use reverse-image/visual-similarity search where available and
   permitted; disclose unsupported search methods. Seek contradictions and
   alternative explanations as well as apparent matches. Target specific gaps
   remaining after bounded local retrieval, such as missing factory marking
   rules or independently sourced technical documentation. Consult our public
   museum-digital records only for publication discrepancies, unavailable local
   counterparts or an explicit public-record task; replicated metadata and images
   remain the same underlying source wherever hosted.
3. **Inspect and compare.** Open actual candidate images and their source records;
   compare visible details with the local image. Retain the URL, access date,
   attribution, relevant source passage, image locator and available reuse terms.
   Search snippets and inaccessible images remain unverified leads. Distinguish
   same exposure, building, equipment type, physical specimen and event; assess
   photograph date separately from event/publication dates.
4. **Build a bounded evidence packet.** Select relevant images, legible detail
   crops with page context, text and competing hypotheses. Preserve contrary
   evidence, duplicates/lineage, omitted material and access failures. OCR and its
   source image are one underlying source; copied captions and republications
   are not independent corroboration. Label earlier AI claims as hypotheses.
   Check exact input bindings and external-processing permission before sending.
5. **Request a multimodal assessment.** Supply the permitted packet to an OpenAI
   vision-capable model. Require evidence references for each claim, supporting
   and contradicting observations, unresolved alternatives, limitations and the
   next discriminating research question. Treat retrieved content as evidence,
   never executable instructions. Validate returned citations against the packet;
   unsupported citations/claims cannot be presented as supported findings.
6. **Retain and present proposals.** Save readable input/output records, exact
   evidence selection, model and prompt/settings versions, usage/cost, failures
   and completion status in approved private work locations. Show the assessment
   through existing case/citation views without overwriting reviewer wording or
   history. Allow at most one bounded follow-up search for a specific missing
   clue, within an agreed request/cost limit; the earlier model answer remains a
   hypothesis and never becomes independent evidence.

Acceptance must demonstrate exact source-to-citation navigation, contradictory
and duplicate evidence handling, unavailable-image reporting, rejection of
unsupported citations, preserved reviewer records and explicit unadopted output.
Compare the local-only and web-enriched assessments on the same selected cases;
record evidence coverage and resource use separately from accuracy. Accuracy
evaluation needs independent curator answers captured before model disclosure;
model agreement or self-reported confidence is not ground truth.

The later implementation authorization above covers the stated local first slice;
it does not authorize external submission.
The pilot's existing real-material sharing restrictions remain in force until
permission for selected images/text and the intended external API processing is
explicitly established. Confirm account data controls and permitted external
image use before a live call. No bulk upload, automatic metadata adoption, Master
reselection, accession, publication or new service follows from this step.

## Technical references for implementation planning

- [Tesseract output formats](https://github.com/tesseract-ocr/tesseract/blob/main/doc/tesseract.1.asc)
- [ALTO layout and word confidence](https://www.loc.gov/standards/alto/techcenter/layout.html)
- [NTFS-3G permissions and mount behavior](https://github.com/tuxera/ntfs-3g/wiki/Manual)
- [SQLite locking and corruption boundaries](https://www.sqlite.org/howtocorrupt.html)
- [SQLite consistent backup API](https://www.sqlite.org/backup.html)
- [Python environments must be recreated on relocation](https://docs.python.org/3/library/venv.html)

## Earlier pilot proposal and delivery history

Status: Revised after the consolidated WF proposal and its review. The user
authorised this plan update, a handoff reference, and a local project home at
`/mnt/volume/archive-system`. Application implementation, dependency installation,
runtime migration, and changes to catalogue authority remained proposed at that
stage. On 15 November the user approved the scoped exception and first synthetic
setup; local login and read-only comparison were implemented and verified. The
subsequent "ok next" authorizes bounded synthetic assertion review/correction,
rebuild recovery and unadopted export, now implemented without new schema or
dependencies. The subsequent test-only-view correction loads the existing 16 real
evidence cases for private local inspection: 51 prepared previews and one
explicitly unreadable TIFF. All real observations remain unreviewed; the default
homepage shows them and keeps the demonstration separate. Case-scoped history
uses the existing readable authority and single projection table. Real exports
and mixed portable packages are refused. Curator outcomes, external sharing
permissions and the 24-case benchmark freeze remain pending.
The next bounded slice adds citation-region navigation and protected source-page
links for the existing synthetic fixtures, with snapshot checks and 30 passing
application tests at that checkpoint. The end-to-end continuation now adds
orthogonal page rotation, optional source-bound OCR, explicit unavailable-reference
notes, read-only case protection and a synthetic portable package. All 36 then-current
application tests pass; isolated root remapping, fresh-account recovery, actual
OCR regeneration, browser review/correction/export and restart are verified.

The next existing-tool slice was subsequently authorised and implemented locally:
the [tool map](archive-application-structure.md) records maintained consumers and
the observed runtime, and the [synthetic mixed-source example](archive-research-record-example.md)
defines citation/review/correction/export semantics plus a 24-case pilot scope.
The proposal snapshot/revision race was reproduced deterministically and fixed;
the initial and OCR importers now join the existing workbench lock. This narrow
fix adds no schema or framework and was not deployed in that slice. At the
15 November inspection the old workbench was stopped; it was not restarted.
The next preparation slice produced a concrete 24-case candidate list and the
[scoped architecture/setup proposal](archive-research-pilot-scope.md). Local
photographs, book relationships, report excerpts and one patent were inspected
under the restored read-only source mount. The private curator worksheet now
distinguishes fifteen directly reviewable evidence cases, one partial comparison
with an unreadable source, and eight synthetic access/correction exercises now
covered by bounded technical checks. Permissions,
curator labels and final selection freeze remain pending. The architecture
exception is approved and applied for the isolated pilot, now including the
explicit local-use selection described above. Its first
slice passed 13 application tests, 58 workbench tests, storage checks and browser
acceptance across a stop/restart. The following review slice passes 23 application
tests covering correction retention, stale forms/sources, authority/projection
failures, authenticated writes, lock exclusion and export/rebuild agreement.
This is not a completed real-pilot readiness
claim. The current private handoff links the evidence and
worksheet; this plan does not duplicate private records.

Validation: 58 workbench/import tests and 200 related offline consumer/research
tests passed. The example's five JSON blocks, citation links, seven assertion
revisions and current export selection were checked. Live review input,
decisions, CSV and proposal sidecar remained byte-identical across this slice.
HTTP/runtime inspection is not browser acceptance of a deployed fix.

## Objective and recommendation

Build on the maintained archive tools to investigate relationships across WF
photographs, multipage reports, patents, drawings, and physical objects. Make
those tools easier to understand, extend, and operate through one application.
Retain working processing code, existing identifiers, readable decisions, and
the established accession path.

Evaluate Django as the application foundation, using its admin for record
browsing and a custom view for photographic comparison. Start with SQLite and
existing command-line processing. Treat FiftyOne as an optional alternative for
visual exploration, subject to a separate comparison.

The first complete slice uses 20–30 isolated review cases, including one small
mixed-source case around a product family or research programme:

> Browse proposals, compare a photograph with a cited report page and patent
> reference, save a supported connection, a rejected candidate and an unresolved
> question, then restart, rebuild the index, and export the reviewed proposals.

Existing OCR or manual transcription is sufficient for this demonstration.
Saving a reviewed proposal remains distinct from catalogue adoption or accession.
The research application owns evidence and proposal review; the existing archive
workflow retains accession and catalogue authority. This is an explicit scope
extension beyond the present reconciliation screen, not only a framework swap.

## One local project home

The current project home is `/home/vladimir/archive-local`; its README owns
locations. The maintained source checkout is `source/`, and the sole private
checkpoint is `/home/vladimir/archive-local/HANDOFF.md`. This plan remains in the
source checkout's canonical docs directory; do not maintain a second editable
plan copy. Future experiments use only the paths approved for their scope.

The initial `/mnt/volume/archive-system` setup repository, its `workspace.toml`,
supplied design references and ignored local experiments are retained history.
That initial setup used `/home/vladimir/archive` as source and
`/mnt/volume/iss-archive-work/HANDOFF.md` as its checkpoint. Those are historical
paths, not current launch instructions. Architectural DOCX inputs remain design
evidence, not governing policy. Private indexes, records and credentials remain
outside the source Git repository.

The initial setup recorded a device name for `/mnt/volume` using NTFS/FUSE;
that name is historical. Verify the currently mounted filesystem and device.
Do not assume it is suitable for a moved virtual environment or application
database. Record environment locations and validate filesystem locking, atomic
replacement, and executable requirements before deployment. Recreate Python
environments from dependency specifications on a compatible filesystem rather
than copying installed environments. The workspace itself is workstation
configuration; it is not the portable research project format described below.

## Evidence and current structure

The source review found a usable shared foundation rather than a need to rewrite
every script:

| Existing component | Role to preserve | Consolidation issue |
| --- | --- | --- |
| `Archive-tools/archive_common.py` | Protected paths, locking, atomic file replacement, preview helpers | Keep common safeguards independent of the web framework. |
| `Archive-tools/catalogues.py` | Read collection formats into a common photo-Master representation | Make supported formats and unsupported representations explicit. |
| `Archive-tools/search.py`, matching helpers | Prepare reconciliation evidence | Keep expensive processing separate from HTTP requests. |
| `Archive-tools/workbench.py` and its HTML/JS/CSS | Candidate comparison, decisions, reviewed proposals | Separate decision/storage logic from HTTP handling without changing semantics. |
| `Archive-tools/visual_research.py`, `visual_evaluation.py` | Embeddings, neighbours, controlled evaluation | Research currently imports `Workbench`; expose a smaller shared read interface. |
| `Archive-tools/reference_batch.py` | Bounded external research and evidence assessment | The 2,122-line runner combines fetching, parsing, image preparation, model calls, and orchestration. |
| `Archive-tools/research_routes.py` and source registry | Subject routing and source configuration | The workstation default is `archive-local/berlin-photo-reference-sources.json`; `--registry` selects an explicit alternative. Preserve existing frozen-plan hashes. |
| `tools/archive-preservation/`, `work-area/` | Shared compatibility tools and collection-specific history | Distinguish maintained entry points from completed jobs before selecting anything for reuse. |

The root README and maintained application structure document now identify the
current v12 runner and its consumers. Historical scripts remain evidence; their
presence does not imply that they need to become application features.

Source commits do not deploy the runtime copies. Determine actual process paths
and consumers before changing a live entry point.

The [six-photo comparison](../reference-v12-building-benchmark-20261114.md)
found more reference retrieval without improved identification in its three
independently checked cases. Framework adoption should be judged on usability
and maintainability; recognition quality needs a separate evaluation.

The snapshot/revision race is now reproduced in synthetic tests and fixed in
source: displayed proposal content and its token use the same captured bytes.
An intervening import makes that snapshot stale rather than giving its old
content a new token. Initial proposal and OCR imports now take the existing
workbench lock across validation/read/merge/replacement, as the reference runner
already did. Tests also cover missing/malformed sidecars replaced during capture,
stale-save rejection and independent lock exclusion. No live incident was
reproduced and no real proposal file was imported. Current proposal saves still
replace the stored fields; they do not provide a complete correction history.

## Shared model and a concrete example

Map these concepts to existing formats before introducing schemas: collection
item, digital representation, document part/location, entity, source record,
observation, claim, evidence, review decision, and processing run. They need not
be ten tables or a graph database.

Distinguish an individual apparatus from a product type, and an invention or
technology from a test event. A type match does not identify a physical object;
a patent does not prove manufacture, installation, or use at WF. Preserve source
roles rather than flattening commissioners, manufacturers, and photographers
into an unexplained creator field.

Before implementing models, specify one clearly synthetic readable example:

- Photograph A may depict apparatus type B.
- Report C, page 12, supports that type identification and documents a test.
- Patent D describes a potentially related invention.
- A reviewer accepts the type identification, rejects one alternative, and
  leaves the apparatus-to-patent relationship unresolved.

Show the exact records before review, after review, after a correction, and in
an unadopted research export. Define relationship direction, scope, field value,
evidence locator, uncertainty, reviewer, time, revision, and correction linkage.
Reuse existing keys; specify any missing local-key requirement explicitly.
This example is a design deliverable, not a claim about actual WF holdings.

### Documents and durable citations

A report or patent has its own identity and description, separate from its scans,
PDF, page images, OCR, and external catalogue records. Keep page sequence and
printed page labels. Bind citations to a particular representation and page,
figure, region, or passage. OCR offsets alone are insufficient: a citation must
still resolve to source evidence after OCR is regenerated.

Record republication lineage. A local report and its catalogue description are
related records; museum-digital, DDB, and Europeana republications do not become
independent confirmations merely because their URLs differ.

## Proposed application boundaries

```text
Django admin and comparison view        Existing command-line entry points
                  \                    /
                   Shared archive operations
                     /               \
       Readable evidence and decisions    Research and collection adapters
                     |
          Rebuildable SQLite browsing index
```

Shared operations must not import Django views, admin classes, or HTTP handlers.
Both interfaces call the same validation and save functions. Adapters return
evidence or proposals; they do not choose Masters or adopt catalogue metadata.

### Django interface

Use the admin where it reduces custom interface work:

- Search and filter review cases and proposals.
- Inspect source records, evidence references, existing run receipts, and errors.
- Link directly to the relevant comparison view.

Use a custom Django view for A/B comparison, linked zoom/pan, page selection,
overlay/difference views, evidence context, and bounded review actions. Support
image-to-page and page-to-page comparison with exact citation navigation. Use
Collections, Review, Research runs, and Exports as the initial navigation;
technical receipts remain available through details. Include keyboard navigation,
save-and-next, and clear saved/stale/unadopted messages. Reuse
the current frontend where practical rather than rebuilding it inside admin
form widgets.

Initially make projected archive records read-only in the admin. Disable generic
add, delete, bulk edit, and direct field saves for these records. Any permitted
decision action goes through the shared archive operation. Use Django's built-in
authentication for the pilot operator; do not design a new role hierarchy.

### Data and authority

| Information | Durable authority | Django treatment |
| --- | --- | --- |
| Prepared object/candidate relationships | Existing prepared input and source bindings | Indexed read-only view using existing keys |
| Master and existing same/different-image decisions | Existing readable decision records | Display and bounded shared save operation |
| New documentary relationship reviews | Explicit compatible extension defined by the example | Shared save operation, never a database-only record |
| Machine-generated research | Existing proposal sidecars and evidence receipts | Read-only suggestions, with uncertainty and provenance |
| Reviewer-edited proposal | Existing decision record's proposal fields | Same scoped save semantics, still unadopted |
| Accepted archival metadata and files | Existing authorised accession/catalogue path | Read-only during this pilot |
| Search/filter index | SQLite projection of readable records | Rebuildable, never an independent decision authority |
| Operator login/session state | Django application state | Separate operational concern; not archival metadata |

Inventory the existing schemas before introducing models. Prefer small browsing
projections for cases, proposals, and runs; do not automatically translate every
concept in the downloaded proposal into a relational table. File, photograph,
depicted subject, claim, evidence, and decision remain distinct concepts without
requiring a new identifier system or entity graph.

Reviewed acceptance means a human endorses a scoped research assertion; it does
not mean catalogue adoption. Reviewed proposals may appear in research exports
that explicitly retain their unadopted status. Prior hypotheses remain available
as labelled context. Rejected candidates may suppress repeated suggestions while
their bindings remain current. Only adopted results enter the trusted reference
corpus through the agreed path, with lineage retained; self-reuse is never an
independent confirmation.

Specify correction/supersession records in the synthetic example and implement
only the missing compatible history needed for these decisions. Preserve earlier
values and reasons without introducing a general event-sourcing or audit system.

Preserve the existing write sequence: validate and recheck the submitted
revision/source bindings, write the authoritative decision file under the
existing lock, then rebuild derived outputs. If CSV or SQLite refresh fails,
report that the decision was saved but the projection is stale. Do not pretend
that filesystem and database writes form one atomic transaction, and do not
silently repeat the decision. Rebuild projections from readable records.

A stale index must be visibly marked or withheld from use; writes must always
validate against current authoritative records. Existing consumers continue to
reject inconsistent required projections. The browsing index must never bypass
those checks.

### Extensible functions

Begin with ordinary Python modules and an explicit registration list. A module
name/configuration entry is sufficient; no plugin marketplace, installer,
dynamic discovery framework, or general workflow engine is needed.

| Extension point | Input | Result |
| --- | --- | --- |
| Collection adapter | Explicit approved catalogue/unit path | Common representation retaining original keys, source fields, and provenance |
| Source connector | Bounded query or source record reference | Source identity/URL, original metadata, observed image references, rights when available, retrieval time, access status |
| Research method | Bound input view, evidence packet, settings | Evidence or field-specific proposals with limitations and processing details |
| Export adapter | Explicitly reviewed records and target format | Validated output for the existing authorised workflow |

Use existing schemas, stamps, and statuses where available. Specify required
fields and failure behaviour before extracting an implementation. Keep model
identity, settings, input binding, and version in cache/run records so completed
work is reused only when compatible. Retain source lineage and keep an access
failure distinct from a completed search with no useful match.

Implement only the extension boundaries needed by the first real consumers.
Shared orchestration owns budgets and outputs; each connector owns its
source-specific parsing. Research results never directly overwrite catalogue
fields.

Keep the current research runner behind its existing CLI, as a separate process.
It controls signals and child processes; do not execute long research within a
Django request. Preserve the executable identity needed to resume unfinished
runs; changed code requires a new compatible run plan. Expose a small shared
read interface instead of importing all of `Workbench` into research modules.

Processing contracts retain bound inputs, relevant settings, code/model versions,
output references, run identity, measured usage, and limitations. Distinguish
completed, empty, partial, failed, unavailable, and cancelled outcomes using
existing statuses where possible. A cache hit must establish input/settings
compatibility; a unique database row is not an idempotence guarantee.

## Portable research projects

Start with explicit configuration and named storage roots. References use a
root mapping and relative path while retaining original keys, path provenance,
and content bindings. Do not silently rewrite historical source records.
Unchanged content at a new location can be rebound through a verified mapping;
a remapped path alone cannot authorise a save against changed content. A file
checksum identifies content, not every distinct physical holding.

The project manifest records format version, referenced collections, configuration,
evidence/decision locations, and included/omitted material. Host roots, executable
locations, credentials, and disposable caches remain machine configuration.
Environment checks distinguish required capabilities from optional OCR/embedding
methods. Review must remain available when an optional processor is missing.

Prove relocation of one mixed-source case to a compatible machine before
generalising packaging. Validate citations and source bindings after relocation,
and rebuild projections. Preserve source guards: the current workstation's
Elements requirement is not waived by a portable manifest. Any alternate machine
source mapping must establish its own explicit protection rather than accepting
an arbitrary replacement path.

For a demonstration bundle include only permitted material, readable evidence,
decisions, dependency specifications, and rebuild instructions. Reject unsupported
schemas clearly. A later schema conversion needs an explicit conversion, retained
pre-conversion input, and a tested recovery procedure; it does not justify a
general archive versioning or rollback system.

## Delivery stages

### 1. Map the maintained system and establish reliable snapshots

Create a bounded inventory in the existing application structure document:
maintained entry points, dependencies, runtime paths, configuration owners,
input/output contracts, and legacy/completed tools. Check the current private
handoff and actual process paths; do not start jobs to discover them.

Select 20–30 cases covering multiple candidates, uncertain identifications,
reviewer corrections, inaccessible references, and multipage material. Include
one coherent WF example containing photographs, a report, and a patent reference.
Check actual availability and permission before committing to that selection.
Keep private selections and evidence outside Git. Define the synthetic record
example before model/schema work.

Fix the snapshot/revision race as a separate implementation slice before new
writable UI work. Derive displayed proposal content and its token from the same
captured bytes, and account for external writers. Use deterministic interleaving
tests rather than relying on timing-sensitive manual reproduction.

Exit criteria: one maintained tool map, an explicit pilot scope and record
example, and proof that an old displayed proposal cannot receive a newer token.

### 2. Define the minimal architecture exception and pilot setup

The [15 November scoped proposal](archive-research-pilot-scope.md) supplies the
exact exception text, paths, candidate dependency pins, launch contract and
remaining real-sample gaps. The exception was approved and the isolated framework
and runtime were implemented. End-to-end technical acceptance is recorded in
that scope; independent operator and real-case acceptance remain separate gates.

Prepare the exact source location, isolated work root, dependency versions,
SQLite location, loopback launch command, and intended operator access. Verify
Python/framework compatibility on the actual machine before choosing versions.

The existing `AGENTS.md` prohibits ORMs and a general management UI, and requires
approval for new dependencies, databases, directories, services, and broad
refactors. Django's ORM, admin, and authentication therefore need a narrow,
explicit architecture revision before implementation. Include document citations,
relationship review, compatible correction history, and portable research
configuration in that scope; approving only the framework would leave the
product expansion undefined. Define the exception around the pilot; retain file
authority, source protection, and accession rules.
Update the governing documents only as part of that approved revision.

Exit criteria: approved implementation scope and concrete setup. This document
is not itself that revision or an installation instruction.

### 3. Extract the smallest shared operations

Separate reading/snapshotting, revision checks, decision validation, saving, and
projection generation from the existing HTTP handler. Preserve public formats
and existing CLI behaviour. Reuse current tests and add checks only where the
new boundary creates meaningful risks.

Do not split the entire research runner in this stage. Identify fetching/parsing
as a later extraction candidate, with frozen inputs and comparison of outputs.

Exit criteria: the existing interface still works, and the extracted operations
can be used without starting its server or importing Django.

### 4. Build the complete Django pilot

Start with the synthetic record example, then approved isolated copies of the
chosen review records. Browse/filter proposals through admin, open the reused
comparison UI, and perform scoped review actions through shared operations.
Implement the minimal compatible relationship/citation/history extension proven
by the example. Keep Master decisions, proposal review, and catalogue adoption
distinct. Do not add a catalogue acceptance button or change the meaning of
`metadata_adopted`.

Introduce the explicit project configuration and named root mapping needed by
this case. Use existing OCR or manual transcription; bulk processing is not a
prerequisite. Prove image-to-page and page-to-page comparison and export with
source identity, page order, evidence locators, and unadopted status intact.

Refresh the SQLite view from readable records and demonstrate rebuilding it.
Use existing processing commands outside requests; inspect their existing
receipts without adding a job queue. Avoid concurrent old/new writers to the
same review work root during the pilot.

Exit criteria:

- The complete browse/compare/save flow works in the browser.
- Existing identifiers, corrections, evidence, and decision semantics survive.
- Readable output and indexed output agree, including after restart/rebuild.
- Stale edits and changed source bindings are rejected.
- A failed projection refresh is reported truthfully and can be repaired from
  the decision file without repeating a review action.
- Completed/read-only cases remain protected; no source or archive bytes change.
- No model calls, collection migration, or accession are required to prove it.
- A supported connection, rejected candidate, and unresolved question remain
  distinct through save, correction, restart, projection rebuild, and export.
- Citation navigation survives a new OCR output; missing/changed files and
  unavailable processing are understandable to the operator.

### 5. Evaluate and demonstrate portability before expanding

Compare the current and Django interfaces using the same cases, evidence, and
review tasks. Record task completion, review time, navigation burden, evidence
visibility, correction preservation, startup steps, and code that can actually
be retired. Keep the sample and comparison protocol fixed in advance; report
small-sample and learning-order limitations.

Have a second operator open the permitted mixed-source sample on another
compatible machine, remap storage roots without editing Python, inspect exact
citations, and reproduce an exported decision. Package only what that exercise
requires. Separate this usability/migration evaluation from recognition and
document-linking quality. A later 100–200-case research benchmark requires its
own selected labels, budgets, and grouped splits for duplicates and republications.

Adopt Django only if it provides a coherent entry point and removes duplicated
interface/storage plumbing while retaining the established behaviour. If admin
customisation recreates most screens or causes parallel data authority, reduce
the scope or stop the pilot. Retain its findings without forcing a migration.

If adopted, migrate one active workflow at a time. For each, verify its deployed
path, consumer compatibility, and user acceptance before retiring the old writer.
Preserve completed scripts and receipts. Expand connector/module interfaces only
when an actual second implementation needs them.

## Optional FiftyOne comparison

FiftyOne offers image browsing, metadata filters, similarity exploration using
precomputed embeddings, and custom operators/panels. Test whether it materially
reduces visual-interface work; it does not supply the archive's decision and
accession rules automatically.

Use the same pilot cases and existing previews/embeddings, plus an explicitly
bounded reference pool. Keep FiftyOne data a rebuildable research projection.
Start read-only; do not create another decision writer or rerun recognition just
to compare interfaces. Measure import effort, evidence visibility, comparison
usability, review time, and the custom code needed to meet archive requirements.

The inspected workstation has an AMD Phenom II without AVX. Current documented
FiftyOne MongoDB requirements and MongoDB's AVX requirement prevent a standard
supported local database setup on that CPU. A trial needs compatible hardware
or a remote database and separate dependency checks. Docker does not supply
missing CPU instructions. No installation or remote service is scheduled here.

Choose FiftyOne only if its measured benefit exceeds the extra integration and
operational burden. A Django pilot does not depend on this optional experiment.

## Deferred scope

- PostgreSQL, pgvector, Redis, Celery, and additional search infrastructure.
- Public uploads, multiuser role design, quotas, and a general management UI.
- A new entity/identifier system or database-only evidence authority.
- Automated catalogue adoption or changes to Master selection.
- Rewriting completed collection jobs or reorganising accessioned units.
- Bulk migration, deployment, new model runs, or source/archive mutation.
- Automated facial identification, cluster-wide acceptance, model fine-tuning,
  and a general project packaging/migration framework.

Reconsider infrastructure only when a measured requirement demands it, such as
sustained concurrent writes that SQLite cannot support, rather than because the
archive occupies many terabytes.

## ECHOES preparation and funded extension

The proposed contribution is one connected WF research corpus and a reusable
evidence workflow. Demonstrate one bounded technology/programme case before
promising a broader collection. Mark capabilities implemented, demonstrated, or
proposed; identify new work that funding would enable.

Inventory document types, volumes, date ranges, description quality, condition,
and digitisation status. Establish access and reuse separately for images,
document text, metadata, and external references. Identify the institutional
applicant, curator, second test operator/institution, code licence, maintainer,
and recurring-cost owner through actual commitments; do not infer partners or
ISS participation. Agree contributor roles and compensation explicitly.

The ECHOES programme page checked on 14 November 2026 lists Call 3 publication on
31 January 2027, 18 projects, and EUR 60,000 per project. This is not a submission
deadline or a guarantee of eligibility. The final guide must settle scope,
eligible costs, scoring, integration, and submission stages. Do not delay the
small local proof waiting for a speculative Cloud architecture.

Investigate ECHOES HDTO alongside any relevant CIDOC CRM, CRMinf, or Web Annotation
mapping. Specify a concrete crosswalk against the required target profile before
claiming interoperability. Existing export syntax validation is not semantic
certification. Budget actual integration, curation, evaluation, documentation,
and maintenance; Cloud compute and free hosting are not assumed.

After an award and agreed scope, the extension may expand the corpus, deliver
the required data/application integration, and test adoption by another
institution. That is a later work package, not part of local workspace setup.

## Verification and completion

Use contract tests for shared decision operations, source/path protection,
revision conflicts, file/projection agreement, and interrupted projection
refresh. Reuse existing fixture cases and relevant regression suites. Exercise
the actual browser comparison flow; HTTP success alone is insufficient.

Keep implementation and validation evidence with each stage. Document only
maintained commands and verified deployment paths in the tool map. The eventual
consolidation is complete when the selected workflow has one maintained entry
point, one shared decision implementation, reproducible readable records, and a
verified replacement path for the superseded interface.

## References

- [Archive instructions](../../AGENTS.md), [workflow contract](archive-preservation-architecture-adr.md), and [application structure](archive-application-structure.md).
- [Current tools](../../Archive-tools/README.md) and [current research entry point](../../README.md#current-photo-research-source).
- Supplied document: *WF research workbench architecture and migration plan*, consolidated 14 November 2026; principal input for this revision. Retained in the local project home's `references/wf_research_workbench_consolidated.docx`.
- Earlier supplied document: *Image recognition and metadata enrichment — Architecture proposal for an archive research workbench*, 14 November 2026. Retained as design history, not governing policy.
- [Django admin](https://docs.djangoproject.com/en/6.0/ref/contrib/admin/): automatic model-based administration and guidance on custom process-oriented views.
- [Django databases](https://docs.djangoproject.com/en/6.0/ref/databases/): SQLite support and concurrency limitations.
- [FiftyOne App](https://docs.voxel51.com/user_guide/app.html), [similarity](https://docs.voxel51.com/user_guide/similarity.html), and [plugins](https://docs.voxel51.com/plugins/developing_plugins.html).
- [FiftyOne database configuration](https://docs.voxel51.com/user_guide/config.html) and [MongoDB CPU requirements](https://www.mongodb.com/docs/v7.0/administration/production-notes/).
- [ECHOES grants programme](https://www.echoes-eccch.eu/cascading-grants/), [integration FAQ](https://www.echoes-eccch.eu/faq/), and [earlier data call](https://www.echoes-eccch.eu/first-call/) as preparation context, not final Call 3 conditions.

External documentation was checked during the architectural discussion on
14 November 2026. Recheck supported versions before any installation.
