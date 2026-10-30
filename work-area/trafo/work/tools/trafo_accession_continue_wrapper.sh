#!/bin/bash
set -u

state=/mnt/volume/iss-archive-work/trafo/work/accession-runs/trafo-full-002/exception-pass-001
runner=/mnt/volume/iss-archive-work/trafo/work/tools/trafo_accession_continue.py

PYTHONDONTWRITEBYTECODE=1 python3 -B "$runner"
code=$?
sync
mkdir -p "$state"
printf '%s\n' "$code" > "$state/runner.exit.tmp"
mv -n "$state/runner.exit.tmp" "$state/runner.exit"
exit "$code"
