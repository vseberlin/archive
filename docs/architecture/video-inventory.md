# Industriesalon Video Inventory Workbench

This tool maintains a local discovery projection for reconciling
the two read-only source repositories:

- `/mnt/data/IS-videos/`: Industriesalon production, editing, source-capture,
  export, and thumbnail material.
- `/media/vladimir/Elements/restore/archive/MEDIATHEK-Backup/`: mixed reference
  and research media. Material here is not publication-cleared by default.

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

## Project evidence

See [private project notes](/mnt/volume/iss-archive-work/video-inventory/project-notes.md) for the dated discovery checkpoint.
Current archive continuation is in `/home/vladimir/archive-local/HANDOFF.md`;
website delivery uses `/home/vladimir/wp-website/handoff_CURRENT.md`.

## Current locations and invocation

- Source: `tools/archive-preservation/video_inventory.py` in this repository.
- Private database, reports and historical notes:
  `/mnt/volume/iss-archive-work/video-inventory/`.
- Default WordPress Compose directory: `/home/vladimir/wp-website`.
- Override locations with `--workbench` and the `wordpress` subcommand's
  `--compose-dir`; use `--help` to inspect commands without starting a job.

```sh
python3 tools/archive-preservation/video_inventory.py --help
```

The paused July census was relocated on 16 November 2026, not resumed or
recensused. Its historical path references remain evidence. Relocation does not
authorize scanning protected media, merging files, publication or accession.
