# Archive Tool Structure

## Status

Implementation map for the existing Python tools. The archive contract is
`docs/architecture/archive-preservation-architecture-adr.md`; this document
does not define a second package, catalog, identity, or workflow model.

`/home/vladimir/archive` is the source-code and documentation repository for this
workstation. Unit scripts are versioned under `work-area/`, mirroring their
paths under `/mnt/volume/iss-archive-work`. Existing services still use the
original work paths; a commit does not deploy or execute them.
Use this file to route harnesses. Do not infer current policy from
old downloaded ADRs, local memories, historical handoffs, or completed run
artifacts.

## Dependency Direction

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

## Current Simplified Path

- `md_export_discovery.py`: bounded museum-digital evidence capture.
- `tiff_metadata.py` and `tiff_accession.py`: safe TIFF metadata write/readback
  helpers.
- unit-specific scripts under `/mnt/volume/iss-archive-work`: explicit
  reconciliation, accession, validation, and receipt work for a named unit.

`archive_v3.py`, `reconciliation.py`, and `core_workbench.py` are retained
compatibility tools. Use them only when an existing documented workflow still
depends on them. Do not extend their generation, registry, package, or
publication concepts.

## Harness Routing

| Bucket | Meaning | Examples | Rule |
| --- | --- | --- | --- |
| `ACTIVE_ENTRYPOINT` | Current tool for unfinished archive work, confirmed by its unit handoff. | Avantmedia `workbench.py`, `tfa_workbench.py`, and `collection_551_workbench.py`. | May be run or changed only within the current task and with the protected-mount and no-delete guards intact. |
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
