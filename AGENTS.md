# AGENTS.md - Archive Scripts

## Repository Scope

This is the dedicated archive source repository, checked out locally at
`/home/vladimir/archive` on branch `archive/scripts`.

It versions reusable helpers, unit-specific scripts, review interfaces, tests,
and archive workflow documentation. `work-area/` mirrors script paths relative
to `/mnt/volume/iss-archive-work`; it contains no live review data.

Existing services still run from their original work-area paths. A source commit
is not deployment or permission to rerun a completed job. Read the actual unit
handoff and receipts before executing a script. Keep data, images, decisions,
SQLite files, credentials, generated reports and backups out of this repository.

The original mixed checkout at `/home/vladimir/wp` and website checkout at
`/home/vladimir/wp-website` remain separate. Do not switch, merge, clean or
rewrite either checkout as part of archive source work.

This workspace uses a deliberately simple archive workflow:

1. reconcile existing files into archival objects;
2. select exactly one Master for each resolved object;
3. mark all other copies, derivatives, and rejected candidates as redundant;
4. accession the selected Master;
5. generate derivatives only during accession;
6. keep SQLite as a rebuildable catalogue/index while the filesystem remains
   understandable without it.

Simplicity is binding. Do not turn this into a DAM, preservation platform,
workflow engine, service system, transaction system, or distributed database.

## Authority

Follow, in order:

1. the user's current explicit request;
2. this file;
3. approved project documentation;
4. existing code and directory conventions.

The shared documentation and harness repository is `/home/vladimir/archive`.

- `/home/vladimir/archive/docs/` contains canonical repo-owned documentation.
- `/home/vladimir/archive/docs/architecture/archive-preservation-architecture-adr.md`
  is the detailed workflow contract, subordinate to this file.
- `/home/vladimir/archive/docs/architecture/archive-application-structure.md`
  routes active, completed, legacy, and support-index harnesses.
- `/home/vladimir/archive/tools/archive-preservation/` contains reusable helpers and
  retained compatibility code.

Older ADRs, downloaded drafts, local memories, skills, historical handoffs, and
completed run artifacts are evidence, not policy, when they conflict with this
file or the canonical docs above.

If instructions conflict, stop and ask. Do not resolve ambiguity by inventing a
larger system.

## Boundaries

Resolve actual paths from project configuration before work.

| Root | Access | Purpose |
| --- | --- | --- |
| `USB Elements/restore/archive` | read-only | Existing archive/source evidence |
| `iss-archive-work` | read/write | Reconciliation candidates and proposed decisions |
| `iss-archive` | read/write | Approved accession output |
| `/home/vladimir/archive` | explicit-approval write only | Source code, tests and workflow documentation |

Requested source-code and documentation changes may be made in this repository.
Do not write elsewhere unless the user explicitly authorizes that scope. Never modify source collections, backup media, discovered external
paths, or protected archive/source files.

For Elements source work, require the exact protected mount before source or
drain claims:

```text
/dev/sdf1 /media/vladimir/Elements ntfs3 ro
```

Reject writable fallback mounts and alternate targets such as `Elements1`.

## Archive Layout

Use only the approved archive-level structure:

```text
iss-archive/
├── 00_README.md
├── 00_MANIFESTS/
├── units/
├── 90_UNRECONCILED/
└── 99_TRANSIENT_REFERENCE/
```

New units may contain only:

```text
PROJECT/
├── 00_README.md
├── CATALOG.csv
├── CATALOG.xlsx
├── 00_MANIFESTS/
├── masters/
├── export/
├── metadata/
├── documents/
└── db-restore/
```

Do not add speculative folder levels by category, workflow state, date, format,
person, project, or processing step. Existing completed legacy units with older
equivalent paths remain evidence; do not restructure them without explicit
migration approval.

## Invariants

- One archival object has exactly one selected Master.
- Source files are never silently promoted, replaced, renamed, modified, moved,
  or deleted.
- Derivatives are generated from the selected Master during accession; they are
  not Master candidates.
- Redundant means not selected, not safe to delete.
- Deletion requires a separate explicit path-specific user request.
- The filesystem is the primary human-readable archive representation.
- SQLite is a catalogue/index, not the sole source of truth.
- Essential object identity, selected Master, provenance, relationships,
  redundant candidates, generated derivatives, and final hashes must exist in
  human-readable CSV/XLSX/JSONL/Markdown records.
- The archive is read-only during reconciliation.

## Workflow

For each object or unit:

1. Search approved local/archive evidence and relevant external catalogues.
2. Gather plausible Masters, derivatives, copies, source paths, and provenance
   under `iss-archive-work`.
3. Present candidates together in a bounded workbench.
4. Let a human select one Master or leave unresolved.
5. Record all other candidates as redundant, with notes where useful.
6. Reconcile metadata without inventing or silently overwriting conflicts.
7. After decisions are complete and accession is explicitly authorized, build
   one output unit under `iss-archive`.
8. Generate derivatives during accession.
9. Hash finalized accession output once and update SQLite plus human-readable
   manifests.

Workbench actions must stay limited to: select one Master, mark others
redundant, leave unresolved, and record a short note. Do not build a general
metadata editor, role system, task queue, approval workflow, or management UI.

## SQLite And Hashing

- Keep SQLite schemas small and rebuildable from files/manifests.
- Store durable decisions in human-readable records as well as SQLite.
- Hash candidates only when needed for identification or deduplication.
- Hash final accession output once after metadata embedding and derivative
  generation are complete.
- Reuse stored hashes for unchanged files.

Do not add PostgreSQL/MySQL, ORMs, event sourcing, replication, hash chains,
periodic fixity jobs, BagIt/OCFL, package generations, rollback systems, or
extra checksum/manifest frameworks unless the user explicitly changes the
architecture.

## Approval Gates

Explicit user approval is required before:

- any new directory or nesting level;
- any new database, service, daemon, container, queue, dependency, workflow
  phase, role, approval state, identifier system, or metadata authority;
- any change to Master-selection rules;
- any write outside `iss-archive-work`, `iss-archive`, or the approved
  docs/harness exception;
- accession, publication, sealing, migration, broad refactor, schema redesign,
  source mutation, archive mutation, deletion, versioning, rollback, or audit
  subsystem work.

Before implementing a change, state which existing requirement it satisfies and
make the smallest change that satisfies it.

## Completion

A task is complete only when the requested behavior works, exactly-one-Master
rules are preserved, redundant candidates are recorded without destructive
source changes, derivatives come only from accession, SQLite and readable
records agree, approved structure is unchanged, metadata is reconciled, and no
unrequested subsystem was introduced.


## Source Disposition And Continuity

Every new archive unit must have `00_MANIFESTS/source-disposition.csv`; an
already completed legacy unit may keep an accepted equivalent such as
`db-restore/manifests/source-disposition.csv`. Before sealing, cover all known
exact-path drain candidates. This table never authorizes deletion.

Generated catalogs, provenance tables and unit receipts record archive work.
Do not copy their rows or session history into website continuity files. The
current archive checkpoint stays in the existing archive workspace/unit handoff;
`/home/vladimir/wp-website/handoff_CURRENT.md` is website-only. Older downloaded
ADRs, local memories and completed-run artifacts are evidence, not policy.
