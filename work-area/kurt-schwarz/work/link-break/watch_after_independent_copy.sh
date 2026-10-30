#!/bin/bash
set -u

copy_pid="${1:?copy PID required}"
unit_root=/mnt/volume/iss-archive/units/kurt-schwarz
predecessor_root=/mnt/volume/iss-archive/units/kurt-schwarz-pre-subdivisions
run_root=/mnt/volume/iss-archive-work/kurt-schwarz/work/link-break/kurt-schwarz-independent-media-001
copy_receipt="$run_root/receipt.json"
failure_log="$run_root/copy-failures.jsonl"
completion_signal="$run_root/completion-signal.json"

while kill -0 "$copy_pid" 2>/dev/null; do
    sleep 30
done

python3 - "$unit_root" "$predecessor_root" "$copy_receipt" "$failure_log" "$completion_signal" <<'PY'
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

unit = Path(sys.argv[1])
predecessor = Path(sys.argv[2])
copy_receipt = Path(sys.argv[3])
failure_log = Path(sys.argv[4])
completion_signal = Path(sys.argv[5])

missing = []
size_mismatches = []
linked = []
independent = []
temporaries = []
scan_error = None
rows = []

try:
    with (unit / "db-restore/checksums.csv").open(
        newline="", encoding="utf-8-sig"
    ) as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        path = unit / row["path"]
        predecessor_path = predecessor / row["path"]
        temporary = path.with_name(f".{path.name}.independent-copy.tmp")
        if temporary.exists():
            temporaries.append(row["path"])
        if not path.is_file() or not predecessor_path.is_file():
            missing.append(row["path"])
            continue
        stat = path.stat()
        predecessor_stat = predecessor_path.stat()
        if stat.st_size != int(row["size_bytes"]):
            size_mismatches.append(row["path"])
        if stat.st_nlink != 1 or (
            stat.st_dev == predecessor_stat.st_dev
            and stat.st_ino == predecessor_stat.st_ino
        ):
            linked.append(row["path"])
        else:
            independent.append(row["path"])
except Exception as error:
    scan_error = f"{type(error).__name__}: {error}"

receipt = None
receipt_error = None
if copy_receipt.exists():
    try:
        receipt = json.loads(copy_receipt.read_text(encoding="utf-8"))
    except Exception as error:
        receipt_error = f"{type(error).__name__}: {error}"

failure_events = 0
if failure_log.exists():
    with failure_log.open(encoding="utf-8") as handle:
        failure_events = sum(1 for line in handle if line.strip())

mount = subprocess.run(
    ["findmnt", "-rn", "-S", "/dev/sdf1", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"],
    capture_output=True,
    text=True,
    check=False,
).stdout.strip()
source_state = mount or "/dev/sdf1 unmounted"
source_unsafe = False
if mount:
    for line in mount.splitlines():
        options = set(line.split(None, 3)[3].split(","))
        if "ro" not in options or "rw" in options:
            source_unsafe = True

data_loss_risk = bool(
    scan_error
    or receipt_error
    or missing
    or size_mismatches
    or source_unsafe
)
if data_loss_risk:
    result = "halted_or_ended_with_safety_risk"
elif receipt is None:
    result = "process_ended_without_completion_receipt"
elif linked:
    result = "completed_with_logged_failures"
else:
    result = "completed_ok"

payload = {
    "format": "iss-kurt-schwarz-independent-media-completion-signal-v1",
    "result": result,
    "signalled_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    "shutdown_requested": False,
    "poweroff_requested": False,
    "copy_receipt": str(copy_receipt) if receipt is not None else None,
    "copy_receipt_result": receipt.get("result") if receipt else None,
    "copy_receipt_error": receipt_error,
    "published_unit": str(unit),
    "predecessor": str(predecessor),
    "checksum_rows": len(rows),
    "independent_published_files": len(independent),
    "remaining_linked_published_files": len(linked),
    "missing_files": len(missing),
    "size_mismatches": len(size_mismatches),
    "temporary_files": len(temporaries),
    "failure_events_logged": failure_events,
    "scan_error": scan_error,
    "source_state": source_state,
    "source_unsafe": source_unsafe,
    "data_loss_risk": data_loss_risk,
    "deletion_authorized": False,
}

temporary_signal = completion_signal.with_suffix(".json.tmp")
with temporary_signal.open("x", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary_signal, completion_signal)
directory_fd = os.open(completion_signal.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
PY

# This watchdog emits status only. It never invokes shutdown or poweroff.
