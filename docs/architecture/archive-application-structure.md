**Additional support tools (16 November 2026):**
[video inventory](video-inventory.md) owns the paused census; private state is on
the work volume. [Transcription scripts](../../tools/archive-preservation/transcription/README.md)
use private data under `archive-local/transcripts` and shared whisper.cpp under
`~/.local/opt`. [Photograph research prompt](photo-metadata-research-prompt.md)
is an optional reference, not policy. These moves did not resume any job.

**Current home locations (15 November consolidation):** source is
`/home/vladimir/archive-local/source`, private pilot is
`/home/vladimir/archive-local/research-pilot`, and home indexes are under
`/home/vladimir/archive-local/indexes`. Earlier checkpoint paths below are
historical. Use the canonical consolidation plan and private handoff for current
operations; old home aliases have been retired.

# Archive Tool Structure

## Status

Implementation map for the existing Python tools. The archive contract is
`docs/architecture/archive-preservation-architecture-adr.md`; this document
does not define a second package, catalog, identity, or workflow model.

`/home/vladimir/archive-local/source` is the source-code and documentation repository for this
workstation. Unit scripts are versioned under `work-area/`, mirroring their
paths under `/mnt/volume/iss-archive-work`. The hub README and private handoff own
current runtime locations; a commit does not deploy or execute them.
Use this file to route harnesses. Do not infer current policy from
old downloaded ADRs, local memories, historical handoffs, or completed run
artifacts.

## Dependency Direction

`src/archive_research/` is the new stage-2 standard-library boundary for explicit
sources, pages, recipes, results and machine roots. Its
[API guide](../../src/archive_research/README.md) owns use and limits.
`archive_research.visual.preview` wraps an explicitly supplied existing renderer;
it does not import the legacy tools, Django or numerical libraries. Existing
photo CLIs do not depend on this package yet, and retain their original outputs.
The bounded positioned-extraction slice adds separate `units`, `preparation`,
`pdf_text`, `ocr`, `geometry`, `runner` and `storage` modules. Existing catalogue
and manifest mappings supply selected members; no Master selection is introduced.
One manual per-page runner writes readable derived receipts under explicit work
roots. Optional Pillow/Poppler/Tesseract are used only during processing.
The existing Django pilot reads receipts through `research_web.extraction` for
page search, source regions and existing visual leads. It adds no database/table,
daemon, review authority or automatic extraction inside HTTP requests.

This is the intended direction for small shared extractions, not a description
of all current imports. The retained `archive_core/`, `interfaces/`, and
`workflows/` directories below are legacy code, not a target package layout.

```text
interfaces -> workflows -> archive_core
```

- Interfaces translate command-line or workbench input.
- Workflows coordinate bounded operations.
- Core modules provide path safety, catalog access, metadata handling, and
  format checks.
- Core does not import workflows or interfaces; workflows do not import
  interfaces.

`archive.py` remains a compatibility façade for existing commands. New
architecture does not belong in that file.

## Maintained tools and consumers

Bounded source/runtime inspection: 14 November 2026. This map covers the shared
photo review and research path, its direct consumers, and the retained unit
harnesses. It does not certify every historical work-area script or index.
The [consolidation plan](archive-django-consolidation-plan.md) and
[synthetic record example](archive-research-record-example.md) describe research
expansion. On 15 November the approved first [Django pilot](../../Archive-tools/research_web/README.md)
added synthetic login and read-only comparison. Its source is
`Archive-tools/research_web/`; its isolated environment, readable records and
single SQLite database now live in `/home/vladimir/archive-local/research-pilot/`.
The intended pilot address is `127.0.0.1:8785`; check the private handoff and live
sockets before launch. The following slice descriptions record the 15 November
implementation sequence; the linked operating guide owns current usage.
`archive_common.content_revision` is shared by the pilot and old workbench;
no live review root is shared. The next approved slice adds bounded synthetic
assertion review/correction, rebuild recovery and unadopted JSON export. Review
files remain authoritative; generic research admin remains read-only. No real
sample or new schema/dependency is introduced.
The next citation-navigation slice adds a prepared-region viewer and protected
bound-source PDF/PNG reads, with snapshot checks on generated links. It uses
the existing three fixtures; it does not process PDFs or OCR in requests.
End-to-end acceptance additionally covers orthogonal rotation, CLI-only optional
OCR receipts, explicit unavailable-reference notes and read-only cases.
`configure.py` and `bootstrap package` provide synthetic root remapping and
secret-free packaging; isolated acceptance stays inside the approved runtime cache.

| Source in `Archive-tools/` | Inputs/configuration owner | Outputs and consumers | Dependencies/status |
| --- | --- | --- | --- |
| `workbench.py`, `workbench.html/.js/.css` | Explicit `--work-root`, actor/port; `workbench-input.json` owns candidates, source stamps, read roots, completion receipts and review scope | Browser snapshots; `decisions.jsonl` is authoritative for human saves, `dispositions.csv` is derived; proposal sidecar remains unadopted | Python, Pillow for images, Poppler for PDFs; only observed running archive process |
| `search.py` | Explicit source/archive/prior-review paths, read-only visual index, optional Czkawka/SIFT profiles | Fresh prepared input, dispositions, summaries and CSV; calls `Workbench.rebuild_csv()` | `catalogues`, `consolidation`, matching helpers, ExifTool, configured Czkawka/optional SIFT; not running at inspection |
| `avantmedia_derivatives.py` | Prior prepared review plus explicitly selected evidence and output root | New preparation carrying compatible prior decisions; imports `Workbench`, its objects/candidates, decisions and CSV builder | Collection-specific preparation adapter; not a live decision writer; do not rerun completed preparation |
| `visual_research.py`, `visual_evaluation.py` | Explicit review/unit/work roots and checked view recipes | Research manifest, previews, embeddings, neighbour/evaluation reports; proposals only | Research imports `Workbench` for objects/candidates/decision revision; evaluation imports research helpers; optional numerical/embedding environment |
| `import_model_proposals.py` | Explicit work/research roots; completed 111-image harvest, manifest, schema and preview bindings | Creates `model-proposals.json` only with `--apply`, refuses overwrite; workbench/research consume it | `jsonschema`, `Workbench`; bounded existing-set importer, not a generic ingest interface |
| `photo_clue_research.py` | Explicit existing research outputs and source-bound manifest; CLI OCR settings | OCR receipts, clue/search packets and comparison reports in work area | Pillow/Tesseract and protected-source checks; packets do not prove completed external searches |
| `import_ocr_research.py` | Explicit review/research directories; existing dated OCR receipts, clues and findings | Merges only OCR evidence into the current proposal sidecar; preserves model results and other attached research | Standard library and existing shared file helpers; specific to the retained OCR batches |
| `reference_batch.py` (v12) | Work/research roots, prefix, registry and prepare-time settings frozen into plan | Receipts, downloaded references, measured usage, field-specific assessments; normal runs attach `reference_check`, named pilots stay isolated | `jsonschema`, Pillow, configured Codex executable, network and optional Poppler; separate CLI process, no observed worker |
| `research_routes.py`, `berlin-photo-reference-sources.json` | Shared route contract; explicit registry is search configuration | Workbench grouping, reviewer overrides, source preferences and frozen research routing | Registry is not catalogue authority; no new configuration registry introduced |
| `archive_common.py`, `catalogues.py`, matching/consolidation helpers | Explicit paths and existing collection formats | Protected reads, locks, atomic replacement, candidate/evidence adapters | Shared helpers; no HTTP/framework imports in `archive_common.py` |

`tools/archive-preservation/reconciliation_workbench.py` remains a separate,
retained shared interface, documented in its adjacent Markdown file. It is not
the currently observed photo workbench. `md_export_discovery.py`,
`tiff_metadata.py`, and `tiff_accession.py` remain bounded discovery/accession
helpers for their documented callers. Unit tools execute only under their unit
handoff and explicit task; copying their source here did not migrate services.

### Extraction and support inventory — 16 November 2026

The user-authorized tools audit extends the earlier photo-only map. The complete
dated file inventory, unit mapping schemas, consumer references, runtime counts
and verification limits remain in the private
[audit report](/mnt/volume/iss-archive-work/tools-inventory-20261116.md) and its
adjacent JSON. The ordered next work remains in the consolidation plan.
The earlier pilot slice descriptions above are delivery history: the real-case
pilot and physical-object reference slice have already been delivered.

| Owning source | Role and consumers | Boundary to preserve |
| --- | --- | --- |
| `Archive-tools/catalogues.py`, `search.py`, matching/consolidation helpers | Photo catalogue adapters and candidate preparation, consumed by review and visual research | TIFF/JPEG scope; multiple selected photo members require an ordered-representation adapter. Do not silently broaden the current photo contract. |
| `Archive-tools/visual_research.py`, `visual_evaluation.py` | Prepared-view OpenCLIP embeddings and research evaluation | Checked source/preview recipes and proposal-only results; optional numerical dependencies stay outside Django. Candidate freeze currently requires one member. |
| `Archive-tools/photo_clue_research.py` | First-frame source OCR, reduced overview and native crops; text/TSV and partial-result receipts | Existing positioned-word precedent, not all-page OCR. Processed-image coordinates need explicit crop/scale/rotation lineage before shared page search. |
| `Archive-tools/research_web/` | Bound evidence, prepared views, readable review history and Django browsing projection | Its optional CLI OCR reads a prepared PNG and emits plain English text. No rendering/OCR in HTTP requests. Named real-source roots remain restricted to approved workstation paths. |
| `work-area/document-index/document_index.py` | Document discovery SQLite/FTS, used by Bibliothek, collection and TFA/WF preparation/review harnesses | Preserve known-unit skipping and bounded PDF headers. Content-hash-only cache reuse cannot safely implement different page coverage or recipes. Source and runtime copies matched during the audit. |
| `tools/archive-preservation/visual_index.py`, `visual_indexer.py`, `visual_candidate_search.py` | aHash/dHash/pHash index and ranked candidates, also read by unit preparation | Separate from OpenCLIP vectors; first-image normalization and path-derived occurrence IDs are not archival object authority. Adapt existing results before changing internals. |
| `tools/archive-preservation/kurt_schwarz_identity_provenance.py` and related provenance consumers | Read the retained restoration index and write bounded evidence for their existing workflows | Preserve schema/path consumers. `source_registry.py` and `jobs/provenance_restoration/` are retained compatibility architecture, not the shared research design. |
| `tools/archive-preservation/video_inventory.py` | Paused AV discovery, technical metadata, rights/project review and optional WordPress comparison | Separate AV workflow; no implied rescan, source merge or publication. See [video inventory](video-inventory.md). |
| `tools/archive-preservation/transcription/` | Manual whisper.cpp and Python Whisper batches | Distinct engines and completion markers; preserve model provenance and existing outputs. See its [README](../../tools/archive-preservation/transcription/README.md). |
| `work-area/` unit scripts; accession, TIFF/metadata and recovery helpers | Unit-specific preparation, workbenches and completed output evidence | Route through current receipts. Listing a script does not authorize running an accession, drain, deletion or publication command. |

Two operational ownership gaps remain outside source: the visual-index `bin/`
launch/shutdown wrappers and the restoration-index builders. The hub guide owns
their locations. They were not executed or copied into source. Review ownership,
old root assumptions and exact consumers before maintaining or relocating them.

The delivered stage-2 boundary is source/member/page/result plus explicit named
roots and one opt-in existing-preview adapter. Preserve historical identities: photo IDs
include canonical unit paths; visual IDs include root paths and file stamps;
document IDs use source-root/relative-path strings. Do not regenerate them when
adding portable locators. Unit adapters must join established ordered-member
manifests instead of inventing order from filenames or choosing Masters by format.

Environment metadata inspection is not execution acceptance. Keep the three-package
pilot, photo/SIFT environment, visual embedding environment and system utilities
separate. Use the existing pinned requirements and CPU constraints; no dependency
installation or heavy-library import was performed. The volume has capacity but
has not been qualified for private runtime/SQLite migration by this read-only audit.

### Runtime and environment boundary

The 14 November observation recorded a process executing
`/home/vladimir/archive/Archive-tools/workbench.py` with
`/mnt/volume/iss-archive-work/archive-tools-venv/bin/python`, using
`/mnt/volume/iss-archive-work/avantmedia-master-review-20261103`, actor Vladimir,
and loopback port 8783. `/api/overview` returned 1,083 objects, 111 proposals and
an agreeing CSV projection. This is HTTP/data verification, not browser acceptance.
These are historical observations, not current startup paths or service claims.
Private process details and continuation belong in the sole checkpoint at
`/home/vladimir/archive-local/HANDOFF.md`; follow [continuity](archive-continuity.md).

The observed interpreter is Python 3.13.5 with Pillow. Its environment lacks
`jsonschema`; it is not a verified all-tools environment. System `python3` has
Pillow and `jsonschema` and runs the offline research/import regression suites.
The optional visual/SIFT requirements files cover their specific processors,
not the whole application. No dependency was installed during this slice.

The two inspected active review/research work directories contain no top-level
Python copies matching `Archive-tools` filenames. Historical source under
`work-area/visual-similarity/reference_batch.py` is retained older code; the
maintained research entry point is `Archive-tools/reference_batch.py`.
Other unit runtime copies were not recursively audited. No process restart or
deployment was performed: an existing Python process retains its loaded code.

### Configuration and write contracts

- Canonical source is `/home/vladimir/archive-local/source`; the former
  `/home/vladimir/Archive-tools` compatibility link is retired. The hub README
  owns locations; `/mnt/volume/archive-system` is retained earlier setup material.
- The v12 CLI defaults to `/home/vladimir/archive-local/berlin-photo-reference-sources.json`.
  Use the existing explicit `--registry` option to select the versioned copy for
  a new authorised plan. Existing frozen plans retain their recorded paths and
  hashes; do not rewrite them as part of relocation. Codex's executable
  path remains configured in `codex_image_pilot.py`.
- Source membership/bindings come from prepared input. Archive identity remains
  canonical unit path plus original record key. Human decisions live in readable
  records; the document, visual and provenance indexes are support projections.
- A proposal snapshot parses and hashes the same captured sidecar bytes. Its
  metadata token also includes the existing input/decision revision. An import
  after capture leaves a valid older snapshot whose stale save is rejected.
- `save_metadata`, initial proposal import, OCR import, and reference attachment
  use the existing `.workbench.lock`. Importers hold it across authoritative
  read/validation/merge/replacement. The initial importer still refuses overwrite.
  Atomic replacement prevents partial-file reads; the lock prevents competing
  supported writers from overwriting each other's work. Arbitrary external
  writers bypassing this lock are unsupported.
- Metadata saving checks current bindings and revision again before writing.
  The decision file is saved before CSV projection. A projection failure reports
  saved-but-stale state; it does not repeat or undo the authoritative save.
  No new history, database, file format or Master-selection rule was introduced.

Before extracting operations, preserve the direct consumers above and the
existing CLI/API shapes. First extraction candidates are snapshot/revision,
validation/save and projection operations; a full research-runner split is
deferred. Runtime root changes, imports into real review data, and retirement
of legacy writers require their own scoped work.

`archive_v3.py`, `reconciliation.py`, and `core_workbench.py` are retained
compatibility tools. Use them only when an existing documented workflow still
depends on them. Do not extend their generation, registry, package, or
publication concepts.

## Harness Routing

| Bucket | Meaning | Examples | Rule |
| --- | --- | --- | --- |
| `ACTIVE_ENTRYPOINT` | Maintained tool for unfinished work; running state is established separately. | `Archive-tools/workbench.py`, `search.py`, and `reference_batch.py`; unit-specific `tfa_workbench.py`/`collection_551_workbench.py` only when the central checkpoint and unit receipts confirm unfinished work. | May be run or changed only within the current task and with the protected-mount and no-delete guards intact. |
| `COMPLETED_UNIT_EVIDENCE` | Tool or receipt used to produce an already accessioned/published unit. | `collection_549_accession.py`, `collection_553_accession.py`, `baeckerhandwerk_accession.py`, `brigadebuecher_accession.py`, `wf_alben_accession.py`, `tfa_accession.py`, `wf_sender_accession.py`, Trafo accession tools. | Preserve for evidence and reproducibility. Do not reuse as a template without reconciling it to current `AGENTS.md`. |
| `LEGACY_DO_NOT_EXTEND` | Older architecture or compatibility path that conflicts with the current KISS workflow. | `core_workbench.py`, `archive_v3.py`, `catalog_core.py`, `archive_core/`, `interfaces/`, `workflows/`, `jobs/provenance_restoration/`, backup files named `*.pre-*`. | Do not extend. If needed, extract only the smallest proven helper into the current unit-specific workflow. |
| `SUPPORT_INDEX` | Rebuildable search, technical, visual, or provenance index. | `/mnt/volume/iss-archive-work/document-index`, `/mnt/volume/iss-archive-work/identity-provenance`, visual index tools. | Use read-only when possible. Never treat as archival authority. |

For a status audit, prefer a small read-only harness that reports mount state,
unit directories, receipt presence, SQLite quick checks, deletion flags, and
known open work. It must not start services, accession, publish, migrate,
rehash everything, or repair data.

## Legacy Compatibility

`archive_core/`, `interfaces/`, `workflows/`, `catalog_core.py`, `core_workbench.py`,
`archive_v3.py`, and the recovery scripts contain behavior from earlier pilots.
Preserve them until code cleanup is explicitly requested, because uncommitted
implementations and tests still depend on them.

They are not policy. Do not extend their:

- package generations;
- BagIt or multi-copy paths;
- identity registry;
- periodic or repeated fixity machinery;
- parallel catalog projections.

When legacy behavior conflicts with the canonical KISS workflow, stop and
resolve the mismatch before changing code.

## Safety

- Protected source roots remain read-only evidence.
- Recovery USB media must be mounted read-only.
- Tools write only to explicit work roots or final-unit roots.
- Existing output roots are not overwritten by default.
- Drain and deletion require separate path-specific approval.
- WordPress remains a projection/runtime consumer, not preservation-master
  storage.

Tests for complex metadata transformation, path protection, and destructive
refusal remain useful. Do not add tests merely to reproduce trivial file-copy
steps.
