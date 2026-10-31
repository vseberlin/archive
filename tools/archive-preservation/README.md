# Archive Preservation Harness

This directory contains reusable archive helpers, completed-unit scripts, and
legacy compatibility code. It is not the archive policy source.

Current authority:

1. `/home/vladimir/archive/AGENTS.md` (archive rules; the home-level file only routes workspaces)
2. `../../docs/architecture/archive-preservation-architecture-adr.md`
3. `../../docs/architecture/archive-application-structure.md`
4. unit manifests, receipts, catalogues, and current handoff state

Do not read old ADRs, historical handoffs, local memory, or completed run
artifacts as policy.

## Current Rules

- New units use `00_README.md`, `CATALOG.csv`, `CATALOG.xlsx`,
  `00_MANIFESTS/`, `masters/`, `export/`, `metadata/`, `documents/`, and
  `db-restore/`.
- Legacy completed units may keep `README.md` or `db-restore/manifests/`.
- A resolved object has exactly one selected Master.
- Derivatives are generated only during accession.
- SQLite is a catalogue/index; human-readable files remain sufficient to
  understand the unit.
- `source-disposition.csv` and drain lists never authorize deletion.
- Reuse stored hashes for unchanged files; do not add repeated whole-unit
  rehashing.
- Do not add package generations, BagIt/OCFL, registry systems, background
  daemons, queues, or parallel archive frameworks.

## Harness Buckets

| Bucket | Use | Rule |
| --- | --- | --- |
| `ACTIVE_ENTRYPOINT` | Current unfinished work, as established by the unit handoff. | Run/change only for the current task. Keep protected-mount, one-Master, and no-delete guards. |
| `COMPLETED_UNIT_EVIDENCE` | Scripts/receipts that produced already accessioned or published units. | Preserve for evidence. Do not extend as templates without reconciling to current policy. |
| `LEGACY_DO_NOT_EXTEND` | Old package, generation, registry, or broad workbench code. | Do not load for ordinary tasks. Extract only a small proven helper when explicitly needed. |
| `SUPPORT_INDEX` | Rebuildable technical, visual, metadata, or provenance indexes. | Use read-only where possible. Never treat as archival authority. |

## Active Entrypoints

- Consolidated reconciliation interface: `reconciliation_workbench.py`.
  See [input, startup and test instructions](reconciliation_workbench.md).
  Uses prepared evidence and readable decisions; does not deploy or replace
  existing unit workbenches automatically.
- Avantmedia image and metadata review:
  `/mnt/volume/iss-archive-work/avantmedia-305-review/workbench.py`
- TFA remaining image review:
  `/mnt/volume/iss-archive-work/tfa/work/tools/tfa_workbench.py`
- Collection 551 review:
  `/mnt/volume/iss-archive-work/collection-551/collection_551_workbench.py`
- Paragon source review and drain are completed. Retain their code as evidence;
  consult the unit's current records before any separate catalogue repair.
- Read-only status audit:
  use a small bounded checker when needed. It may report mount state, unit
  list, receipt presence, SQLite quick checks, deletion flags, and open work.
  It must not start services, accession, publish, migrate, repair, delete, or
  rehash everything.

## Helper Entrypoints

- `md_export_discovery.py`: bounded museum-digital evidence capture.
- `tiff_metadata.py`: TIFF metadata write/readback helper.
- `tiff_accession.py`: TIFF accession helper used by unit scripts.
- `visual_index.py` / `visual_indexer.py`: rebuildable visual evidence helpers.

## Legacy Exclusion

Do not load or extend these for normal archive work:

- `archive.py`
- `archive_v3.py`
- `catalog_core.py`
- `core_workbench.py`
- `workbench.py`
- `archive_core/`
- `interfaces/`
- `workflows/`
- `jobs/provenance_restoration/`
- `recovery_*`, `surrogate_*`, and `*_drain_*` tools
- backup files named `*.pre-*`

These files may still explain historical outputs. They do not override current
architecture.

## Workbench Rules

Every active workbench must be direct and bounded:

- show all candidates for the object together;
- show preview, original path, format/size/technical metadata, IDs, provenance,
  and hash/similarity evidence when available;
- allow only these archival decisions: select one Master, mark others
  redundant, leave unresolved, and record a short note;
- keep metadata reconciliation explicit and separate from Master selection;
- write durable decisions to human-readable CSV/JSONL/Markdown and mirror to
  SQLite only when useful;
- never modify source files, generate derivatives, accession, publish, drain,
  delete, or create services;
- enforce `deletion_authorized=false`;
- fail when the protected source guard is required and not proven.

## Before Running Anything

1. Read `/mnt/volume/iss-archive-work/HANDOFF.md` if current archive state matters.
2. Identify the unit and bucket from
   `../../docs/architecture/archive-application-structure.md`.
3. Verify the protected source when in scope:

   ```bash
   findmnt -n -o SOURCE,TARGET,FSTYPE,OPTIONS --target /media/vladimir/Elements
   ```

   Required Elements state is `/dev/sdf1 /media/vladimir/Elements ntfs3 ro`.
4. Prefer the unit's own `--check`.
5. Report what is verified and what remains blocked.
