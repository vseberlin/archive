# Industriesalon archive scripts

Source code for archive discovery, reconciliation, human Master selection,
accession, and bounded local review workbenches.

## Layout

| Path | Source and purpose |
| --- | --- |
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
The initial source import was collected on 30 October 2026. It includes current
uncommitted archive scripts from the old checkout, without importing WordPress
code or the old repository's mixed history.

Existing services continue to execute their original script paths. This
repository does not automatically deploy scripts, start workbenches, or change
archive data. Many unit scripts expect their original local data beside them or
use absolute workstation paths. They are not standalone applications that can
run against an empty clone.

For Avantmedia, the running workbench remains in
`/mnt/volume/iss-archive-work/avantmedia-305-review`, at
<http://127.0.0.1:8774/tiff-scan>. Its live handoff, readable decisions and SQLite
index stay in that work area. Source review must use the exact protected mount
`/dev/sdf1 /media/vladimir/Elements ntfs3 ro`.

## Source-only boundary

Archive payloads, source images, catalogues, review decisions, databases,
credentials, generated reports, downloaded websites, binaries and backup
script copies are excluded. The `.gitignore` protects common data formats;
review each staged change before publication to this public repository.

One resolved object has one selected Master. Derivatives are generated during
authorized accession. Marking a candidate redundant never authorizes deletion.

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
