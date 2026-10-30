# Archive Workflow

## Status

Accepted. This is the canonical preservation workflow for all archive units and
projects. It supersedes the former package-format, identity-registry,
reconciliation-workbench, provenance-restoration, and simplified-v3 design
documents.

The design follows KISS. It guards against ordinary staff neglect, accidental
source changes, incomplete copying, guessed matches, and lost provenance. It is
not a banking, forensic, or adversarial-security system.

## Authority

`/home/vladimir/archive/AGENTS.md` is the archive agent entrypoint. This document is
the detailed, repo-owned workflow contract for agents that need more context.
If this document, older ADRs, local memories, downloaded drafts, or a tool
README conflict with `AGENTS.md`, follow `AGENTS.md` and treat the other source
as historical evidence until it is corrected.

The shared documentation and harness repository is `/home/vladimir/archive`.
Archive policy belongs under `docs/`; reusable harness routing belongs in
`docs/architecture/archive-application-structure.md`; tool entrypoint notes
belong in `tools/archive-preservation/README.md`.

## Objective

Process one project or archival unit, referred to below as `PROJECT_X`.

The workflow is linear:

1. discover and match candidates;
2. gather evidence under `iss-archive-work`;
3. let a human select exactly one Master per resolved object;
4. record all other candidates as redundant without deletion authority;
5. prepare one approved accession output under `iss-archive`;
6. generate derivatives during accession;
7. write final hashes, SQLite catalogue data, and human-readable manifests.

Do not add workflow versions, generations, preservation packages, state
machines, hash chains, repeated hashing, or parallel archive frameworks.

## Standard Unit Layout

Every new final unit uses the established filesystem-readable layout:

```text
PROJECT_X/
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

- `00_README.md` explains the unit, its scope, unresolved gaps, and recovery
  entrypoints.
- `CATALOG.csv` and `CATALOG.xlsx` are readable views of the same current
  catalog state.
- `00_MANIFESTS/` contains accession receipts, representation manifests,
  source dispositions, drain lists, and other human-readable control records.
- `masters/` contains the authoritative preservation files.
- `export/` contains accession-generated access derivatives and approved
  external export files. Use `export/access`, `export/md`, or `export/web` only
  when the unit already needs that grouping.
- `metadata/` contains supporting metadata captures, reconciliation summaries,
  and external catalogue evidence.
- `documents/` contains confirmed project documentation and, when documents are
  retained, `documents/CONTENTS.md`.
- `db-restore/` contains the current material needed to restore the catalog.
  It is not a history of database versions or generations.

Existing units with `README.md`, `db-restore/manifests/`, or other accepted
legacy equivalents remain valid evidence. Do not rename or restructure them
unless the user explicitly requests a migration.

Preserve meaningful grouping inside these directories when it carries
provenance or object structure. Do not add unnecessary folder levels. Existing
units are not renamed or restructured unless migration is explicitly requested.

## 1. Discovery And Matching

Search for `PROJECT_X` in:

- museum-digital;
- the protected local archive;
- relevant read-only recovery media or indexes when they are in scope.

Use available evidence, including:

- project names and alternative spellings;
- filenames and folder names;
- museum-digital IDs;
- titles and descriptions;
- dates;
- creators, photographers, and organisations;
- embedded metadata;
- visual and contextual relationships;
- associated inventories and documentation.

Discovery candidates are not automatically archive members. First record a
relevance decision:

- `included`: confirmed relevant to the project;
- `excluded`: reviewed and not part of the project;
- `needs_review`: relevance remains unresolved.

Only `included` material enters the accession output. This prevents
false-positive search hits and cross-unit material from entering the archive.

### Museum-digital matching

For each included local file:

- link it to museum-digital when a sufficiently reliable match exists;
- preserve the evidence supporting the match;
- mark several plausible matches as `ambiguous`; never guess;
- retain relevant local material even when museum-digital has no record.

Use these match states:

- `matched`;
- `ambiguous`;
- `no_match_found`;
- `not_searched`.

`no_match_found` is valid only after an appropriate search was completed.
Absence from one supplied spreadsheet or export is not proof of absence from
museum-digital. Record the search source or method and date.

### Provenance

At minimum, record:

- original source path;
- original filename;
- source size and modification time;
- museum-digital object ID and URL, when matched;
- match status and basis;
- work or accession-output path;
- final archive path;
- object key or existing inventory number;
- file role and sequence when an object has several files.

The provenance chain maps locations, identities, and decisions. It does not
require duplicate preservation packages or historical file generations.

## 2. Work Evidence And Metadata Enrichment

Use one work area for `PROJECT_X` under `iss-archive-work`. Sources are
evidence:

- never modify source files;
- confirm removable recovery media is mounted read-only;
- treat designated archive and intake roots as protected;
- write only inside the approved work root and final unit root, except for
  explicitly approved documentation/harness edits.

Copy only included candidates into the approved accession output when accession
is authorized.

Enrich accession copies with reliable metadata from museum-digital, embedded
metadata, filenames, folder context, associated documents, and established
project information.

- Do not invent missing values.
- Do not silently overwrite conflicting evidence.
- For ambiguous matches, embed only independently reliable metadata.
- For unmatched files, retain available local/contextual metadata and record
  `no_match_found`.
- Embed metadata only when the format and the established tool path support it
  safely.
- If safe embedding is not established, leave the file bytes unchanged and
  store metadata in the catalog.
- Do not create derivatives unless the unit requires an access or external
  export copy.

After a metadata write, perform one immediate format/readback check. For images,
confirm that metadata reads back and the image still decodes correctly. This is
part of the write operation, not another workflow phase, preservation version,
or hashing cycle.

## 3. Final Archive Creation

Create the final unit only after reconciliation and metadata decisions are
complete. Use a guarded copy or same-filesystem rename that refuses unintended
overwrite and reports failures.

Do not remove work evidence until the final catalog, file counts, documentation
contents, and checksum inventory have completed successfully.

An interrupted run must be safe to resume or repeat for only the affected
files. It must not create a new package generation.

## Documentation

Copy confirmed project documentation into `documents/`. This can include:

- project descriptions and correspondence;
- reports and publications;
- captions and research notes;
- original spreadsheets and inventories;
- technical or administrative documents with a documented project relation.

Do not include a document merely because a broad search found a keyword. Record
its relationship as `direct`, `mixed`, `contextual`, or `excluded`; only
confirmed retained documents belong in the final unit.

Create `documents/CONTENTS.md`. For every retained document include:

- filename;
- short description;
- date, when known;
- original source path or container/member path;
- relationship to the project;
- condition when damaged or partly unreadable.

Preserve original document bytes. Do not redesign, normalize, or manufacture
document versions. When an explicitly approved ZIP is used to consolidate
scattered originals, index its members and provenance; do not also retain a
second duplicate set unless requested.

## Catalog And Restore State

The catalog contains one row per archived file and, where available:

- final relative path;
- filename and file type;
- object key or inventory number;
- file role and sequence;
- title, description, date, and creator;
- museum-digital object ID and URL;
- match status and basis;
- original source path;
- final checksum.

Archived files are the preservation authority. The restore database describes
them; the CSV/XLSX catalog and provenance table are readable workflow records
of the same current state. Use a simple schema and existing identifiers. Do not
introduce a new identifier system or general archive-management framework.

`db-restore/` holds one current restore set. Replacing that set after an
explicit catalog correction does not create or retain a restore generation.

## Source Disposition And Sealing

Every new unit must contain `00_MANIFESTS/source-disposition.csv`. Create it
even when the unit has no drain candidates; in that case it contains the
defined header and no data rows, and the unit `00_README.md` states the
reviewed source scope and that no candidates were found.

Existing legacy units may store the equivalent table under
`db-restore/manifests/source-disposition.csv`. Treat that as accepted legacy
evidence, not as the template for new units.

The table is the unit's complete, path-specific drain-candidate register. By
the time a unit is ready for sealing, it must contain every known source file
for that unit that may later be considered for draining. Do not leave
candidates only in workbench decisions, temporary manifests, notes, or
spreadsheets.

Each row must record enough evidence to review the source independently:

- exact source file path, or exact container/member reference;
- source role and object/inventory relation when known;
- accepted final path or paths;
- preservation status and basis;
- source and final checksum when already established by the accession;
- whether an exact byte copy was proven;
- drain state;
- `deletion_authorized=false`;
- an explanatory note for exceptions.

Use this minimum CSV header, in this order:

```text
source_path,source_kind,unit,inventory,source_role,final_path,preservation_status,preservation_basis,source_sha256,final_sha256,source_size_bytes,exact_copy_proven,drain_state,deletion_authorized,notes
```

Use one row per exact source path. Do not use directory-wide targets, globs, or
inferred filename matches. Include explicit retain/block rows for source
losses, mixed-project containers, unresolved provenance, or any other source
that must not enter a later drain operation. Generated derivatives with no
source file on the medium do not create drain rows.

`candidate_after_separate_review` means only that the unit contains an accepted
preservation representation and that the source may be reviewed later. It does
not mean the source may be deleted. Sealing the unit does not change
`deletion_authorized` and does not grant drain authority.

## Hashing

Calculate SHA-256 once for each archived payload or copied documentation file
after it has reached its final archive location. Store the result in
`00_MANIFESTS/payload-checksums.csv` or the unit's accepted legacy equivalent,
and in the current catalog/restore state.

Reuse stored checksums for unchanged files. Do not:

- hash sources, work files, and final files as repeated workflow gates;
- rehash unchanged media;
- create hash generations or version manifests;
- add periodic fixity jobs;
- add BagIt, OCFL, or another packaging standard;
- treat generated catalogs as self-checksumming payload.

Generated control files such as the catalog, README, and restore exports do not
store their own checksum inside themselves.

## Explicit Non-goals

Do not add any of the following unless separately requested:

- preservation versions or generations;
- workflow state machines;
- multiple work or accession-output generations;
- append-only or hash-chained journals;
- repeated package validation;
- automatic derivative programmes;
- duplicate archival packages;
- independent identifier registries;
- speculative metadata;
- generalized infrastructure for future projects;
- deletion or modification of original source files.

Drain, retirement, source deletion, or cleanup is never implied by accession.
It requires a separate, explicit, path-specific decision.

## Completion And Sealing Criteria

The unit is complete when:

1. museum-digital and approved local sources were searched;
2. candidate relevance was reviewed;
3. all included material was recorded in work evidence or the final accession
   output;
4. matched, ambiguous, no-match, and not-searched states were recorded;
5. provenance from source to final path was preserved;
6. reliable metadata was embedded or cataloged without invention;
7. modified files passed one immediate format/readback check;
8. final files occupy the standard unit layout, or a documented accepted legacy
   layout for already completed units;
9. documentation is retained and indexed in `documents/CONTENTS.md`;
10. final payload/document files have one SHA-256 checksum;
11. current catalog, provenance table, and restore files exist and are readable;
12. `00_MANIFESTS/source-disposition.csv` exists, or an already completed
    legacy unit has an accepted equivalent;
13. source files remain unchanged.

The unit is ready for sealing only when the source-disposition table contains
all known drain candidates for that unit plus the explicit retain/block
exceptions needed to make later review safe. This completeness check is
required even when no drain operation is planned.

Before implementing anything outside this workflow, stop and ask for
permission.

## Documentation Discipline

Archive reports and receipts belong with the run under the approved work root.
Do not copy every command, check, intermediate count, or corrected row into
root continuity documents.

- `handoff_CURRENT.md` records only the current operational checkpoint, risks,
  protected boundaries, and next action.
- `TODO.md` records only active, executable follow-up—not completed work,
  investigation history, or speculative programmes.
- Git history is the implementation changelog. Source code and this canonical
  document are the implementation record.
- Generated unit catalogs and provenance tables are the archive workflow
  record.

Do not duplicate implementation or workflow history in repository continuity
files. If a step does not change accepted state, operational risk, or the next
action, it does not require a handoff or TODO edit.
