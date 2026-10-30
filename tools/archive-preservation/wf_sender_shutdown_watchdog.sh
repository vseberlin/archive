#!/usr/bin/env bash
set -uo pipefail

readonly accession_pid="${1:?accession PID required}"
readonly accession_starttime="${2:?accession start time required}"
readonly run_root="${3:?run root required}"
readonly final_unit="${4:?final unit required}"
readonly publication="$run_root/publication.json"
readonly state_log="$run_root/shutdown-watchdog.log"
readonly pid_file="$run_root/shutdown-watchdog.pid"
readonly expected_command="/home/vladimir/wp/tools/archive-preservation/wf_sender_accession.py"

log() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$state_log"
}

fail_open() {
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
    "result": "ok",
    "objects": 1863,
    "museum_digital_ids": 1862,
    "payload_representations": 7209,
    "documents_and_md_exports": 23,
    "catalog_rows": 7232,
    "final_unit": str(final_unit),
    "promotion": "same-filesystem atomic rename",
    "media_rehashed_after_promotion": False,
}
for key, value in expected.items():
    if data.get(key) != value:
        raise SystemExit(
            f"publication field {key!r} is {data.get(key)!r}, expected {value!r}"
        )
if not data.get("published_at"):
    raise SystemExit("publication has no published_at timestamp")
if not final_unit.is_dir() or final_unit.is_symlink():
    raise SystemExit("final unit is absent or is a symlink")
required = (
    final_unit / "README.md",
    final_unit / "db-restore/RESTORE.sqlite",
    final_unit / "db-restore/checksums.csv",
    final_unit / "db-restore/manifests/source-disposition.csv",
)
for path in required:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"required final control file is absent: {path}")
with (final_unit / "db-restore/checksums.csv").open(
    encoding="utf-8", newline=""
) as handle:
    if sum(1 for _ in csv.DictReader(handle)) != 7232:
        raise SystemExit("final checksum inventory does not contain 7232 rows")
connection = sqlite3.connect(
    f"file:{final_unit / 'db-restore/RESTORE.sqlite'}?mode=ro",
    uri=True,
)
try:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("final restore database integrity check failed")
    if connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0] != 1863:
        raise SystemExit("final restore database object count differs")
    if (
        connection.execute("SELECT COUNT(*) FROM representations").fetchone()[0]
        != 7232
    ):
        raise SystemExit("final restore database representation count differs")
finally:
    connection.close()
PY
}

printf '%s\n' "$$" >"$pid_file"
log "started pid=$$ accession_pid=$accession_pid accession_starttime=$accession_starttime"

while true; do
    if [[ -f "$publication" ]]; then
        if ! validation_error="$(validate_publication 2>&1)"; then
            fail_open "publication validation failed: $validation_error"
        fi
        sleep 5
        if ! validation_error="$(validate_publication 2>&1)"; then
            fail_open "publication changed after validation: $validation_error"
        fi
        source_options="$(findmnt -no OPTIONS /media/vladimir/Elements 2>&1)" \
            || fail_open "cannot inspect source mount"
        case ",$source_options," in
            *,ro,*) ;;
            *) fail_open "source mount is not read-only: $source_options" ;;
        esac
        log "publication validated; syncing final archive filesystem"
        sync -f "$final_unit" || fail_open "filesystem sync failed"
        log "validated accession complete; requesting system poweroff"
        exec sudo -n /usr/bin/systemctl poweroff
    fi

    if [[ ! -r "/proc/$accession_pid/stat" ]]; then
        fail_open "accession process exited without a valid publication receipt"
    fi
    current_starttime="$(awk '{print $22}' "/proc/$accession_pid/stat" 2>/dev/null)" \
        || fail_open "cannot read accession process identity"
    [[ "$current_starttime" == "$accession_starttime" ]] \
        || fail_open "accession PID was reused"
    current_command="$(tr '\0' ' ' <"/proc/$accession_pid/cmdline" 2>/dev/null)" \
        || fail_open "cannot read accession command"
    case "$current_command" in
        *"$expected_command"*) ;;
        *) fail_open "accession command identity changed: $current_command" ;;
    esac
    sleep 30
done
