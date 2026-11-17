# Industriesalon Video Inventory Workbench

This tool maintains a local discovery projection for reconciling
two read-only source repositories selected through private configuration:

- Production, editing, source-capture, export and thumbnail material.
- Mixed reference and research media; publication rights are not implied.

The source repositories are never renamed, moved, deleted, or rewritten by
this tool. The SQLite database, CSV files, and reports below are not the
preservation authority. Generated observations can be rebuilt, but preserve any edited project/rights
review files and the existing database before rebuilding. No deletion is implied.

## Generated outputs

- `data/inventory.sqlite`: restartable working census.
- `data/files.csv`: every observed path occurrence.
- `data/media.csv`: AV technical properties and provisional roles.
- `data/projects.csv`: editable projects and dependency-resolution status.
- `data/rights-review.csv`: publication-rights queue; unknown is the default.
- `data/duplicate-size-candidates.csv`: candidates only, not proof of identity.
- `data/wordpress-videos.csv`: current local WordPress Video records.
- `data/candidate-matches.csv`: fuzzy discovery candidates requiring review.
- `reports/inventory-status.md`: compact current status.

## Safety boundary

Inventory comes before merge. A future merged root must be built copy-only
from a frozen, reviewed manifest. No source is releasable until copied,
SHA-256 verified, and independently backed up. Rights status and publication
status are explicit human decisions; filename or repository location never
grants publication rights.

## Project evidence and invocation

Dated discovery notes, runtime locations and current status belong in the private
handoff. Website delivery has separate operating records. The maintained source
is `tools/archive-preservation/video_inventory.py` in this repository.

Use `--workbench` for an explicit private output directory and the `wordpress`
subcommand's `--compose-dir` for an explicit Compose project. Legacy defaults are
deployment-specific; use `--help` without starting a job:

```sh
python3 tools/archive-preservation/video_inventory.py --help
```

Existing census records remain private evidence. Documentation or source changes
do not authorize scanning protected media, merging files, publication or accession.
