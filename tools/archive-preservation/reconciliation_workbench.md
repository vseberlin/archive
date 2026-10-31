# Consolidated reconciliation workbench

One localhost interface for reviewing prepared archival objects, comparing
candidate representations, selecting exactly one Master, and recording bounded
metadata choices and notes. It does not run accession, publication, discovery,
source repair, drain execution, or deletion.

The interface combines Collection 551's separation of binary and metadata
decisions, Collection 553's cross-object identity checks, NEF-style image
comparison, and Bibliothek/591-style PDF paging. It does not import their live
databases or execute their unit-specific repair functions.

## Run the synthetic demonstration

Use the existing checkout; a separate Git worktree is unnecessary. Requires
Linux, Python 3.10+, the existing Pillow image dependency, and Poppler's
`pdftoppm` for PDF previews (`pdftotext` for optional page-text comparison).
No database, frontend build, package installation, or network access is needed
when these tools are installed. Missing renderers surface as preview errors;
they do not alter source files.

From the repository root, choose a **new or empty** temporary demo directory:

```sh
python3 tools/archive-preservation/reconciliation_workbench.py \
  --demo --work-root /tmp/archive-reconciliation-demo --actor 'Your name'
```

The command creates only synthetic images, a two-page PDF and an input manifest
in the explicitly selected directory, then serves on loopback port 8780.
Open that port in a local browser on the same machine. There is no cloud preview
publication or automatic deployment. The demo refuses to overwrite an existing
work area. Restart it using the same command **without `--demo`**.

The three demo objects exercise image alternatives with a metadata conflict,
a multipage document, and a local-only ordered image set. Some members are
deliberately shared between candidates to demonstrate cross-object collision
refusal. Use the full source scan for DEMO-001 and the ordered set for LOCAL-003
to resolve both without a collision.

Stop with Ctrl-C. Processes do not survive environment snapshots; restart the
entrypoint against the retained work root as needed.

## Use prepared unit evidence

Run against an approved existing work area, never an accession/archive/source
root. Do not run two different workbench implementations as writers to the
same unit. Read its current handoff and completion receipts before mapping it.

```sh
python3 tools/archive-preservation/reconciliation_workbench.py \
  --work-root /absolute/approved/work-area --actor 'Your name' --check

python3 tools/archive-preservation/reconciliation_workbench.py \
  --work-root /absolute/approved/work-area --actor 'Your name' --port 8780
```

`--check` validates every source stamp as well as input and decision structure;
it does not write files. Inputs must explicitly establish candidate eligibility
and complete representations. This tool does not infer completeness, object
identity, or quality from a filename, extension, resolution, similarity score,
or museum-digital membership.

### Direct prepared CSV input

Without a manifest, the tool reads Bibliothek/Collection-591-style files:

- `objects.csv`: `object_key` or `md_object_id`, plus `title` and optional inventory.
- `candidates.csv`: the same object key, `candidate_id`, explicit
  `eligible=true|false`, optional `role`, `label`, `evidence`, and `members_json`.
- Optional `master-decisions.csv`: existing object key, `selected_candidate_id`,
  `note`/`decision_note`, `actor`, `decided_at`, `deletion_authorized=false`.

Members contain absolute `path` or `source_path`, integer `size_bytes`, integer
`mtime_ns`, and `pages` when greater than one. Existing `eligible=true` means
the preparation already established a complete representation. Elements sources
are automatically guarded. Other external source roots require the explicit
manifest below. No unmanifested file scanning occurs.

Existing choices are imported as the initial review state without rewriting
the original CSV. Once this workbench saves decisions, its readable file is the
authority for this interface. Changes to the legacy CSV will invalidate an open
form but will **not** silently replace saved consolidated decisions.

### Explicit input manifest

For other units, prepare `reconciliation-input.json` in the approved work root.
This is a bounded evidence mapping, not a migration of the original database.
TFA, Collection 551/553, NEF and other complex unit-specific schemas require
this explicit mapping; their raw databases are not automatically compatible.
In particular, presence of `master-candidates.csv` without a manifest is refused
instead of guessing compound representations or metadata rules.

Minimal example (use actual prepared stamps, not these placeholder numbers):

```json
{
  "format": "archive-reconciliation-1",
  "title": "Named unit",
  "read_only": false,
  "read_roots": ["/media/vladimir/Elements"],
  "completion_receipts": ["/absolute/existing/unit/publication.json"],
  "objects": [{
    "id": "existing-object-key",
    "inventory": "existing inventory",
    "title": "Recorded title",
    "provenance": "Existing catalogue or local evidence",
    "candidates": [{
      "id": "existing-candidate-key",
      "label": "Complete source representation",
      "eligible": true,
      "complete": true,
      "role": "candidate",
      "evidence": "Existing preparation established complete scope",
      "members": [{
        "path": "/media/vladimir/Elements/exact/source.tif",
        "size_bytes": 123456,
        "mtime_ns": 1234567890000000000,
        "pages": 1
      }]
    }],
    "metadata": [{
      "field": "Title",
      "choices": [{"id": "local", "value": "Recorded title", "source": "Existing inventory"}]
    }]
  }],
  "initial_decisions": []
}
```

Candidate IDs must be unique across the unit. `members` order defines the
representation order; a multi-file Master is one selected representation.
Optional member `sha256` is a stored lowercase SHA-256; unchanged media is not
rehashed. Shared paths or known hashes cannot be selected for two objects.
Roles `derivative`, `reference`, `component` and `excluded` are never eligible
Masters even if an input incorrectly sets eligibility true.

Map existing decisions into `initial_decisions` before the first save, preserving
all confirmed choices, notes and metadata evidence. Each row has `object_id`,
`selected_candidate_id` (empty for unresolved), `metadata_choices` (field name
to prepared choice ID), `note`, `actor`, `decided_at`, and boolean
`deletion_authorized: false`. Duplicate, foreign or contradictory decisions are
refused. Once a saved consolidated authority exists, it must cover all objects;
changing object membership requires an explicit reviewed reconciliation rather
than silently creating or discarding decisions.

Set `read_only: true` for completed units. List their actual existing completion
receipt paths; receipt presence freezes editing even if its contents are invalid.
`accession-receipt.json` or `publication.json` directly in the work root also
freezes edits. The workbench cannot discover unknown external completion
locations: a unit mapping must declare them correctly. An input file is not
permission to reopen a completed archive.

## Review behavior

- Needs review lists unresolved Masters and pending metadata. All and Complete
  views retain access to existing decisions; there is no automatic acceptance.
- Choose any prepared candidate in the A/B panes. Zoom, pan, fit, overlay and
  rendered difference help compare images. Each pane independently chooses a
  representation member and PDF/TIFF page.
- Exact-copy candidate occurrences are grouped by stored member hashes (or
  identical stamped member paths) when eligibility and role also agree.
  Expand the group to inspect every occurrence and original path. Similarity
  alone never collapses distinct candidates.
- Pixel differences are viewing evidence; they do not establish file equality
  or align different crops automatically. Optional PDF text displays embedded
  text side by side; it does not OCR, infer similarity or choose metadata.
- Select one eligible complete representation, or explicitly leave unresolved.
  Other alternatives are projected as redundant; excluded material stays excluded.
- Metadata radio choices refer only to existing supplied evidence; pending
  metadata stays pending even after a Master is selected. There is no general
  metadata editor and no implicit museum-digital authority.
- Missing or changed evidence is visible on candidate cards. A save verifies
  every member displayed for that object; refresh/reconcile changed preparation
  before recording a new decision. Source byte integrity is not established by
  file size/mtime alone; these stamps prevent ordinary unnoticed drift.

## Persistence and boundaries

The tool writes only these fixed names in the approved work root:

- `reconciliation-decisions.jsonl`: the current readable authority, including
  selected ordered members, supported metadata values, actor, timestamp and
  non-authorizing decisions.
- `reconciliation-dispositions.csv`: rebuildable member-level projection.
- `.reconciliation.lock`: interprocess lock for cooperating writers.

It retains no new history database, schema, generations, or journal. JSONL is
replaced atomically under thread/file locks. A revision over prepared inputs and
decisions rejects stale tabs or changed metadata evidence. Interrupted projection
writes cannot erase authoritative choices; the interface reports disagreement.
Refresh the derived CSV when needed:

```sh
python3 tools/archive-preservation/reconciliation_workbench.py \
  --work-root /absolute/approved/work-area --actor 'Your name' --refresh-projection
```

The command refuses completed/read-only units. Replacing prepared members or
the effective metadata of an already saved choice is refused; handle that
discrepancy explicitly rather than silently rebinding an accepted decision.

Source previews are decoded in memory, with bounded render concurrency and
a small cache keyed by recorded file stamps. Preview IDs resolve only to
prepared members under approved roots. Symlink sources/outputs are refused.
Elements access requires exactly `/dev/sdf1 /media/vladimir/Elements ntfs3 ro`.
No source hash is recomputed for browsing. Loopback binding, Host/Origin checks,
a per-process request token and bounded JSON protect decision requests.

## Portable validation

```sh
python3 -B -m unittest discover -s tests/archive-preservation \
  -p test_reconciliation_workbench.py -v
node --check tools/archive-preservation/reconciliation_workbench.js
```

Tests create synthetic media in temporary directories, including multipage PDF
and ordered representations. They exercise conflicting selections, source drift,
stale evidence, existing-choice import, read-only units, interrupted derived
writes, restart, protected mounts, HTTP request guards, and a real HTTP save.
The HTTP test needs permission to bind a localhost socket; an environment socket
restriction is not a reason to disable its assertions.

Live collection integration remains a separate verification against the unit's
existing evidence, handoff, receipt paths, prior choices and representation rules.
This implementation does not deploy to `/mnt/volume` or retire older interfaces.
