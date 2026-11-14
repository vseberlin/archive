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

## Local checkout and runtime

The dedicated checkout is `/home/vladimir/archive`, branch `archive/scripts`,
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

The new shared tools are versioned in `Archive-tools/`. On this workstation,
`/home/vladimir/Archive-tools` links to that directory, preserving the documented
commands. Their current private continuation checkpoint is
`/mnt/volume/iss-archive-work/HANDOFF.md`; generated review state remains outside
Git. Read that checkpoint before resuming preparation or an existing review.

For the legacy Avantmedia/Kurt Schwarz review, the workbench remains in
`/mnt/volume/iss-archive-work/avantmedia-305-review`, at
<http://127.0.0.1:8774/tiff-scan>. Its live handoff, readable decisions and SQLite
index stay in that work area. Recheck its service state before reuse. Source review must use the exact protected mount
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
`--registry /home/vladimir/archive/Archive-tools/berlin-photo-reference-sources.json`
when preparing and running a new, explicitly authorized batch. Existing plans
freeze script and registry hashes; consult the private handoff before resuming.

This checkout is independent of `/home/vladimir/wp-website` (website source).
`/home/vladimir/wp` retains historical mixed code and runtime mounts; it is not
the source for new archive development. Publishing here does not deploy or
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
