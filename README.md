# Industriesalon archive

Source code for archive discovery, reconciliation, human Master selection,
accession, and bounded local review workbenches.

## Layout

| Path | Source and purpose |
| --- | --- |
| `Archive-tools/` | Retained reconciliation tools and compatibility launchers. New research implementation lives in the installed package. |
| `src/archive_research/` | Installed research application: extractors, cross-unit retrieval, evidence, assessment and Django workbench. See its [application guide](src/archive_research/README.md). |
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
The project home is `/home/vladimir/archive-local`; its KEEP README owns current
locations and links to the sole private handoff. `/mnt/volume/archive-system` is
retained earlier setup material, not the current startup authority.
The 16 November retention-only cleanup moved the large historical Paragon recovery
tree into a verified volume bundle; the hub README owns its exact recovery location.
Referenced legacy helpers and active pilot/index state remain in their current roots.
The approved [local research pilot](src/archive_research/web/README.md) now shows
16 real photograph/report/relationship cases, including book and patent evidence,
with 51 prepared images and one explicitly unreadable source. It supports local
login, citations and bounded research review; all real observations begin
unreviewed. The synthetic demonstration and its export remain separate.

The [current consolidation plan](docs/architecture/archive-django-consolidation-plan.md#current-agreed-direction-and-checkpoint)
records the next phase: modular extractors, global unit/page mappings, positioned
OCR and cross-unit evidence search. Its ordered TODO includes portable root
configuration and eventual storage consolidation without permanent compatibility
symlinks. Necessary Linux runtime components stay in home for now; NTFS-specific
limitations must not become a permanent portability blocker. The tools audit,
shared package/configuration boundary, positioned PDF text/ALTO extraction and
bounded workbench page search/image leads are delivered. The current private
handoff records approved coverage, acceptance and the manually owned pilot.
Whole-archive indexing and storage migration remain later work.

## Research application

Install the project into the selected Python environment with `python -m pip install .`.
The [application guide](src/archive_research/README.md) documents optional dependency
sets, offline installation and configured roots. Use one entry point:

```text
archive-research extract   selected local extraction tasks
archive-research index     index selected source-bound receipts
archive-research search    cross-reference that index
archive-research packet    assemble selected evidence and hypotheses
archive-research run       bounded extraction/index/search/packet workflow
archive-research assess    prepare, validate or explicitly submit a model request
archive-research web       check, test or serve the local review application
```

`run` prepares local artifacts only. External submission remains explicit, and
model output remains an unadopted proposal. CLI and web code share application
modules; the installed package runs outside the checkout without `PYTHONPATH`.
Old research script paths forward to the package. The retained accession,
reconciliation and unit-specific tools have separate scopes and are not run by it.

## Local checkout and runtime

The dedicated checkout is `/home/vladimir/archive-local/source`, branch `archive/scripts`,
tracking `origin/main` at <https://github.com/vseberlin/archive>.
On 16 November 2026 the user approved a fresh source baseline on `main`.
Older remote branches retain their earlier history and are not maintained mirrors;
the workstation keeps its established `archive/scripts` checkout.
The initial source import was collected on 30 October 2026. It included then
uncommitted archive scripts from the old checkout, without importing WordPress
code or the old repository's mixed history.

Resolve current runtime and data paths through the home workspace's KEEP README
and private handoff. This repository does not automatically deploy scripts, start workbenches, or change
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

The retained photograph-reference batch runner is [`Archive-tools/reference_batch.py`](Archive-tools/reference_batch.py)
(v12); it is separate from the installed extraction/retrieval workflow above. Its helpers, regression tests and review UI live alongside it. The older
`work-area/visual-similarity/reference_batch.py` and its v2 notes are retained
history; use the `Archive-tools` runner for current work.

The [search source registry](Archive-tools/berlin-photo-reference-sources.json)
contains 19 public reference sources and subject routing. It is search
configuration, not a catalogue or a file of research results. Its access notes
are dated observations, not a guarantee of current website availability.
The published copy matches the workstation registry in
`/home/vladimir/archive-local/berlin-photo-reference-sources.json` at this sync.
The runner retains that workstation default; select the versioned copy with
`--registry /home/vladimir/archive-local/source/Archive-tools/berlin-photo-reference-sources.json`
when preparing and running a new, explicitly authorized batch. Existing plans
freeze script and registry hashes; consult the private handoff before resuming.

This checkout is independent of `/home/vladimir/wp-website` (website source).
The former `/home/vladimir/wp` mixed checkout is retained in verified bundles at
`/mnt/volume/sites-backup/website-consolidation-20261116/`; it is not a runtime or
source dependency. Maintained helpers use this archive checkout directly. Publishing here does not deploy or
remove any of those retained copies.

## Guarded Git workflow

[tools/repo-guard.py](tools/repo-guard.py) selects this repository from its own
location, independent of the caller's working directory. It uses standard-library
Python and system Git; it has no website or runtime dependency.

```sh
python3 /home/vladimir/archive-local/source/tools/repo-guard.py install
python3 /home/vladimir/archive-local/source/tools/repo-guard.py check
python3 /home/vladimir/archive-local/source/tools/repo-guard.py status
python3 /home/vladimir/archive-local/source/tools/repo-guard.py stage path/to/reviewed-file.py
python3 /home/vladimir/archive-local/source/tools/repo-guard.py commit "Describe the reviewed change"
python3 /home/vladimir/archive-local/source/tools/repo-guard.py sync
python3 /home/vladimir/archive-local/source/tools/repo-guard.py push
```

The policy binds the canonical root's `.git`, branch `archive/scripts`, and the
effective fetch/push URLs for `vseberlin/archive`. Another clone, detached/unexpected
branch, Git environment redirection, custom `core.hooksPath` (even empty), changed
remote, or unfinished Git operation is refused before mutation. A legitimate future
move needs an explicit policy update; the guard never guesses a new destination.

`install` installs local hooks without staging, committing, fetching or publishing.
`check` verifies them read-only. Other commands recreate a missing hooks directory
and repair missing/non-executable managed hooks after validating identity.
Unrelated hooks and symlinked hook files or directories
are preserved and refused; inspect/integrate them explicitly. Hooks are local Git
metadata and do not travel with a clone: install them at a new approved checkout.

`stage` takes explicit project-relative files (including reviewed deletions), not
directories or pathspec patterns. Escaping paths, external symlinks and nested Git
repositories are refused. Commit hooks reject staged gitlinks/submodules too.
`commit` commits the existing reviewed index. `sync` requires a clean tree and
only fast-forwards from fetched `origin/main`; ahead commits are retained and
divergence stops without stash/reset/rebase. `push` requires clean source and a
fast-forward from freshly fetched `origin/main`; its sole target is `HEAD:main`.
Pre-push hooks also reject other refs/remotes, deletion and divergent force pushes.

For explicitly requested publication through the connected GitHub tools, run
`publish-check` first. Its receipt identifies the root, HEAD, tree, destination and
`expected_remote_head`; use those exact values and an expected-head API update
that preserves ancestry. Rerun if either side changes. Terminal Git authentication
must be available for guarded terminal push; hooks do not provide credentials.

These checks protect project commands and normal hooks, not arbitrary shell or
API operations. Git in a third-party checkout cannot infer an intended archive
operation. Raw reset/clean, disabled hooks, edited policy and direct API calls can
bypass the boundary; agent instructions require the guarded entrypoint and receipt.
On refusal preserve state, inspect the cause and repair only the verified mismatch.
Destructive recovery requires explicit scope; never use hook bypass as a workaround.

Run the isolated regression checks with:

```sh
python3 -B /home/vladimir/archive-local/source/tests/archive-preservation/test_repo_guard.py -q
```

## Tests and dependencies

With the package installed in the qualified processing environment, run
`python -B -m unittest discover -s tests -p 'test_*.py'`. With explicit pilot
configuration, run `archive-research web test` for isolated Django tests.
The installed-worker test exercises a subprocess from an unrelated directory;
application tests also check changed-source rejection and local-only preparation.

Legacy tests retain their original layout. For example:

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
