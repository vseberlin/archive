# Industriesalon archive

Local archive research and cross-referencing for the Industriesalon collection.
The main application connects photographs, scanned documents, books, newspapers
and object records through positioned text, visual comparisons and cited evidence.
Selected evidence can be prepared for multimodal OpenAI assessment, then reviewed
in a local Django workbench. Machine findings remain proposals until reviewed.

This repository contains the application source, tests, workflow documentation,
and retained reconciliation/accession tools. Archive holdings, private research
results, accounts and runtime databases live outside the repository.

## Start here

- [Application guide](src/archive_research/README.md): installation, configuration,
  extractor contracts, retrieval, evidence packets and assessment commands.
- [Workbench guide](src/archive_research/web/README.md): local review, citations,
  configuration and launch instructions.
- [Implementation plan](docs/architecture/archive-django-consolidation-plan.md):
  agreed direction, delivered work and remaining limitations.
- [Tool map](docs/architecture/archive-application-structure.md): active, retained
  and unit-specific code.
- [Project instructions](AGENTS.md) and [archive contract](docs/architecture/archive-preservation-architecture-adr.md):
  source protection, authority and permitted operations.

## Repository layout

```text
archive/
├── pyproject.toml                # iss-archive-research package and dependencies
├── src/archive_research/         # active, installed research application
│   ├── cli.py, __main__.py       # archive-research / python -m archive_research
│   ├── config.py, pipeline.py   # shared configuration and application operations
│   ├── contracts.py, sources.py # source, page, region and result bindings
│   ├── storage.py, units.py     # readable artifacts and existing unit mappings
│   ├── preparation.py          # selected page/frame rendering
│   ├── geometry.py, runner.py   # coordinates and isolated extraction workers
│   ├── extractors/             # OCR, text regions, layouts and visual features
│   ├── retrieval.py            # text and visual cross-references
│   ├── evidence.py, profiles.py # evidence packets and subject-specific clues
│   ├── external_evidence.py     # retained external references and provenance
│   ├── sift.py                 # selected geometric comparisons
│   ├── assessment/             # request preparation, transport and validation
│   └── web/                    # Django application, templates and web tests
├── tests/                       # application tests and retained harness tests
├── Archive-tools/               # retained tools and research compatibility paths
├── tools/
│   ├── repo-guard.py           # guarded repository operations
│   └── archive-preservation/   # reusable and retained archive helpers
├── work-area/                   # source for unit-specific jobs and review UIs
└── docs/                        # architecture, workflow and published benchmarks
```

The CLI and workbench use the same application modules. The active package does
not import `Archive-tools` and works outside the checkout without `PYTHONPATH`.
Old research script paths and import names forward to the installed package.
Reconciliation/accession tools and completed unit jobs retain their separate
entry points; the research pipeline does not execute them.

## Research workflow

```text
Explicit local sources and page selections
    → extract text, regions, layout and visual features
    → index selected extraction records across archive units
    → retrieve text matches and visual candidates
    → assemble cited evidence, hypotheses and conflicting readings
    → optionally prepare and explicitly send a multimodal assessment
    → review the evidence and proposal in the workbench
```

Local archive evidence comes first. External references can supplement a packet
with retained provenance; a republication of the same archive record is not
independent corroboration. Visual similarity and geometric matches are research
leads, not automatic object or person identification.

| Area | Implemented behavior |
| --- | --- |
| Text extraction | Poppler PDF text; positioned Tesseract OCR; selected text/line regions, retained enhancement variants and original readings |
| Image and page analysis | PDF/scan layout candidates, caption regions, local OpenCLIP features and selected SIFT comparisons |
| Cross-references | Explicit unit/member/page bindings; text and visual search over selected receipts; source-change checks |
| Evidence and assessment | Buildings, people, equipment and tube profiles; source/detail views, hypotheses and citations; explicit API or ChatGPT-authenticated Codex submission |
| Local review | Django login, citation/region viewers, prepared search results, evidence packets and unadopted assessment proposals |

Extraction runs locally in explicit batches with per-task progress and reusable
receipts. HTTP requests read prepared results; they do not start extraction or
model calls. Human-readable evidence and review records remain authoritative;
SQLite provides rebuildable projections.

Current coverage is a bounded research pilot, not an archive-wide index.
Whole-archive processing, specialist detectors and held-out identification
accuracy remain future work. Person/face regions do not provide automatic names.
Packaging has been qualified locally on Linux; other-platform storage and
permission behavior still need qualification.

## Install and use

Requires Python 3.11 or newer. From the checkout, in the selected virtual
environment:

```sh
python -m pip install '.[web]'
archive-research --help
```

The core package has no third-party runtime dependencies. Optional dependency
sets in [pyproject.toml](pyproject.toml) are `web`, `images`, `vision` and
`embeddings`; select those needed by the operation. Poppler and Tesseract are
separate system tools. Visual adapters require explicitly supplied local models;
installation does not fetch model weights. The pinned CPU torch wheels need the
matching PyTorch CPU wheel index. See the application guide for the qualified
processing environment and offline installation.

| Command | Purpose |
| --- | --- |
| `archive-research extract` | Run selected extraction tasks and retain receipts |
| `archive-research index` | Index selected existing receipts |
| `archive-research search` | Query a bound index |
| `archive-research packet` | Assemble selected evidence and hypotheses |
| `archive-research run` | Coordinate extraction, indexing, search and packet preparation |
| `archive-research reference` | Index cached catalogue previews, evaluate agreement and import shared visual receipts |
| `archive-research assess` | Prepare, validate or explicitly submit an assessment |
| `archive-research web` | Configure, check, test or serve the local workbench |

Each command has `--help`. `retrieve` preserves the earlier combined index/search
interface. `python -m archive_research` exposes the same CLI.

A bounded workflow using existing receipts:

```sh
archive-research run --roots /path/to/roots.toml \
  --retrieval-selection /path/to/retrieval-selection.json \
  --packet-selection /path/to/packet-selection.json \
  --output-prefix selected-research
```

Add `--plan /path/to/plan.json` for extraction first, `--sift` for selected geometric
comparisons, or `--model MODEL` to prepare a local model request. **`run` never
submits externally.** Output prefixes must be unused. The application guide
specifies selection formats and source bindings; a clone does not include private
archive data or a configured runtime.

For an existing configured workbench:

```sh
archive-research web --project /path/to/project.toml \
  --machine /path/to/machine.toml check
archive-research web --project /path/to/project.toml \
  --machine /path/to/machine.toml serve
```

The manual server listens at `http://127.0.0.1:8785` without a reloader. Project and
private machine configuration select the existing runtime; see the workbench guide
for initialization and the separate synthetic demonstration.

## Tests and verification

With the package and required processing dependencies installed:

```sh
python -B -m unittest discover -s tests -p 'test_*.py'
archive-research web --project /path/to/project.toml \
  --machine /path/to/machine.toml test
```

The 17 November 2026 consolidation passed 384 tests: 119 application, 67 Django,
186 retained route/reference/workbench and 12 SIFT tests. The installed workflow
also ran outside the checkout and reproduced the prior index, evidence packet and
prepared model request. Existing readable records and database table counts stayed
unchanged. These checks establish local behavior, not collection-wide accuracy.

Legacy suites have their own dependencies and instructions. Some unit tests need
private evidence absent from Git; accession or drain executors are not test runners.
The [six-photo V11/V12 benchmark](docs/reference-v12-building-benchmark-20261114.md)
documents the retained photograph-reference runner and its evaluation limits.

## Retained tools and data boundaries

[Archive-tools](Archive-tools/README.md) includes reconciliation preparation,
workbenches and the retained [photograph-reference batch runner](Archive-tools/reference_batch.py).
Its [public reference registry](Archive-tools/berlin-photo-reference-sources.json)
is search configuration, not research results. Older copies under `work-area/`
are historical; consult the tool map before choosing an entry point.
`work-area/` mirrors unit-job source paths, not live holdings or review state.

Archive images, catalogues, decisions, credentials, databases, model weights and
raw research outputs are excluded from Git. The invented installed web fixture,
public search configuration and explicitly published benchmark measurements are
intentional source/documentation assets. One resolved archival object has one
selected Master; derivatives belong to authorized accession. Marking another
copy redundant does not authorize deletion.

On the maintained workstation, the source checkout is
`/home/vladimir/archive-local/source`, branch `archive/scripts` → GitHub `main`.
The private hub `/home/vladimir/archive-local/README.md` owns runtime/data locations;
`/home/vladimir/archive-local/HANDOFF.md` is the sole current checkpoint. Private
runtime stays outside this source tree. Historical paths and completed scripts
are not launch instructions. A source publication does not deploy services or
change archive data. See [continuity](docs/architecture/archive-continuity.md).

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
