# Industriesalon archive scripts

Source code for archive discovery, reconciliation, human Master selection,
accession, and bounded local review workbenches.

## Layout

| Path | Source and purpose |
| --- | --- |
| `Archive-tools/` | Shared photo reconciliation preparation and workbench, with configurable Czkawka profiles. See its README for the contract and commands. |
| `tools/archive-preservation/` | Shared helpers and retained compatibility tools, copied from the mixed `/home/vladimir/wp` checkout. |
| `tests/archive-preservation/` | Existing tests for the shared tools. |
| `work-area/` | Unit-specific scripts and UI assets, preserving their relative paths under `/mnt/volume/iss-archive-work`. Includes Avantmedia, TFA, Paragon, Kurt Schwarz, Costa and other collections. |
| `docs/architecture/` | Archive workflow contract and tool routing. |

Read [AGENTS.md](AGENTS.md), the [workflow contract](docs/architecture/archive-preservation-architecture-adr.md),
and the [tool map](docs/architecture/archive-application-structure.md) before
running archive operations. Legacy tools and completed jobs are retained as
source evidence; their presence does not authorize running them again.

The [WF research workbench plan](docs/architecture/archive-django-consolidation-plan.md)
proposes a bounded Django pilot connecting photographs, documents, and patents,
with portable research configuration and an optional FiftyOne comparison.
The volume project entry point is `/mnt/volume/archive-system`; its location map
points to the consolidated home workspace `/home/vladimir/archive-local` and the
existing private handoff. Read that home workspace's KEEP README before cleanup.
The approved [local research pilot](Archive-tools/research_web/README.md) now shows
16 real photograph/report/relationship cases, including book and patent evidence,
with 51 prepared images and one explicitly unreadable source. It supports local
login, citations and bounded research review; all real observations begin
unreviewed. The synthetic demonstration and its export remain separate.

The [current consolidation plan](docs/architecture/archive-django-consolidation-plan.md#current-agreed-direction-and-checkpoint)
records the next phase: modular extractors, global unit/page mappings, positioned
OCR and cross-unit evidence search. Its ordered TODO includes portable root
configuration and eventual storage consolidation without permanent compatibility
symlinks. Necessary Linux runtime components stay in home for now; NTFS-specific
limitations must not become a permanent portability blocker. This is the planning
checkpoint before new implementation, indexing or migration begins.

## Local checkout and runtime

The dedicated checkout is `/home/vladimir/archive-local/source`, branch `archive/scripts`,
tracking `origin/archive/scripts` at <https://github.com/vseberlin/archive>.
The verified source is published on `main` and mirrored to `archive/scripts`;
the workstation keeps its established `archive/scripts` checkout.
The initial source import was collected on 30 October 2026. It includes current
uncommitted archive scripts from the old checkout, without importing WordPress
code or the old repository's mixed history.

Existing services continue to execute their original script paths. This
repository does not automatically deploy scripts, start workbenches, or change
archive data. Many unit scripts expect their original local data beside them or
use absolute workstation paths. They are not standalone applications that can
run against an empty clone.

The shared tools are versioned in `Archive-tools/`. On this workstation, use
the real `/home/vladimir/archive-local/source/Archive-tools` directory; the old
`~/Archive-tools` compatibility link is retired. Their private continuation checkpoint is
`/home/vladimir/archive-local/HANDOFF.md`; generated review state remains outside
Git. Read it when starting a task or when current state is missing/changed; reuse
unchanged guidance after compaction. See [continuity](docs/architecture/archive-continuity.md).

For the legacy Avantmedia/Kurt Schwarz review, the workbench remains in
`/mnt/volume/iss-archive-work/avantmedia-305-review`, at
<http://127.0.0.1:8774/tiff-scan>. Its historical `review-checkpoint-20261015.md`, readable decisions and SQLite
index stay in that work area; current continuation uses the central handoff. Recheck its service state before reuse. Source review must use the exact protected mount
`/dev/sdf1 /media/vladimir/Elements ntfs3 ro`.

## Source-only boundary

Archive payloads, source images, catalogues, review decisions, databases,
credentials, generated reports, downloaded websites, binaries and backup
script copies are excluded. The `.gitignore` protects common data formats;
review each staged change before publication to this public repository.

One resolved object has one selected Master. Derivatives are generated during
authorized accession. Marking a candidate redundant never authorizes deletion.

## Current photo research source

The working research runner is [`Archive-tools/reference_batch.py`](Archive-tools/reference_batch.py)
(v12). Its helpers, regression tests and review UI live alongside it. The older
`work-area/visual-similarity/reference_batch.py` and its v2 notes are retained
history; use the `Archive-tools` runner for current work.

The [search source registry](Archive-tools/berlin-photo-reference-sources.json)
contains 19 public reference sources and subject routing. It is search
configuration, not a catalogue or a file of research results. Its access notes
are dated observations, not a guarantee of current website availability.
The published copy matches the workstation registry in
`/home/vladimir/Downloads/berlin-photo-reference-sources.json` at this sync.
The runner retains that workstation default; select the versioned copy with
`--registry /home/vladimir/archive-local/source/Archive-tools/berlin-photo-reference-sources.json`
when preparing and running a new, explicitly authorized batch. Existing plans
freeze script and registry hashes; consult the private handoff before resuming.

This checkout is independent of `/home/vladimir/wp-website` (website source).
The former `/home/vladimir/wp` mixed checkout is retained in verified bundles at
`/mnt/volume/sites-backup/website-consolidation-20261116/`; it is not a runtime or
source dependency. Maintained helpers use this archive checkout directly. Publishing here does not deploy or
remove any of those retained copies.

## Tests and dependencies

Tests retain their original layout. For example:

```sh
python3 -B -m unittest discover -s tests/archive-preservation -p test_md_export_discovery.py
python3 -B -m unittest discover -s tests/archive-preservation -p test_tiff_metadata.py
```

Other tests may require Pillow, ExifTool, GCC/OpenSSL, or local unit evidence.
The existing `tools/archive-preservation/requirements.txt` belongs to the legacy
harness; it is not a complete installation recipe for every unit script.
Avantmedia's tests depend on local scan evidence, which is intentionally absent
from Git. Do not run accession or drain executors as a test.

The [six-photo V11/V12 comparison report](docs/reference-v12-building-benchmark-20261114.md) includes
per-photo benchmark results, evidence-quality limitations, reproduction commands
and the exact pilot-source reconstruction patch. This report and its selected
measurements are an explicitly authorized publication exception to the runtime
data boundary; private images and raw logs remain excluded.
