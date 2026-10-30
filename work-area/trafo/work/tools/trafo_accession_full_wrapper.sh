#!/bin/bash
set -u

run_root=/mnt/volume/iss-archive-work/trafo/work/accession-runs/trafo-full-002
runner=/mnt/volume/iss-archive-work/trafo/work/tools/trafo_accession_full.py
exit_file="$run_root/runner.exit"

PYTHONDONTWRITEBYTECODE=1 python3 -B "$runner"
code=$?
sync
mkdir -p "$run_root"
printf '%s\n' "$code" > "$exit_file.tmp"
mv -n "$exit_file.tmp" "$exit_file"
exit "$code"
