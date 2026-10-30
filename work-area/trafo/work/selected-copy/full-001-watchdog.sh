#!/usr/bin/env bash
set -euo pipefail

PID=224036
OUTPUT="/mnt/volume/iss-archive-work/trafo/work/selected-copy/full-001"
LOG="/mnt/volume/iss-archive-work/trafo/work/selected-copy/full-001-watchdog.log"
EXPECTED_RECORDS=1548
EXPECTED_FILES=11402

timestamp() {
  date -Is
}

log() {
  printf '%s %s\n' "$(timestamp)" "$*" >> "$LOG"
}

fail() {
  log "FAIL: $*"
  exit 1
}

pid_is_target() {
  [[ -r "/proc/$PID/cmdline" ]] || return 1
  local cmdline
  cmdline="$(tr '\0' ' ' < "/proc/$PID/cmdline")"
  [[ "$cmdline" == *"trafo_copy_pilot.py"* && "$cmdline" == *"full-001"* ]]
}

validate_summary() {
  python3 - "$OUTPUT" "$EXPECTED_RECORDS" "$EXPECTED_FILES" <<'PY'
import csv
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected_records = int(sys.argv[2])
expected_files = int(sys.argv[3])
summary_path = root / "summary.json"
selected_path = root / "manifests" / "selected-copy.csv"
checksums_path = root / "manifests" / "checksums.csv"

if not summary_path.is_file():
    raise SystemExit("missing summary.json")
if not selected_path.is_file():
    raise SystemExit("missing selected-copy.csv")
if not checksums_path.is_file():
    raise SystemExit("missing checksums.csv")

summary = json.loads(summary_path.read_text(encoding="utf-8"))
if summary.get("records") != expected_records:
    raise SystemExit(f"records mismatch: {summary.get('records')} != {expected_records}")
if summary.get("copied_files") != expected_files:
    raise SystemExit(f"copied_files mismatch: {summary.get('copied_files')} != {expected_files}")

with selected_path.open(encoding="utf-8", newline="") as handle:
    selected_rows = list(csv.DictReader(handle))
with checksums_path.open(encoding="utf-8", newline="") as handle:
    checksum_rows = list(csv.DictReader(handle))
if len(selected_rows) != expected_files:
    raise SystemExit(f"selected row mismatch: {len(selected_rows)} != {expected_files}")
if len(checksum_rows) != expected_files:
    raise SystemExit(f"checksum row mismatch: {len(checksum_rows)} != {expected_files}")
bad_pdfinfo = [
    row["inventory"]
    for row in selected_rows
    if row["role"] in {"local_pdf", "local_pdf_fallback"}
    and row.get("pdfinfo_status") != "0"
]
if bad_pdfinfo:
    raise SystemExit("pdfinfo failures: " + ", ".join(bad_pdfinfo[:20]))
PY
}

log "watchdog start pid=$PID output=$OUTPUT"
if ! pid_is_target; then
  fail "pid $PID is not the expected full-001 copy process"
fi

while pid_is_target; do
  sleep 60
done

log "copy process exited; waiting for output to settle"
sleep 10
validate_summary >> "$LOG" 2>&1 || fail "summary/manifests validation failed"

if find "$OUTPUT" -type l -print -quit | grep -q .; then
  fail "symlink found under output"
fi
if find "$OUTPUT" -type f -links +1 -print -quit | grep -q .; then
  fail "hard-linked file found under output"
fi
if [[ -e /mnt/volume/iss-archive/units/trafo ]]; then
  fail "final Trafo unit exists unexpectedly"
fi

log "clean finish validated; requesting shutdown"
sync
sudo /sbin/shutdown -h now "Trafo full selected copy clean finish"
