#!/usr/bin/env bash
# Fail-closed shutdown watchdog for the Kurt Schwarz archive accession.

set -uo pipefail

readonly work_root="/mnt/volume/iss-archive-work/kurt-schwarz"
readonly final_unit="/mnt/volume/iss-archive/units/kurt-schwarz"
readonly completion_root="$work_root/work/completion"
readonly accession_run="$work_root/work/accession-runs/kurt-schwarz-full-001"
readonly publication="$accession_run/publication.json"
readonly state_log="$completion_root/shutdown-watchdog.log"
readonly pid_file="$completion_root/shutdown-watchdog.pid"

mkdir -p "$completion_root"

log() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$state_log"
}

refuse_shutdown() {
    log "REFUSED shutdown: $*"
    exit 1
}

validate_publication() {
    python3 - "$publication" "$final_unit" <<'PY'
import csv
import json
import sqlite3
import sys
from pathlib import Path

publication = Path(sys.argv[1])
final_unit = Path(sys.argv[2])
data = json.loads(publication.read_text(encoding="utf-8"))

expected = {
    "format": "iss-kurt-schwarz-publication-v1",
    "result": "ok",
    "final_unit": str(final_unit),
    "source_read_only": True,
    "source_safety_state": "safe",
    "write_boundary_state": "confined",
    "fatal_safety_errors": 0,
    "deletion_authorized_rows": 0,
}
for key, value in expected.items():
    if data.get(key) != value:
        raise SystemExit(
            f"publication field {key!r} is {data.get(key)!r}, expected {value!r}"
        )
for key in (
    "published_at",
    "object_rows",
    "representation_rows",
    "planned_representation_rows",
    "checksum_rows",
    "source_disposition_rows",
    "drain_list_rows",
    "non_destructive_errors",
):
    if data.get(key) is None:
        raise SystemExit(f"publication field is absent or empty: {key}")
if int(data["object_rows"]) != 1669:
    raise SystemExit("publication object count differs from reconciled scope")
if int(data["source_disposition_rows"]) != 17076:
    raise SystemExit("source disposition differs from reviewed scope")
if int(data["drain_list_rows"]) != 14688:
    raise SystemExit("drain list differs from exclusive USB scope")
if (
    int(data["representation_rows"]) + int(data["non_destructive_errors"])
    != int(data["planned_representation_rows"])
):
    raise SystemExit("representations are neither preserved nor error-logged")

if not final_unit.is_dir() or final_unit.is_symlink():
    raise SystemExit("final unit is absent or is a symlink")
required = (
    final_unit / "README.md",
    final_unit / "CATALOG.csv",
    final_unit / "CATALOG.xlsx",
    final_unit / "documents/CONTENTS.md",
    final_unit / "db-restore/RESTORE.sqlite",
    final_unit / "db-restore/RESTORE.sql",
    final_unit / "db-restore/RESTORE.csv",
    final_unit / "db-restore/RESTORE.xlsx",
    final_unit / "db-restore/checksums.csv",
    final_unit / "db-restore/manifests/representations.csv",
    final_unit / "db-restore/manifests/source-disposition.csv",
    final_unit / "db-restore/manifests/drain-list.csv",
    final_unit / "db-restore/manifests/copy-errors.csv",
)
for path in required:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"required final control file is absent: {path}")

with (final_unit / "db-restore/checksums.csv").open(
    encoding="utf-8-sig", newline=""
) as handle:
    checksum_rows = list(csv.DictReader(handle))
if len(checksum_rows) != int(data["checksum_rows"]):
    raise SystemExit("final checksum row count differs from publication")
if len({row["path"] for row in checksum_rows}) != len(checksum_rows):
    raise SystemExit("final checksum inventory contains duplicate paths")

disposition_path = final_unit / "db-restore/manifests/source-disposition.csv"
with disposition_path.open(encoding="utf-8-sig", newline="") as handle:
    disposition_rows = list(csv.DictReader(handle))
if len(disposition_rows) != int(data["source_disposition_rows"]):
    raise SystemExit("source disposition row count differs from publication")
if len({row["source_path"] for row in disposition_rows}) != len(disposition_rows):
    raise SystemExit("source disposition contains duplicate source paths")
if any(row["deletion_authorized"].casefold() != "false" for row in disposition_rows):
    raise SystemExit("source disposition contains deletion authorization")

drain_path = final_unit / "db-restore/manifests/drain-list.csv"
with drain_path.open(encoding="utf-8-sig", newline="") as handle:
    drain_rows = list(csv.DictReader(handle))
if len(drain_rows) != int(data["drain_list_rows"]):
    raise SystemExit("drain-list row count differs from publication")
if len({row["source_path"] for row in drain_rows}) != len(drain_rows):
    raise SystemExit("drain list contains duplicate source paths")
if any(row["deletion_authorized"].casefold() != "false" for row in drain_rows):
    raise SystemExit("drain list contains deletion authorization")

database = final_unit / "db-restore/RESTORE.sqlite"
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
try:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("final restore database integrity check failed")
    object_rows = connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    if object_rows != int(data["object_rows"]):
        raise SystemExit("restore database object count differs from publication")
    representation_rows = connection.execute(
        "SELECT COUNT(*) FROM representations"
    ).fetchone()[0]
    if representation_rows != int(data["representation_rows"]):
        raise SystemExit(
            "restore database representation count differs from publication"
        )
    source_rows = connection.execute(
        "SELECT COUNT(*) FROM source_disposition"
    ).fetchone()[0]
    if source_rows != int(data["source_disposition_rows"]):
        raise SystemExit(
            "restore database source-disposition count differs from publication"
        )
    database_drain_rows = connection.execute(
        "SELECT COUNT(*) FROM drain_list"
    ).fetchone()[0]
    if database_drain_rows != int(data["drain_list_rows"]):
        raise SystemExit("restore database drain-list count differs from publication")
finally:
    connection.close()
PY
}

printf '%s\n' "$$" >"$pid_file"
log "started pid=$$ publication=$publication"

while true; do
    if [[ -f "$publication" ]]; then
        before="$(sha256sum "$publication")" \
            || refuse_shutdown "cannot hash publication receipt"
        if ! validation_error="$(validate_publication 2>&1)"; then
            refuse_shutdown "publication validation failed: $validation_error"
        fi
        sleep 10
        after="$(sha256sum "$publication")" \
            || refuse_shutdown "cannot re-hash publication receipt"
        [[ "$before" == "$after" ]] \
            || refuse_shutdown "publication changed during validation"
        if ! validation_error="$(validate_publication 2>&1)"; then
            refuse_shutdown "publication changed after validation: $validation_error"
        fi
        source_options="$(findmnt -no OPTIONS /media/vladimir/Elements 2>&1)" \
            || refuse_shutdown "cannot inspect USB source mount"
        case ",$source_options," in
            *,ro,*) ;;
            *) refuse_shutdown "USB source mount is not read-only: $source_options" ;;
        esac
        if pgrep -f \
            'python3 .*kurt_schwarz_(hash_inventory|accession_plan|accession)\.py' \
            >/dev/null 2>&1; then
            log "publication is valid but a Kurt Schwarz archive job is still running"
            sleep 30
            continue
        fi
        log "publication validated; syncing archive filesystems"
        sync -f "$final_unit" || refuse_shutdown "final-unit sync failed"
        sync -f "$work_root" || refuse_shutdown "work-root sync failed"
        log "validated Kurt Schwarz completion; requesting system poweroff"
        exec sudo -n /usr/bin/systemctl poweroff
    fi
    sleep 30
done
